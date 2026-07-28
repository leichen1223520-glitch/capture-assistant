from __future__ import annotations

import asyncio
import json
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.bridge import BrowserBridge, BrowserContext, get_browser_context


def _bridge_uri(bridge: BrowserBridge) -> str:
    port = bridge.bound_port
    assert port is not None
    return f"ws://127.0.0.1:{port}"


async def _hello(websocket: object) -> None:
    await websocket.send('{"type":"hello","protocol":3}')
    acknowledgement = json.loads(await websocket.recv())
    assert acknowledgement == {"type": "hello_ack", "protocol": 3}


def _context_message(request_id: str, **overrides: object) -> dict[str, object]:
    """构造字段完整的 v3 context 消息，单项测试只覆盖关心的值。"""

    message: dict[str, object] = {
        "type": "context",
        "request_id": request_id,
        "url": "https://example.test/",
        "title": "示例页面",
        "selection": "",
        "video_time": None,
        "sensitive_input": False,
        "tab_id": 7,
        "observation_text": "",
        "observation_kind": "none",
        "video_key": "",
    }
    message.update(overrides)
    return message


@pytest.fixture
def bridge() -> BrowserBridge:
    instance = BrowserBridge(port=0)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def test_module_level_bridge_is_inert_until_started() -> None:
    assert get_browser_context(timeout=0.01) is None


def test_no_connection_returns_none_without_waiting_for_timeout(
    bridge: BrowserBridge,
) -> None:
    started = time.perf_counter()
    result = bridge.get_browser_context(timeout=0.3)
    elapsed = time.perf_counter() - started

    assert result is None
    assert elapsed < 0.15


def test_protocol_v2_hello_is_rejected(bridge: BrowserBridge) -> None:
    async def scenario() -> int | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await websocket.send('{"type":"hello","protocol":2}')
            with pytest.raises(ConnectionClosed) as caught:
                await websocket.recv()
            return caught.value.rcvd.code if caught.value.rcvd is not None else None

    assert asyncio.run(scenario()) == 1008
    assert bridge.is_running


def test_real_websocket_round_trip_returns_validated_context(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            assert request["type"] == "get_context"
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": request["request_id"],
                        "url": "https://example.test/watch?v=1",
                        "title": "示例视频",
                        "selection": "保留原始观点",
                        "video_time": 12.5,
                        "sensitive_input": False,
                        "tab_id": 7,
                        "observation_text": "保留原始观点",
                        "observation_kind": "selection",
                        "video_key": "video-1:0",
                    },
                    ensure_ascii=False,
                )
            )
            return await request_task

    assert asyncio.run(scenario()) == BrowserContext(
        url="https://example.test/watch?v=1",
        title="示例视频",
        selection="保留原始观点",
        video_time=12.5,
        sensitive_input=False,
        tab_id=7,
        observation_text="保留原始观点",
        observation_kind="selection",
        video_key="video-1:0",
    )


def test_sensitive_context_returns_no_selection(bridge: BrowserBridge) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": request["request_id"],
                        "url": "https://example.test/login",
                        "title": "登录",
                        "selection": "",
                        "video_time": None,
                        "sensitive_input": True,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                )
            )
            return await request_task

    assert asyncio.run(scenario()) == BrowserContext(
        url="https://example.test/login",
        title="登录",
        selection="",
        video_time=None,
        sensitive_input=True,
        tab_id=7,
    )


@pytest.mark.parametrize(
    ("sensitive_input", "selection"),
    [
        ("false", ""),
        (0, ""),
        (None, ""),
        (True, "不得传递的敏感文字"),
    ],
)
def test_invalid_sensitive_context_is_rejected(
    bridge: BrowserBridge,
    sensitive_input: object,
    selection: str,
) -> None:
    async def scenario() -> tuple[int | None, BrowserContext | None]:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": request["request_id"],
                        "url": "https://example.test/login",
                        "title": "登录",
                        "selection": selection,
                        "video_time": None,
                        "sensitive_input": sensitive_input,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                )
            )
            with pytest.raises(ConnectionClosed) as caught:
                await websocket.recv()
            close_code = (
                caught.value.rcvd.code if caught.value.rcvd is not None else None
            )
            return close_code, await request_task

    close_code, result = asyncio.run(scenario())
    assert close_code == 1008
    assert result is None
    assert bridge.is_running


def test_caption_context_accepts_optional_tab_id_and_bounded_video_identity(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    _context_message(
                        str(request["request_id"]),
                        url="https://example.test/video",
                        title="字幕视频",
                        video_time=8.25,
                        tab_id=None,
                        observation_text="这是一条原生字幕",
                        observation_kind="caption",
                        video_key="video-2:3",
                    ),
                    ensure_ascii=False,
                )
            )
            return await request_task

    assert asyncio.run(scenario()) == BrowserContext(
        url="https://example.test/video",
        title="字幕视频",
        selection="",
        video_time=8.25,
        sensitive_input=False,
        tab_id=None,
        observation_text="这是一条原生字幕",
        observation_kind="caption",
        video_key="video-2:3",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"tab_id": True},
        {"tab_id": -1},
        {"tab_id": 2_147_483_648},
        {"observation_text": 1},
        {"observation_text": "候选", "observation_kind": "none"},
        {"observation_text": "", "observation_kind": "selection"},
        {"observation_text": "   ", "observation_kind": "caption"},
        {"observation_text": "候选", "observation_kind": "unknown"},
        {
            "observation_text": "候" * 16_385,
            "observation_kind": "caption",
        },
        {"video_key": None},
        {"video_key": "document-1:video-2"},
        {"video_key": "v" * 129},
        {
            "sensitive_input": True,
            "observation_text": "不得传递的敏感候选",
            "observation_kind": "selection",
        },
        {"unexpected": True},
    ],
)
def test_invalid_observation_context_is_rejected(
    bridge: BrowserBridge,
    overrides: dict[str, object],
) -> None:
    async def scenario() -> tuple[int | None, BrowserContext | None]:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    _context_message(str(request["request_id"]), **overrides),
                    ensure_ascii=False,
                )
            )
            with pytest.raises(ConnectionClosed) as caught:
                await websocket.recv()
            close_code = (
                caught.value.rcvd.code if caught.value.rcvd is not None else None
            )
            return close_code, await request_task

    close_code, result = asyncio.run(scenario())
    assert close_code == 1008
    assert result is None
    assert bridge.is_running


def test_timeout_and_stale_request_id_do_not_poison_next_request(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> tuple[BrowserContext | None, BrowserContext | None]:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)

            timed_out_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.06)
            )
            expired_request = json.loads(await websocket.recv())
            timed_out = await timed_out_task
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": expired_request["request_id"],
                        "url": "https://stale.invalid/",
                        "title": "过期",
                        "selection": "不应返回",
                        "video_time": None,
                        "sensitive_input": False,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                )
            )

            valid_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            valid_request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": valid_request["request_id"],
                        "url": "https://fresh.test/",
                        "title": "新请求",
                        "selection": "有效",
                        "video_time": 0,
                        "sensitive_input": False,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                )
            )
            return timed_out, await valid_task

    timed_out, valid = asyncio.run(scenario())
    assert timed_out is None
    assert valid is not None
    assert valid.url == "https://fresh.test/"
    assert valid.video_time == 0.0


def test_extension_error_resolves_only_matching_request(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "context_error",
                        "request_id": request["request_id"],
                        "error": "content_script_unavailable",
                    }
                )
            )
            return await request_task

    assert asyncio.run(scenario()) is None


def test_invalid_message_is_closed_without_crashing_server(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> int | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            await websocket.send('{"type":"context","unexpected":true}')
            with pytest.raises(ConnectionClosed) as caught:
                await websocket.recv()
            return caught.value.rcvd.code if caught.value.rcvd is not None else None

    assert asyncio.run(scenario()) == 1008
    assert bridge.is_running


def test_new_valid_connection_replaces_old_connection(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> int | None:
        async with connect(_bridge_uri(bridge), proxy=None) as first:
            await _hello(first)
            async with connect(_bridge_uri(bridge), proxy=None) as second:
                await _hello(second)
                with pytest.raises(ConnectionClosed) as caught:
                    await first.recv()
                return caught.value.rcvd.code if caught.value.rcvd is not None else None

    assert asyncio.run(scenario()) == 1012


def test_web_page_origin_is_rejected_without_replacing_extension(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as extension:
            await _hello(extension)
            with pytest.raises(InvalidStatus):
                async with connect(
                    _bridge_uri(bridge),
                    origin="https://untrusted-page.test",
                    proxy=None,
                ):
                    pytest.fail("普通网页 Origin 不应完成 WebSocket 握手")

            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await extension.recv())
            await extension.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": request["request_id"],
                        "url": "https://trusted-context.test/",
                        "title": "仍由扩展响应",
                        "selection": "可信连接仍在",
                        "video_time": None,
                        "sensitive_input": False,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                )
            )
            return await request_task

    result = asyncio.run(scenario())
    assert result is not None
    assert result.selection == "可信连接仍在"


def test_chrome_extension_origin_is_allowed(bridge: BrowserBridge) -> None:
    async def scenario() -> None:
        extension_origin = "chrome-extension://" + ("a" * 32)
        async with connect(
            _bridge_uri(bridge), origin=extension_origin, proxy=None
        ) as websocket:
            await _hello(websocket)

    asyncio.run(scenario())


def test_non_ascii_context_within_utf8_budget_round_trips(
    bridge: BrowserBridge,
) -> None:
    selection = "观点" * 6_000

    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 0.5)
            )
            request = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "context",
                        "request_id": request["request_id"],
                        "url": "https://example.test/",
                        "title": "中文",
                        "selection": selection,
                        "video_time": None,
                        "sensitive_input": False,
                        "tab_id": 7,
                        "observation_text": "",
                        "observation_kind": "none",
                        "video_key": "",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return await request_task

    result = asyncio.run(scenario())
    assert result is not None
    assert result.selection == selection


def test_utf8_message_limit_is_enforced_in_bytes(bridge: BrowserBridge) -> None:
    async def scenario() -> int | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            await websocket.send("密" * 22_000)
            with pytest.raises(ConnectionClosed) as caught:
                await websocket.recv()
            return caught.value.rcvd.code if caught.value.rcvd is not None else None

    assert asyncio.run(scenario()) == 1009
    assert bridge.is_running


def test_concurrent_requests_can_resolve_out_of_order(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> list[BrowserContext | None]:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            tasks = [
                asyncio.create_task(
                    asyncio.to_thread(bridge.get_browser_context, 0.5)
                )
                for _ in range(2)
            ]
            requests = [json.loads(await websocket.recv()) for _ in range(2)]
            for index, request in reversed(list(enumerate(requests))):
                await websocket.send(
                    json.dumps(
                        {
                            "type": "context",
                            "request_id": request["request_id"],
                            "url": f"https://example.test/{index}",
                            "title": f"请求 {index}",
                            "selection": str(index),
                            "video_time": None,
                            "sensitive_input": False,
                            "tab_id": 7,
                            "observation_text": "",
                            "observation_kind": "none",
                            "video_key": "",
                        },
                        ensure_ascii=False,
                    )
                )
            return await asyncio.gather(*tasks)

    results = asyncio.run(scenario())
    assert {result.url for result in results if result is not None} == {
        "https://example.test/0",
        "https://example.test/1",
    }


def test_bridge_can_restart_cleanly() -> None:
    bridge = BrowserBridge(port=0)
    bridge.start()
    first_port = bridge.bound_port
    bridge.stop()
    bridge.start()
    try:
        assert bridge.is_running
        assert bridge.bound_port is not None
        assert first_port is not None
    finally:
        bridge.stop()


def test_stop_resolves_pending_request_and_waits_for_cleanup(
    bridge: BrowserBridge,
) -> None:
    async def scenario() -> BrowserContext | None:
        async with connect(_bridge_uri(bridge), proxy=None) as websocket:
            await _hello(websocket)
            request_task = asyncio.create_task(
                asyncio.to_thread(bridge.get_browser_context, 1.0)
            )
            request = json.loads(await websocket.recv())
            assert request["type"] == "get_context"
            await asyncio.to_thread(bridge.stop)
            return await request_task

    assert asyncio.run(scenario()) is None
    assert not bridge.is_running
