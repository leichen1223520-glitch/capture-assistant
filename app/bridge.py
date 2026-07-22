"""Chrome 扩展与桌面端之间的本机 WebSocket 桥。

桥只监听 IPv4 回环地址。网页与扩展提供的文字均作为不可信数据处理；本模块只做
有界的结构校验和传递，不记录正文，也不会在导入模块时启动网络服务。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import json
import math
import re
import threading
from typing import Any
from uuid import UUID, uuid4

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from app.config import WS_PORT


LOOPBACK_HOST = "127.0.0.1"
PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 64 * 1024

_HELLO_TIMEOUT_SECONDS = 3.0
_MAX_URL_LENGTH = 8_192
_MAX_TITLE_LENGTH = 2_048
_MAX_SELECTION_LENGTH = 32_768
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")
_CONTEXT_ERROR_CODES = frozenset(
    {
        "no_active_tab",
        "restricted_page",
        "content_script_unavailable",
        "internal_error",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserContext:
    """浏览器返回的结构化页面上下文。"""

    url: str
    title: str
    selection: str
    video_time: float | None
    sensitive_input: bool = False


class BrowserBridgeError(RuntimeError):
    """桥服务无法启动、停止或处于不合法生命周期时抛出的错误。"""


class _ProtocolError(ValueError):
    """对端消息不符合本地协议。"""


def _reject_json_constant(value: str) -> None:
    raise _ProtocolError(f"JSON 包含不受支持的常量：{value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("JSON 对象包含重复字段")
        result[key] = value
    return result


def _parse_message(raw: str | bytes) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise _ProtocolError("协议只接受文本消息")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise _ProtocolError("消息超过大小上限")

    try:
        message = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise _ProtocolError("消息不是合法 JSON") from exc

    if not isinstance(message, dict):
        raise _ProtocolError("消息根节点必须是对象")
    return message


def _require_exact_keys(message: dict[str, Any], expected: set[str]) -> None:
    if set(message) != expected:
        raise _ProtocolError("消息字段不符合协议")


def _require_uuid4(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise _ProtocolError("request_id 必须是 UUID4 字符串")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _ProtocolError("request_id 不是合法 UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise _ProtocolError("request_id 必须是规范格式的 UUID4")
    return value


def _require_bounded_string(value: Any, *, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise _ProtocolError(f"{field} 必须是长度受限的字符串")
    return value


def _validate_hello(message: dict[str, Any]) -> None:
    _require_exact_keys(message, {"type", "protocol"})
    if message["type"] != "hello" or type(message["protocol"]) is not int:
        raise _ProtocolError("hello 消息不合法")
    if message["protocol"] != PROTOCOL_VERSION:
        raise _ProtocolError("协议版本不兼容")


def _validate_context(message: dict[str, Any]) -> tuple[str, BrowserContext]:
    _require_exact_keys(
        message,
        {
            "type",
            "request_id",
            "url",
            "title",
            "selection",
            "video_time",
            "sensitive_input",
        },
    )
    if message["type"] != "context":
        raise _ProtocolError("上下文消息类型不合法")

    request_id = _require_uuid4(message["request_id"])
    url = _require_bounded_string(message["url"], maximum=_MAX_URL_LENGTH, field="url")
    title = _require_bounded_string(
        message["title"], maximum=_MAX_TITLE_LENGTH, field="title"
    )
    selection = _require_bounded_string(
        message["selection"], maximum=_MAX_SELECTION_LENGTH, field="selection"
    )
    sensitive_input = message["sensitive_input"]
    if type(sensitive_input) is not bool:
        raise _ProtocolError("sensitive_input 必须是布尔值")
    if sensitive_input and selection:
        raise _ProtocolError("敏感输入聚焦时 selection 必须为空")

    raw_video_time = message["video_time"]
    if raw_video_time is None:
        video_time = None
    elif (
        type(raw_video_time) in (int, float)
        and math.isfinite(raw_video_time)
        and raw_video_time >= 0
    ):
        video_time = float(raw_video_time)
    else:
        raise _ProtocolError("video_time 必须是非负有限数字或 null")

    return request_id, BrowserContext(
        url=url,
        title=title,
        selection=selection,
        video_time=video_time,
        sensitive_input=sensitive_input,
    )


def _validate_context_error(message: dict[str, Any]) -> str:
    _require_exact_keys(message, {"type", "request_id", "error"})
    if message["type"] != "context_error":
        raise _ProtocolError("错误消息类型不合法")
    request_id = _require_uuid4(message["request_id"])
    if (
        not isinstance(message["error"], str)
        or message["error"] not in _CONTEXT_ERROR_CODES
    ):
        raise _ProtocolError("错误代码不在允许列表中")
    return request_id


class BrowserBridge:
    """在后台事件循环中运行的同步生命周期 WebSocket 桥。

    ``start`` 和 ``stop`` 可重复调用。``get_browser_context`` 可以从普通桌面线程
    调用；无扩展连接、协议错误、断线或超时时均安全返回 ``None``。
    """

    def __init__(self, port: int = WS_PORT) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError("port 必须是 0 到 65535 的整数")
        self._requested_port = port
        self._bound_port: int | None = None
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Server | None = None
        self._client: ServerConnection | None = None
        self._pending: dict[str, asyncio.Future[BrowserContext | None]] = {}
        self._startup_error: BaseException | None = None

    @property
    def bound_port(self) -> int | None:
        """返回服务实际绑定端口；使用 ``port=0`` 测试时尤其有用。"""

        with self._state_lock:
            return self._bound_port

    @property
    def is_running(self) -> bool:
        """服务线程和事件循环均可用时返回 ``True``。"""

        with self._state_lock:
            return bool(
                self._thread
                and self._thread.is_alive()
                and self._loop
                and self._loop.is_running()
            )

    def start(self, timeout: float = 3.0) -> None:
        """启动仅监听 ``127.0.0.1`` 的桥服务。

        端口占用或启动超过 ``timeout`` 时抛出 ``BrowserBridgeError``。
        """

        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._startup_error = None
            self._bound_port = None
            thread = threading.Thread(
                target=self._run,
                name="capture-assistant-browser-bridge",
                daemon=True,
            )
            self._thread = thread
            thread.start()

        if not self._ready.wait(timeout):
            self.stop(timeout=timeout)
            raise BrowserBridgeError("WebSocket 桥启动超时")

        with self._state_lock:
            startup_error = self._startup_error
        if startup_error is not None:
            thread.join(timeout)
            raise BrowserBridgeError("WebSocket 桥启动失败") from startup_error

    def stop(self, timeout: float = 3.0) -> None:
        """停止服务、关闭扩展连接并清理所有等待中的请求。"""

        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        with self._state_lock:
            thread = self._thread
            loop = self._loop

        if thread is None or not thread.is_alive():
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
                    self._bound_port = None
            return
        if thread is threading.current_thread():
            raise BrowserBridgeError("不能从桥服务线程同步停止自身")

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        if thread.is_alive():
            raise BrowserBridgeError("WebSocket 桥未能在限定时间内停止")

        with self._state_lock:
            if self._thread is thread:
                self._thread = None
                self._bound_port = None

    def get_browser_context(self, timeout: float = 0.3) -> BrowserContext | None:
        """在有界时间内请求当前浏览器上下文，失败时返回 ``None``。"""

        if timeout <= 0:
            return None
        with self._state_lock:
            loop = self._loop
            thread = self._thread
        if (
            loop is None
            or thread is None
            or not thread.is_alive()
            or not loop.is_running()
        ):
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._request_browser_context(timeout), loop
            )
        except RuntimeError:
            return None

        try:
            return future.result(timeout=timeout + 0.05)
        except (FutureTimeoutError, FutureCancelledError):
            future.cancel()
            return None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop

        try:
            server = loop.run_until_complete(self._start_server())
            self._server = server
            sockets = server.sockets
            bound_port = int(sockets[0].getsockname()[1]) if sockets else None
            with self._state_lock:
                self._bound_port = bound_port
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            with self._state_lock:
                self._startup_error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._shutdown_async())
                remaining = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in remaining:
                    task.cancel()
                if remaining:
                    loop.run_until_complete(
                        asyncio.gather(*remaining, return_exceptions=True)
                    )
            finally:
                loop.close()
                with self._state_lock:
                    if self._loop is loop:
                        self._loop = None
                    self._server = None
                self._ready.set()

    async def _start_server(self) -> Server:
        """在已经运行的事件循环上下文中创建 websockets 服务。"""

        return await serve(
            self._handle_connection,
            LOOPBACK_HOST,
            self._requested_port,
            origins=[None, _EXTENSION_ORIGIN],
            compression=None,
            max_size=MAX_MESSAGE_BYTES,
            max_queue=16,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=0.5,
        )

    async def _shutdown_async(self) -> None:
        self._resolve_all_pending(None)
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close(code=1001, reason="desktop service stopped")
            except ConnectionClosed:
                pass
        server = self._server
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_connection(self, connection: ServerConnection) -> None:
        activated = False
        try:
            raw_hello = await asyncio.wait_for(
                connection.recv(), timeout=_HELLO_TIMEOUT_SECONDS
            )
            _validate_hello(_parse_message(raw_hello))
            await self._activate_client(connection)
            activated = True
            await connection.send(
                json.dumps(
                    {"type": "hello_ack", "protocol": PROTOCOL_VERSION},
                    separators=(",", ":"),
                )
            )

            async for raw_message in connection:
                await self._handle_client_message(connection, raw_message)
        except asyncio.TimeoutError:
            await connection.close(code=1008, reason="hello timeout")
        except _ProtocolError:
            await connection.close(code=1008, reason="invalid protocol message")
        except ConnectionClosed:
            pass
        finally:
            if activated:
                self._deactivate_client(connection)

    async def _activate_client(self, connection: ServerConnection) -> None:
        previous = self._client
        self._client = connection
        self._resolve_all_pending(None)
        if previous is not None and previous is not connection:
            try:
                await previous.close(code=1012, reason="new extension connection")
            except ConnectionClosed:
                pass

    def _deactivate_client(self, connection: ServerConnection) -> None:
        if self._client is connection:
            self._client = None
            self._resolve_all_pending(None)

    async def _handle_client_message(
        self, connection: ServerConnection, raw_message: str | bytes
    ) -> None:
        message = _parse_message(raw_message)
        message_type = message.get("type")

        if message_type == "ping":
            _require_exact_keys(message, {"type"})
            await connection.send('{"type":"pong"}')
            return
        if message_type == "context":
            request_id, context = _validate_context(message)
            self._resolve_pending(request_id, context)
            return
        if message_type == "context_error":
            request_id = _validate_context_error(message)
            self._resolve_pending(request_id, None)
            return
        raise _ProtocolError("未知消息类型")

    async def _request_browser_context(
        self, timeout: float
    ) -> BrowserContext | None:
        connection = self._client
        if connection is None:
            return None

        request_id = str(uuid4())
        pending: asyncio.Future[BrowserContext | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = pending
        request = json.dumps(
            {"type": "get_context", "request_id": request_id},
            separators=(",", ":"),
        )

        try:
            async with asyncio.timeout(timeout):
                await connection.send(request)
                return await pending
        except (TimeoutError, ConnectionClosed):
            return None
        finally:
            self._pending.pop(request_id, None)
            if not pending.done():
                pending.cancel()

    def _resolve_pending(
        self, request_id: str, result: BrowserContext | None
    ) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.done():
            pending.set_result(result)

    def _resolve_all_pending(self, result: BrowserContext | None) -> None:
        pending_requests = tuple(self._pending.values())
        self._pending.clear()
        for pending in pending_requests:
            if not pending.done():
                pending.set_result(result)


_default_bridge = BrowserBridge()


def start_browser_bridge(timeout: float = 3.0) -> None:
    """启动模块级默认桥，供桌面应用生命周期管理器调用。"""

    _default_bridge.start(timeout=timeout)


def stop_browser_bridge(timeout: float = 3.0) -> None:
    """停止模块级默认桥。"""

    _default_bridge.stop(timeout=timeout)


def get_browser_context(timeout: float = 0.3) -> BrowserContext | None:
    """从模块级默认桥获取上下文；未启动或失败时返回 ``None``。"""

    return _default_bridge.get_browser_context(timeout=timeout)


__all__ = [
    "BrowserBridge",
    "BrowserBridgeError",
    "BrowserContext",
    "get_browser_context",
    "start_browser_bridge",
    "stop_browser_bridge",
]
