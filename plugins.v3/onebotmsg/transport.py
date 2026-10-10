"""OneBot v11 动作通道抽象：HTTP 正向接口与正向 WebSocket 两种传输。

插件对上层只暴露一个 ``Transport``：无论底层是 HTTP 还是 WS，调用方式都是
``call(action, params)``。这样通知发送、状态查询、扩展 API 都不需要关心连接方式。

两种传输的能力差异（在 README 里也有对照表）：

======================  ==================  ==========================
能力                    HTTP 正向接口       正向 WebSocket
======================  ==================  ==========================
调用任意 OneBot 动作    支持                支持
连接开销                每次请求新建连接    复用一条长连接
``message_sent`` 回执   收不到              能收到（可确认真的发出）
======================  ==================  ==========================
"""

import threading
from typing import Any, Dict, List, Optional

from .onebot import OneBotClient, OneBotResponse
from .ws import OneBotWSClient, build_ws_url

# 传输类型标识
TRANSPORT_HTTP = "http"
TRANSPORT_WS = "ws"

# 展示用名称
TRANSPORT_LABELS = {
    TRANSPORT_HTTP: "HTTP 正向接口",
    TRANSPORT_WS: "正向 WebSocket",
}


def normalize_transport(value: Any) -> str:
    """把配置值规范化为受支持的传输标识。"""
    text = str(value or "").strip().lower()
    if text in ("ws", "websocket", "正向ws", "正向 websocket"):
        return TRANSPORT_WS
    return TRANSPORT_HTTP


class Transport:
    """统一的动作通道。

    ``call()`` 返回 ``OneBotResponse`` 或 ``None``：HTTP 通道永远返回结构体
    （失败时 ``ok=False`` 且带 ``error``），WS 通道在连接不可用时返回 ``None``。
    上层统一用 :meth:`call_ok` 判断，不必区分两种失败形态。
    """

    def __init__(self, kind: str, endpoint: str) -> None:
        """记录传输类型与地址，供状态展示与日志使用。"""
        self._kind = kind
        self._endpoint = endpoint

    @property
    def kind(self) -> str:
        """传输标识：``http`` 或 ``ws``。"""
        return self._kind

    @property
    def label(self) -> str:
        """传输展示名。"""
        return TRANSPORT_LABELS.get(self._kind, self._kind)

    @property
    def endpoint(self) -> str:
        """当前连接地址。"""
        return self._endpoint

    @property
    def connected(self) -> bool:
        """长连接是否已建立（HTTP 通道恒为 True）。"""
        return True

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败描述。"""
        return None

    def call(self, action: str, params: Optional[Dict[str, Any]] = None,
             **kwargs: Any) -> Optional[OneBotResponse]:
        """调用一个动作。子类必须实现。"""
        raise NotImplementedError

    def call_ok(self, action: str, params: Optional[Dict[str, Any]] = None,
                **kwargs: Any) -> bool:
        """调用动作并只关心是否成功。"""
        result = self.call(action, params, **kwargs)
        return bool(result is not None and result.ok)

    def call_data(self, action: str, params: Optional[Dict[str, Any]] = None,
                  **kwargs: Any) -> Optional[Any]:
        """调用动作并返回 ``data`` 字段，失败返回 None。"""
        result = self.call(action, params, **kwargs)
        if result is None or not result.ok:
            return None
        return result.data

    def close(self) -> None:
        """释放连接资源；HTTP 通道无需处理。"""
        return None

    def sent_receipts(self) -> List[Dict[str, Any]]:
        """取出「自己发出的消息」回执；HTTP 通道没有该能力，返回空列表。"""
        return []


class HttpTransport(Transport):
    """基于 OneBot 正向 HTTP 接口的传输。"""

    def __init__(self, base_url: str, access_token: str = "",
                 timeout: float = 12.0, retries: int = 2,
                 rate_limit: float = 3.0, rate_burst: float = 6.0,
                 dedupe_ttl: float = 30.0) -> None:
        """构造内部 HTTP 客户端；限流与去重仍由它承担。"""
        from .onebot import normalize_base_url

        super().__init__(TRANSPORT_HTTP, normalize_base_url(base_url))
        self._client = OneBotClient(
            base_url=base_url,
            access_token=access_token,
            timeout=timeout,
            retries=retries,
            rate_limit=rate_limit,
            rate_burst=rate_burst,
            dedupe_ttl=dedupe_ttl,
        )

    @property
    def client(self) -> OneBotClient:
        """返回底层客户端，供沿用原有 HTTP 便捷方法的调用方使用。"""
        return self._client

    @property
    def dedupe(self):
        """转发底层去重缓存，保持既有调用方式。"""
        return self._client.dedupe

    def call(self, action: str, params: Optional[Dict[str, Any]] = None,
             **kwargs: Any) -> Optional[OneBotResponse]:
        """通过 HTTP POST 调用动作。"""
        return self._client.call(action, params, **kwargs)

    def set_config(self, **kwargs: Any) -> None:
        """热更新底层客户端配置。"""
        self._client.set_config(**kwargs)

    def clear_dedupe(self) -> None:
        """清空去重缓存。"""
        self._client.clear_dedupe()


class WsTransport(Transport):
    """基于 OneBot v11 正向 WebSocket 的传输。"""

    def __init__(self, url: str, access_token: str = "",
                 timeout: float = 15.0,
                 on_event: Optional[Any] = None) -> None:
        """构造内部 WS 客户端；事件回调用于接收 ``message_sent`` 回执。"""
        super().__init__(TRANSPORT_WS, build_ws_url(url))
        self._client = OneBotWSClient(
            url=url, access_token=access_token, timeout=timeout,
            on_event=on_event,
        )

    @property
    def ws(self) -> OneBotWSClient:
        """返回底层 WS 客户端。"""
        return self._client

    @property
    def connected(self) -> bool:
        """长连接是否已建立。"""
        return self._client.connected

    @property
    def last_error(self) -> Optional[str]:
        """最近一次连接/读写错误。"""
        return self._client.last_error

    def call(self, action: str, params: Optional[Dict[str, Any]] = None,
             **kwargs: Any) -> Optional[OneBotResponse]:
        """通过长连接调用动作，并把响应转换为统一的 ``OneBotResponse``。"""
        timeout = kwargs.get("timeout")
        result = self._client.call(action, params or {}, timeout=timeout)
        if result is None:
            return OneBotResponse(action, False, error=self._client.last_error or "无响应")
        status = result.get("status")
        retcode = result.get("retcode")
        ok = status == "ok" and (retcode in (0, None))
        message = result.get("message") or result.get("msg")
        return OneBotResponse(
            action,
            ok,
            retcode=retcode if isinstance(retcode, int) else None,
            status=status,
            message=message,
            data=result.get("data"),
            error=None if ok else f"retcode={retcode} message={message}",
        )

    def connect(self) -> bool:
        """主动建立长连接（便于测试连通性时提前握手）。"""
        return self._client.connect()

    def sent_receipts(self) -> List[Dict[str, Any]]:
        """取出累计的 ``message_sent`` 回执。"""
        return self._client.sent_receipts()

    def close(self) -> None:
        """发送关闭帧并释放连接。"""
        self._client.close()


def build_transport(kind: str, url: str, access_token: str = "",
                    timeout: float = 12.0, retries: int = 2,
                    rate_limit: float = 3.0, rate_burst: float = 6.0,
                    dedupe_ttl: float = 30.0,
                    on_event: Optional[Any] = None) -> Transport:
    """按配置构造传输对象。"""
    if normalize_transport(kind) == TRANSPORT_WS:
        return WsTransport(url=url, access_token=access_token,
                           timeout=max(timeout, 5.0), on_event=on_event)
    return HttpTransport(
        base_url=url, access_token=access_token, timeout=timeout,
        retries=retries, rate_limit=rate_limit, rate_burst=rate_burst,
        dedupe_ttl=dedupe_ttl,
    )


class RateLimiter:
    """与底层客户端共享的简单令牌桶，供上层在发送前统一限流。"""

    def __init__(self, rate: float, capacity: float) -> None:
        """初始化令牌桶。"""
        from .onebot import TokenBucket

        self._bucket = TokenBucket(rate, capacity)

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取一个令牌。"""
        return self._bucket.acquire(timeout=timeout)

    def set_rate(self, rate: float, capacity: Optional[float] = None) -> None:
        """调整速率。"""
        self._bucket.set_rate(rate, capacity)


def describe_transport(transport: Optional[Transport]) -> str:
    """生成用于详情页/日志的一行传输描述。"""
    if transport is None:
        return "未初始化"
    state = "已连接" if transport.connected else "未连接"
    detail = f"{transport.label} · {transport.endpoint or '未配置'}"
    if transport.kind == TRANSPORT_WS:
        error = transport.last_error
        return f"{detail} · {state}" + (f"（{error}）" if error and not transport.connected else "")
    return detail


# 供测试与诊断使用的互斥锁工厂（保持模块自洽，避免上层再引 threading）
def new_lock() -> threading.Lock:
    """返回一个新的互斥锁。"""
    return threading.Lock()
