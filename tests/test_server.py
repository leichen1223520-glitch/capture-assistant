"""T8 本机只读检索服务的接口、边界和生命周期测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.models import Card
from app.server import (
    LOCAL_API_HOST,
    LocalApiServer,
    create_app,
)
from app.store import Store

_TEST_PNG = b"\x89PNG\r\n\x1a\nserver-test"


def _card(
    text: str,
    *,
    source_url: str | None = "https://example.test/watch?v=1",
    source_title: str | None = "本地测试",
) -> Card:
    card_id = str(uuid4())
    return Card(
        id=card_id,
        text=text,
        edited_text="人工整理后的观点",
        text_source="ocr",
        confidence=0.91,
        screenshot_path=f"screenshots/{card_id}.png",
        full_screenshot_path=f"screenshots/full_{card_id}.png",
        source_url=source_url,
        source_title=source_title,
        video_time=5.5,
        app_name="chrome.exe",
        monitor={"width": 1920, "height": 1080, "scale": 1.0},
        created_at="2026-07-26T12:00:00+08:00",
        stance="useful",
        note="用于服务测试",
    )


class ReadOnlyServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.store = Store(self.data_dir / "cards.sqlite3", self.data_dir)
        self.store.init_db()
        self.card = _card("这是可搜索的本地观点")
        self._write_images(self.card)
        self.store.add_card(self.card)
        self.app = create_app(self.store)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _write_images(self, card: Card) -> None:
        for relative_path in (card.screenshot_path, card.full_screenshot_path):
            path = self.data_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_TEST_PNG)

    def test_rejects_untrusted_host_header(self) -> None:
        response = self.client.get("/search", headers={"host": "attacker.test"})

        self.assertEqual(response.status_code, 400)


    def test_rejects_cross_site_and_foreign_origin_requests(self) -> None:
        cross_site = self.client.get(
            "/search",
            headers={"sec-fetch-site": "cross-site"},
        )
        foreign_origin = self.client.get(
            "/search",
            headers={"origin": "https://attacker.test"},
        )
        local_origin = self.client.get(
            "/search",
            headers={"origin": "http://127.0.0.1:8000"},
        )

        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(foreign_origin.status_code, 403)
        self.assertEqual(local_origin.status_code, 200)


    def test_search_recent_and_single_card(self) -> None:
        response = self.client.get("/search", params={"q": "本地观点"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], self.card.id)
        detail = self.client.get(f"/cards/{self.card.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["text"], self.card.text)

        recent = self.client.get("/search")
        self.assertEqual(recent.json()["items"][0]["id"], self.card.id)
        missing = self.client.get(f"/cards/{uuid4()}")
        self.assertEqual(missing.status_code, 404)

    def test_screenshot_serves_selected_and_full_png(self) -> None:
        selected = self.client.get(f"/screenshot/{self.card.id}")
        full = self.client.get(
            f"/screenshot/{self.card.id}",
            params={"kind": "full"},
        )

        self.assertEqual(selected.status_code, 200)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(selected.content, _TEST_PNG)
        self.assertTrue(selected.headers["content-type"].startswith("image/png"))
        self.assertEqual(selected.headers["x-content-type-options"], "nosniff")

    def test_screenshot_rejects_tampered_path_outside_root(self) -> None:
        payload = self.card.model_dump()
        payload["screenshot_path"] = "../outside.png"
        unsafe_card = Card.model_construct(**payload)
        outside = self.data_dir.parent / "outside.png"
        outside.write_bytes(_TEST_PNG)

        with patch.object(self.store, "get_card", return_value=unsafe_card):
            response = self.client.get(f"/screenshot/{self.card.id}")

        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.content, _TEST_PNG)

    def test_screenshot_rejects_non_png_content(self) -> None:
        (self.data_dir / self.card.screenshot_path).write_bytes(
            b"<script>alert('not an image')</script>"
        )

        response = self.client.get(f"/screenshot/{self.card.id}")

        self.assertEqual(response.status_code, 404)

    def test_screenshot_rejects_file_over_size_limit(self) -> None:
        screenshot = self.data_dir / self.card.screenshot_path
        screenshot.write_bytes(_TEST_PNG + b"too-large")

        with patch("app.server_impl._MAX_SCREENSHOT_BYTES", len(_TEST_PNG)):
            response = self.client.get(f"/screenshot/{self.card.id}")

        self.assertEqual(response.status_code, 404)


    def test_search_page_escapes_content_and_never_links_javascript_url(self) -> None:
        malicious = _card(
            "PROMPT </div><script>alert('screen')</script>",
            source_url="javascript:alert(document.cookie)",
            source_title="<img src=x onerror=alert(1)>",
        )
        malicious.edited_text = "<svg onload=alert(2)>"
        malicious.note = "</div><iframe srcdoc='<script>x</script>'>"
        self._write_images(malicious)
        self.store.add_card(malicious)

        response = self.client.get("/", params={"q": "PROMPT"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert('screen')</script>", response.text)
        self.assertNotIn('href="javascript:', response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn("&lt;img", response.text)
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertIn('/export?format=json&amp;q=PROMPT', response.text)

        exported = self.client.get(
            "/export",
            params={"format": "json", "q": "PROMPT"},
        )
        self.assertEqual(exported.status_code, 200)
        self.assertEqual([item["id"] for item in exported.json()], [malicious.id])

    def test_export_json_and_markdown(self) -> None:
        json_response = self.client.get("/export", params={"format": "json"})
        markdown_response = self.client.get("/export", params={"format": "md"})

        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json_response.json()[0]["id"], self.card.id)
        self.assertEqual(markdown_response.status_code, 200)
        self.assertIn("# 本地观点卡片导出", markdown_response.text)
        self.assertIn(self.card.text, markdown_response.text)
        self.assertIn(".md", markdown_response.headers["content-disposition"])

    def test_export_rejects_a_library_over_the_single_request_limit(self) -> None:
        second = _card("第二张本地观点")
        self.store.add_card(second)

        with patch("app.server_impl._MAX_EXPORT_CARDS", 1):
            response = self.client.get("/export", params={"format": "json"})

        self.assertEqual(response.status_code, 413)
        self.assertIn("单次导出上限", response.json()["detail"])

    def test_write_methods_are_not_exposed(self) -> None:
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/search")
                self.assertEqual(response.status_code, 405)

        exposed_methods = set()
        for route in self.app.routes:
            exposed_methods.update(getattr(route, "methods", set()) or set())
        self.assertTrue({"POST", "PUT", "PATCH", "DELETE"}.isdisjoint(exposed_methods))

    def test_query_and_paging_limits_are_validated(self) -> None:
        self.assertEqual(
            self.client.get("/search", params={"q": "x" * 501}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/search", params={"limit": 101}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/export", params={"format": "html"}).status_code,
            422,
        )


class LocalApiServerTests(unittest.TestCase):
    def test_non_loopback_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            LocalApiServer(host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            LocalApiServer(host="localhost")

    def test_background_lifecycle_is_repeatable(self) -> None:
        class FakeUvicornServer:
            def __init__(self, config: object) -> None:
                self.config = config
                self.started = False
                self.should_exit = False
                self.force_exit = False

            def run(self) -> None:
                self.started = True
                while not self.should_exit and not self.force_exit:
                    time.sleep(0.001)

        with patch(
            "app.server_impl.uvicorn.Server",
            FakeUvicornServer,
        ):
            server = LocalApiServer(host=LOCAL_API_HOST, port=8123)
            self.assertIs(server.start(timeout=1.0), server)
            self.assertTrue(server.running)
            self.assertIs(server.start(timeout=1.0), server)
            server.stop(timeout=1.0)
            self.assertFalse(server.running)
            server.stop(timeout=1.0)

        self.assertEqual(server.host, "127.0.0.1")
        self.assertEqual(server.port, 8123)

    def test_card_ids_must_be_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            store = Store(data_dir / "cards.sqlite3", data_dir)
            store.init_db()
            with TestClient(
                create_app(store), base_url="http://127.0.0.1"
            ) as client:
                response = client.get("/cards/not-a-uuid")

        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(UUID(str(uuid4())), UUID)


if __name__ == "__main__":
    unittest.main()
