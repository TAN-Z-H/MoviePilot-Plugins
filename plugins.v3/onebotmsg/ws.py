"""OneBot v11 正向 WebSocket 传输（纯标准库实现）。

NapCat 在「网络配置」里可开启 **WebSocket 服务器**（正向 WS），此时插件作为客户端
主动连上去，用同一条长连接完成：动作调用 + 事件接收。

相对 HTTP 正向接口，本模块带来两点实际收益：

1. **往返更快**：省去每次动作的 TCP/TLS 建连，通知密集时明显；
2. **可确认投递**：NapCat 会在这条连接上推送 ``post_type=message_sent``
   （自己发出的消息），据此可以确认「消息真的发出去了」，而不只是「接口返回了 ok」——
   这对排查图片/消息发不出去这类问题很关键（见 ``sent_receipts``）。

协议要点（RFC 6455）：

* 握手：``GET <path> HTTP/1.1`` + ``Upgrade: websocket``，
  服务端返回 ``Sec-WebSocket-Accept = base64(sha1(key + GUID))``；
* 客户端发出的帧必须掩码（mask），服务端帧不加掩码；
* 支持分片续帧、ping/pong、close。

线程模型：一个读线程负责收帧并把响应按 ``echo`` 派发、把事件交给回调；
调用方线程通过 ``queue.Queue`` 等待自己的响应。读写各自加锁，互不阻塞。
"""

import base64
import hashlib
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

# WebSocket 握手固定 GUID（RFC 6455）
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 帧操作码
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# 单帧允许的最大载荷（防御异常对端）
_MAX_FRAME_PAYLOAD = 32 * 1024 * 1024

# 单条消息的默认等待上限（秒）
DEFAULT_TIMEOUT = 15.0


class WebSocketError(RuntimeError):
    """WebSocket 连接或协议层错误。"""


def build_ws_url(base_url: str) -> str:
    """把用户填写的 HTTP 地址转换成正向 WS 地址。

    允许用户直接粘贴 NapCat「WebSocket 服务器」里的地址，也允许沿用 HTTP 地址：
    ``http://host:3000`` → ``ws://host:3000``；
    ``https://host`` → ``wss://host``。
    """
    text = str(base_url or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("ws://") or lowered.startswith("wss://"):
        return text.rstrip("/")
    if lowered.startswith("https://"):
        return "wss://" + text[len("https://"):].rstrip("/")
    if lowered.startswith("http://"):
        return "ws://" + text[len("http://"):].rstrip("/")
    return "ws://" + text.rstrip("/")


def _parse_ws_url(url: str) -> Tuple[str, int, str, bool]:
    """解析 WS 地址为 ``(host, port, path, use_tls)``。"""
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "ws").lower()
    use_tls = scheme == "wss"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if use_tls else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path, use_tls


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """读满 ``size`` 字节；连接中断时抛 ``WebSocketError``。"""
    chunks = bytearray()
    while len(chunks) < size:
        piece = sock.recv(size - len(chunks))
        if not piece:
            raise WebSocketError("连接已被对端关闭")
        chunks.extend(piece)
    return bytes(chunks)


def encode_frame(payload: bytes, opcode: int = _OP_TEXT,
                 mask: bool = True) -> bytes:
    """按 RFC 6455 编码一个数据帧。"""
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN + opcode
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not mask:
        return bytes(header) + payload
    mask_key = os.urandom(4)
    header.extend(mask_key)
    masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


def read_frame(sock: socket.socket) -> Tuple[int, bytes]:
    """从连接读取一个完整帧，返回 ``(opcode, payload)``。

    控制帧与数据帧都原样返回；调用方负责处理 ping/pong/close 与分片续帧。
    """
    first, second = _recv_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > _MAX_FRAME_PAYLOAD:
        raise WebSocketError(f"帧载荷过大：{length} 字节")
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked and payload:
        payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    if not fin:
        # 分片续帧：把后续帧拼起来，统一按首帧的操作码返回。
        while True:
            next_opcode, next_payload = read_frame(sock)
            if next_opcode == _OP_CONT:
                payload += next_payload
                break
            if next_opcode in (_OP_PING, _OP_PONG, _OP_CLOSE):
                # 控制帧允许插入分片之间，这里不做处理直接丢弃，交由上层心跳兜底。
                continue
            raise WebSocketError("分片序列异常")
    return opcode, payload


class OneBotWSClient:
    """OneBot v11 正向 WebSocket 客户端。

    懒连接：首次调用动作或启动事件监听时才建连；断线后由下一次调用触发重连。
    """

    def __init__(self, url: str, access_token: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        """建立客户端；``url`` 支持 http/ws 两种写法。"""
        self._url = build_ws_url(url)
        self._access_token = str(access_token or "").strip()
        self._timeout = max(float(timeout), 2.0)
        self._on_event = on_event
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        # 最近收到的 message_sent 回执：{seq: {"message_id":..., "time":...}}
        self._sent_receipts: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=64)
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ 状态

    @property
    def url(self) -> str:
        """返回规范化后的 WS 地址。"""
        return self._url

    @property
    def connected(self) -> bool:
        """是否处于已连接状态。"""
        return self._sock is not None and not self._closed

    @property
    def last_error(self) -> Optional[str]:
        """最近一次连接或读写错误描述。"""
        return self._last_error

    def sent_receipts(self) -> List[Dict[str, Any]]:
        """取出并清空累计的「自己发出的消息」回执。"""
        items: List[Dict[str, Any]] = []
        while True:
            try:
                items.append(self._sent_receipts.get_nowait())
            except queue.Empty:
                break
        return items

    def set_event_handler(self, handler: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """替换事件回调（收到非响应消息时调用）。"""
        self._on_event = handler

    def mirror(self, other: "OneBotWSClient") -> "OneBotWSClient":
        """把运行期状态迁移到按新配置构造的实例上。

        插件保存配置时会重建客户端对象；已经用旧配置连上的会话不应因为一次保存
        就断掉，因此把连接与读线程交接给新实例。
        """
        with self._state_lock:
            other._sock = self._sock
            other._reader = self._reader
            other._pending = self._pending
            other._sent_receipts = self._sent_receipts
            other._on_event = other._on_event or self._on_event
            self._sock = None
            self._reader = None
            self._pending = {}
        return other

    # ------------------------------------------------------------------ 连接

    def connect(self) -> bool:
        """建立连接并启动读线程；失败时返回 False 并记录原因。"""
        if self.connected:
            return True
        if not self._url:
            self._last_error = "未配置 WebSocket 地址"
            return False
        host, port, path, use_tls = _parse_ws_url(self._url)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            raw = socket.create_connection((host, port), timeout=self._timeout)
            raw.settimeout(self._timeout)
            if use_tls:
                context = ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=host)
            request_lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if self._access_token:
                request_lines.append(f"Authorization: Bearer {self._access_token}")
            handshake = ("\r\n".join(request_lines) + "\r\n\r\n").encode("utf-8")
            raw.sendall(handshake)
            header_bytes = self._read_http_header(raw)
            status_line, headers = self._parse_http_header(header_bytes)
            if "101" not in status_line:
                raw.close()
                self._last_error = f"握手被拒绝：{status_line.strip()}"
                return False
            expected = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if headers.get("sec-websocket-accept", "") != expected:
                raw.close()
                self._last_error = "Sec-WebSocket-Accept 校验失败"
                return False
        except (OSError, WebSocketError, ssl.SSLError) as err:
            self._last_error = f"{type(err).__name__}: {err}"
            return False

        with self._state_lock:
            self._sock = raw
            self._closed = False
            self._reader = threading.Thread(
                target=self._reader_loop, name="OneBotWSReader", daemon=True
            )
        self._reader.start()
        return True

    @staticmethod
    def _read_http_header(sock: socket.socket, limit: int = 64 * 1024) -> bytes:
        """读到 HTTP 头结束（``\\r\\n\\r\\n``）为止。"""
        buffered = bytearray()
        while b"\r\n\r\n" not in buffered:
            if len(buffered) > limit:
                raise WebSocketError("握手响应头过长")
            piece = sock.recv(4096)
            if not piece:
                raise WebSocketError("握手期间连接被关闭")
            buffered.extend(piece)
        return bytes(buffered)

    @staticmethod
    def _parse_http_header(raw: bytes) -> Tuple[str, Dict[str, str]]:
        """解析握手响应，返回 ``(状态行, 小写头字典)``。"""
        text = raw.decode("latin-1")
        head, _, _rest = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status_line = lines[0] if lines else ""
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return status_line, headers

    def close(self) -> None:
        """发送关闭帧并释放连接与等待中的调用。"""
        with self._state_lock:
            self._closed = True
            sock = self._sock
            self._sock = None
            pending = list(self._pending.values())
            self._pending.clear()
        if sock is not None:
            try:
                sock.sendall(encode_frame(b"", _OP_CLOSE))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for waiter in pending:
            try:
                waiter.put_nowait({"status": "failed", "retcode": -1,
                                   "message": "连接已关闭"})
            except queue.Full:  # pragma: no cover - 队列不会满
                pass

    # ------------------------------------------------------------------ 收发

    def call(self, action: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """调用一个动作并等待响应。

        :return: 完整响应字典（含 ``status``/``retcode``/``data``），失败返回 None
        """
        if not self.connect():
            return None
        echo = uuid.uuid4().hex
        waiter: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
        with self._state_lock:
            self._pending[echo] = waiter
        payload = json.dumps(
            {"action": action, "params": params or {}, "echo": echo},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            with self._send_lock:
                sock = self._sock
                if sock is None:
                    raise WebSocketError("连接不可用")
                sock.sendall(encode_frame(payload, _OP_TEXT))
        except (OSError, WebSocketError) as err:
            self._last_error = f"发送失败：{type(err).__name__}: {err}"
            with self._state_lock:
                self._pending.pop(echo, None)
            return None
        try:
            return waiter.get(timeout=timeout or self._timeout)
        except queue.Empty:
            self._last_error = f"等待 {action} 响应超时"
            return None
        finally:
            with self._state_lock:
                self._pending.pop(echo, None)

    def _reader_loop(self) -> None:
        """持续读取帧；响应按 echo 派发，其余作为事件回调。"""
        while True:
            with self._state_lock:
                sock = self._sock
                if sock is None or self._closed:
                    return
            try:
                opcode, payload = read_frame(sock)
            except (OSError, WebSocketError) as err:
                self._last_error = f"读取失败：{type(err).__name__}: {err}"
                self._fail_pending("连接中断")
                with self._state_lock:
                    self._sock = None
                return
            if opcode == _OP_CLOSE:
                self._fail_pending("对端关闭了连接")
                with self._state_lock:
                    self._sock = None
                return
            if opcode == _OP_PING:
                self._send_control(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode not in (_OP_TEXT, _OP_BINARY):
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            echo = message.get("echo")
            if echo:
                with self._state_lock:
                    waiter = self._pending.get(echo)
                if waiter is not None:
                    try:
                        waiter.put_nowait(message)
                    except queue.Full:  # pragma: no cover
                        pass
                    continue
            if message.get("post_type") == "message_sent":
                self._remember_receipt(message)
            handler = self._on_event
            if handler is not None:
                try:
                    handler(message)
                except Exception:  # noqa: BLE001 - 回调异常不应打断读线程
                    continue

    def _remember_receipt(self, message: Dict[str, Any]) -> None:
        """记录一条 message_sent 回执。"""
        try:
            self._sent_receipts.put_nowait({
                "message_id": message.get("message_id"),
                "message_type": message.get("message_type"),
                "user_id": message.get("user_id"),
                "group_id": message.get("group_id"),
                "time": message.get("time"),
            })
        except queue.Full:
            try:
                self._sent_receipts.get_nowait()
                self._sent_receipts.put_nowait({
                    "message_id": message.get("message_id"),
                    "time": message.get("time"),
                })
            except queue.Empty:  # pragma: no cover
                pass

    def _send_control(self, opcode: int, payload: bytes) -> None:
        """发送控制帧（pong/close），失败时忽略。"""
        try:
            with self._send_lock:
                sock = self._sock
                if sock is not None:
                    sock.sendall(encode_frame(payload, opcode))
        except (OSError, WebSocketError):
            pass

    def _fail_pending(self, reason: str) -> None:
        """让所有等待中的调用立即失败，避免调用方干等到超时。"""
        with self._state_lock:
            pending = list(self._pending.values())
        for waiter in pending:
            try:
                waiter.put_nowait({"status": "failed", "retcode": -1,
                                   "message": reason})
            except queue.Full:  # pragma: no cover
                pass


class LoopbackWSServer:
    """测试用的最小 WebSocket 服务端（仅本机回环，线程安全）。

    存在的意义是让 ``ws.py`` 的握手、掩码、分片、echo 派发等逻辑能被真实地
    端到端验证，而不是只靠 mock。
    """

    def __init__(self, handler: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
                 token: str = ""):
        """``handler`` 收到请求字典后返回响应字典（None 表示不响应）。"""
        self._handler = handler
        self._token = token
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.port = 0
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def start(self) -> "LoopbackWSServer":
        """在随机空闲端口启动服务。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.5)
        self._server = server
        self.port = server.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="LoopbackWSServer", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止服务并关闭监听套接字。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    @property
    def url(self) -> str:
        """返回 ``ws://127.0.0.1:<port>/`` 形式的地址。"""
        return f"ws://127.0.0.1:{self.port}/"

    def _accept_loop(self) -> None:
        """接受连接并逐个处理。"""
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                conn, _addr = server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._serve(conn)
            except Exception:  # noqa: BLE001 - 测试服务端不应因单连接异常退出
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve(self, conn: socket.socket) -> None:
        """完成握手并循环处理请求帧。"""
        conn.settimeout(5.0)
        header = OneBotWSClient._read_http_header(conn)
        text = header.decode("latin-1")
        head, _, rest = text.partition("\r\n\r\n")
        headers: Dict[str, str] = {}
        lines = head.split("\r\n")
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        if self._token:
            auth = headers.get("authorization", "")
            if auth != f"Bearer {self._token}":
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                return
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii"))
        while not self._stop.is_set():
            try:
                opcode, payload = read_frame(conn)
            except (OSError, WebSocketError):
                return
            if opcode == _OP_CLOSE:
                return
            if opcode == _OP_PING:
                conn.sendall(encode_frame(payload, _OP_PONG, mask=False))
                continue
            if opcode not in (_OP_TEXT, _OP_BINARY):
                continue
            try:
                request = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            with self._lock:
                self.requests.append(request)
            response = self._handler(request) if isinstance(request, dict) else None
            if response is None:
                continue
            echo = request.get("echo")
            if echo:
                response = dict(response)
                response["echo"] = echo
            conn.sendall(encode_frame(
                json.dumps(response, ensure_ascii=False).encode("utf-8"),
                _OP_TEXT, mask=False,
            ))

    def push(self, event: Dict[str, Any]) -> None:
        """占位方法：本测试服务端不支持主动推送事件。"""
        raise NotImplementedError("LoopbackWSServer 仅用于验证请求-响应路径")


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0,
             interval: float = 0.02) -> bool:
    """轮询等待条件成立，主要用于测试同步点。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
