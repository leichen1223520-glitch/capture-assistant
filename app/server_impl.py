"""只监听本机回环地址的只读检索服务。

模块导入不会启动服务、初始化数据库或写入文件。所有 HTTP 路由均为 GET，
查询词不会进入访问日志，屏幕资料只作为转义后的数据展示。
"""

from __future__ import annotations

import html
import stat
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import API_PORT
from .exporting import cards_to_json, cards_to_markdown
from .models import Card
from .store import Store, StoreError

LOCAL_API_HOST = "127.0.0.1"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 64 * 1_024 * 1_024
_MAX_EXPORT_CARDS = 2_000
_STANCE_LABELS = {
    "unknown": "未标记",
    "agree": "认同",
    "disagree": "反对",
    "doubt": "存疑",
    "useful": "只是有用",
}


def _card_payload(card: Card) -> dict[str, object]:
    """把 Card 转成稳定、普通的 JSON 数据对象。"""

    return card.model_dump(mode="json")


def _load_cards(store: Store, query: str, limit: int) -> list[Card]:
    normalized = " ".join(query.split())
    return store.search(normalized, limit) if normalized else store.list_recent(limit)


def _load_all_cards(store: Store) -> list[Card]:
    """用单条查询取得有上限的导出快照，避免跨页期间发生结果漂移。"""

    cards = store.list_saved_snapshot(_MAX_EXPORT_CARDS + 1)
    if len(cards) > _MAX_EXPORT_CARDS:
        raise HTTPException(
            status_code=413,
            detail="卡片数量超过单次导出上限，请先使用搜索词分批导出。",
        )
    return cards


def _safe_external_url(value: str | None) -> str | None:
    """仅返回可放入 HTML 属性的 HTTP(S) 绝对 URL。"""

    if not value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return None
        return quote(value, safe=":/?#[]@!$&'()*+,;=%~.-_")
    except ValueError:
        return None


def _resolve_screenshot_path(store: Store, relative_path: str) -> Path:
    """解析截图路径并强制其最终目标严格位于截图根目录内。"""

    try:
        data_root = store.data_dir.resolve()
        screenshot_root = store.screenshot_dir.resolve()
        candidate = (data_root / relative_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="截图不存在。") from exc

    if (
        screenshot_root == data_root
        or data_root not in screenshot_root.parents
        or candidate == screenshot_root
        or screenshot_root not in candidate.parents
        or candidate.suffix.casefold() != ".png"
    ):
        raise HTTPException(status_code=404, detail="截图不存在。")
    return candidate


def _read_png(store: Store, relative_path: str) -> bytes:
    """在边界验证后一次性读取 PNG，避免响应阶段再次跟随被替换的路径。"""

    candidate = _resolve_screenshot_path(store, relative_path)
    try:
        metadata = candidate.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SCREENSHOT_BYTES:
            raise OSError("截图不是受控大小的普通文件")
        with candidate.open("rb") as stream:
            content = stream.read(_MAX_SCREENSHOT_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="截图不存在。") from exc
    if len(content) > _MAX_SCREENSHOT_BYTES or not content.startswith(_PNG_SIGNATURE):
        raise HTTPException(status_code=404, detail="截图不存在。")
    return content


def _render_source(card: Card) -> str:
    title = html.escape(card.source_title or "未知来源")
    if not card.source_url:
        return f'<span class="source">{title}</span>'
    display_url = html.escape(card.source_url)
    safe_url = _safe_external_url(card.source_url)
    if safe_url is None:
        return (
            f'<span class="source">{title}</span>'
            f'<span class="url">{display_url}</span>'
        )
    return (
        f'<a class="source" href="{html.escape(safe_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">{title}</a>'
        f'<span class="url">{display_url}</span>'
    )


def _render_card(card: Card) -> str:
    original = html.escape(card.text)
    edited = (
        ""
        if card.edited_text is None
        else (
            '<div class="label">人工整理</div>'
            f'<div class="edited">{html.escape(card.edited_text)}</div>'
        )
    )
    note = (
        ""
        if not card.note
        else f'<div class="note">备注：{html.escape(card.note)}</div>'
    )
    return f"""
    <article class="card">
      <a class="thumb" href="/screenshot/{card.id}?kind=full" target="_blank"
         rel="noopener">
        <img src="/screenshot/{card.id}" alt="卡片截图" loading="lazy">
      </a>
      <div class="body">
        <div class="meta">
          <span class="stance">{_STANCE_LABELS[card.stance]}</span>
          <time>{html.escape(card.created_at)}</time>
        </div>
        <div class="label">提取原文</div>
        <div class="original">{original}</div>
        {edited}
        {note}
        <div class="source-row">{_render_source(card)}</div>
        <a class="json-link" href="/cards/{card.id}">查看 JSON</a>
      </div>
    </article>
    """


def _render_search_page(cards: list[Card], query: str) -> str:
    safe_query = html.escape(query, quote=True)
    encoded_query = quote(query, safe="")
    export_query = f"&amp;q={encoded_query}" if encoded_query else ""
    results = "".join(_render_card(card) for card in cards)
    if not results:
        results = '<p class="empty">没有找到匹配的观点卡片。</p>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本地观点库</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, "Microsoft YaHei", sans-serif; }}
    body {{ margin: 0 auto; max-width: 980px; padding: 28px 18px 60px; color: #18212f; background: #f5f7fa; }}
    h1 {{ margin: 0 0 18px; font-size: 28px; }}
    form {{ display: flex; gap: 10px; margin-bottom: 20px; }}
    input {{ flex: 1; min-width: 0; padding: 11px 13px; border: 1px solid #aeb8c6; border-radius: 8px; font-size: 16px; }}
    button {{ padding: 10px 20px; border: 0; border-radius: 8px; color: white; background: #225bd6; font-size: 16px; cursor: pointer; }}
    .links {{ display: flex; gap: 14px; margin: -8px 0 20px; font-size: 14px; }}
    .card {{ display: grid; grid-template-columns: 180px 1fr; gap: 18px; margin: 14px 0; padding: 16px; border: 1px solid #dbe1e9; border-radius: 12px; background: white; }}
    .thumb img {{ width: 180px; height: 130px; object-fit: contain; border-radius: 7px; background: #eef1f5; }}
    .meta {{ display: flex; justify-content: space-between; gap: 12px; color: #667085; font-size: 13px; }}
    .stance {{ padding: 3px 8px; border-radius: 99px; color: #1849a9; background: #eaf1ff; }}
    .label {{ margin-top: 10px; color: #667085; font-size: 12px; }}
    .original, .edited, .note {{ margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .edited {{ color: #344054; }}
    .note {{ margin-top: 10px; color: #475467; }}
    .source-row {{ display: flex; flex-direction: column; gap: 2px; margin-top: 12px; overflow-wrap: anywhere; }}
    .source, .json-link {{ color: #1849a9; }}
    .url {{ color: #667085; font-size: 12px; }}
    .json-link {{ display: inline-block; margin-top: 8px; font-size: 13px; }}
    .empty {{ padding: 35px; text-align: center; color: #667085; background: white; border-radius: 12px; }}
    @media (max-width: 620px) {{ .card {{ grid-template-columns: 1fr; }} .thumb img {{ width: 100%; height: 190px; }} }}
  </style>
</head>
<body>
  <h1>本地观点库</h1>
  <form action="/" method="get">
    <input name="q" maxlength="500" value="{safe_query}" placeholder="搜索原文、整理文字、标题或备注" autofocus>
    <button type="submit">搜索</button>
  </form>
  <nav class="links">
    <a href="/export?format=json{export_query}">导出 JSON</a>
    <a href="/export?format=md{export_query}">导出 Markdown</a>
  </nav>
  <main>{results}</main>
</body>
</html>
"""


def create_app(store: Store | None = None) -> FastAPI:
    """创建可注入 Store 的只读 FastAPI 应用，不初始化或修改数据库。"""

    selected_store = store or Store()
    app = FastAPI(
        title="本地观点库只读接口",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = selected_store
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        fetch_site = request.headers.get("sec-fetch-site", "").casefold()
        origin = request.headers.get("origin")
        origin_allowed = True
        if origin:
            try:
                parsed_origin = urlsplit(origin)
                origin_allowed = (
                    parsed_origin.scheme.casefold() == "http"
                    and parsed_origin.hostname in {"127.0.0.1", "localhost"}
                )
            except ValueError:
                origin_allowed = False
        if fetch_site == "cross-site" or not origin_allowed:
            response = JSONResponse(
                status_code=403,
                content={"detail": "只允许本机同源读取。"},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(StoreError)
    async def store_error_handler(request: Request, exc: StoreError) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=503,
            content={"detail": "本地观点库暂时无法读取。"},
        )

    @app.get("/", response_class=HTMLResponse)
    def search_page(
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> HTMLResponse:
        cards = _load_cards(selected_store, q, limit)
        return HTMLResponse(_render_search_page(cards, q))

    @app.get("/search")
    def search_cards(
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, object]:
        cards = _load_cards(selected_store, q, limit)
        return {
            "query": q,
            "count": len(cards),
            "items": [_card_payload(card) for card in cards],
        }

    @app.get("/cards/{card_id}")
    def get_card(card_id: UUID) -> dict[str, object]:
        card = selected_store.get_card(str(card_id))
        if card is None:
            raise HTTPException(status_code=404, detail="卡片不存在。")
        return _card_payload(card)

    @app.get("/screenshot/{card_id}")
    def get_screenshot(
        card_id: UUID,
        kind: Literal["selected", "full"] = "selected",
    ) -> Response:
        card = selected_store.get_card(str(card_id))
        if card is None:
            raise HTTPException(status_code=404, detail="截图不存在。")
        relative_path = (
            card.full_screenshot_path if kind == "full" else card.screenshot_path
        )
        return Response(
            content=_read_png(selected_store, relative_path),
            media_type="image/png",
        )

    @app.get("/export")
    def export_cards(
        export_format: Literal["json", "md"] = Query(default="json", alias="format"),
        q: str = Query(default="", max_length=500),
    ) -> Response:
        cards = (
            selected_store.search(" ".join(q.split()), limit=500)
            if q.strip()
            else _load_all_cards(selected_store)
        )
        if export_format == "md":
            return Response(
                content=cards_to_markdown(cards),
                media_type="text/markdown",
                headers={
                    "Content-Disposition": (
                        'attachment; filename="capture-assistant-cards.md"'
                    )
                },
            )
        return Response(
            content=cards_to_json(cards),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    'attachment; filename="capture-assistant-cards.json"'
                )
            },
        )

    return app


class LocalApiServer:
    """在后台线程运行仅回环监听的 uvicorn 服务。"""

    def __init__(
        self,
        store: Store | None = None,
        *,
        host: str = LOCAL_API_HOST,
        port: int = API_PORT,
    ) -> None:
        if host != LOCAL_API_HOST:
            raise ValueError("只读检索服务只允许监听 127.0.0.1。")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError("只读检索服务端口必须在 1–65535 之间。")
        self.store = store or Store()
        self.host = host
        self.port = port
        self.app = create_app(self.store)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        """服务线程存在且 uvicorn 已完成启动时返回 True。"""

        thread = self._thread
        server = self._server
        return bool(thread and thread.is_alive() and server and server.started)

    def start(self, timeout: float = 5.0) -> "LocalApiServer":
        """启动后台服务并等待就绪；端口占用或超时会抛出 RuntimeError。"""

        if timeout <= 0:
            raise ValueError("启动等待时间必须大于 0。")
        with self._lock:
            if self.running:
                return self
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("只读检索服务正处于未完成的启动状态。")

            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                access_log=False,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run,
                name="capture-assistant-readonly-api",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()

            deadline = time.monotonic() + timeout
            while not server.started:
                if not thread.is_alive():
                    self._server = None
                    self._thread = None
                    raise RuntimeError("只读检索服务启动失败，端口可能已被占用。")
                if time.monotonic() >= deadline:
                    server.should_exit = True
                    thread.join(timeout=1.0)
                    if thread.is_alive():
                        server.force_exit = True
                        thread.join(timeout=1.0)
                    if thread.is_alive():
                        raise RuntimeError("只读检索服务启动超时且后台线程仍在退出。")
                    self._server = None
                    self._thread = None
                    raise RuntimeError("等待只读检索服务启动超时。")
                time.sleep(0.01)
            return self

    def stop(self, timeout: float = 5.0) -> None:
        """请求 uvicorn 停止并等待线程退出；可重复调用。"""

        if timeout <= 0:
            raise ValueError("停止等待时间必须大于 0。")
        with self._lock:
            server = self._server
            thread = self._thread
            if server is None or thread is None:
                return
            server.should_exit = True
            thread.join(timeout=timeout)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=1.0)
            if thread.is_alive():
                raise RuntimeError("只读检索服务未能在限定时间内停止。")
            self._server = None
            self._thread = None


def start_readonly_server(
    store: Store | None = None,
    *,
    host: str = LOCAL_API_HOST,
    port: int = API_PORT,
    timeout: float = 5.0,
) -> LocalApiServer:
    """构造并启动后台只读服务，返回供调用方关闭的生命周期句柄。"""

    return LocalApiServer(store, host=host, port=port).start(timeout=timeout)
