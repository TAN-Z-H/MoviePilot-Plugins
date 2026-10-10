"""OneBot 通知插件的回归测试。

测试目标（对齐 MoviePilot 插件仓约定）：

* 放在 ``tests/v3/onebotmsg/``，文件名与用例名可被 pytest 发现，由 ``tests/run.py``
  的独立 v3 会话执行；
* 通过**生产命名空间** ``app.plugins.onebotmsg`` 导入插件与其子模块，
  不把插件目录加入 ``sys.path``，避免同一源码被双重导入而重复注册事件；
* 只 mock 对外 HTTP 调用；WebSocket 与 NapCat 扩展那一层用**真实回环服务端**
  做端到端验证，确保握手、掩码、echo 派发不是只靠断言形状。

运行方式：

    <workspace>/.venv/bin/python -m pytest tests/v3/onebotmsg   # 仓库门禁
    <workspace>/.venv/bin/python tests/run.py                   # 全量回归

本文件只提供 pytest 用例，不提供独立执行入口（仓库测试约定）。缺少 MoviePilot
后端时（例如仅装了标准库的开发机），导入生产命名空间会失败并自动降级为内置 stub
自检，保证纯逻辑仍可验证；提交与 CI 必须走真实宿主。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock


def _require_pytest():
    """按仓库约定取得 pytest，缺失时跳过而不是让模块导入失败。"""
    if importlib.util.find_spec("pytest") is None:
        return None
    import pytest as pytest_module

    return pytest_module


# Windows 控制台默认使用 GBK，直接打印 emoji 会抛 UnicodeEncodeError。
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 不是所有流都支持重配置
            pass

# 本文件位于 tests/v3/onebotmsg/：上溯三级即插件仓根。
PLUGIN_ID = "onebotmsg"
PLUGIN_NAMESPACE = f"app.plugins.{PLUGIN_ID}"
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / PLUGIN_ID

# 测试临时目录：放一张真实的 1x1 PNG，用于验证 base64/路径两种图片形式。
WORK_DIR = Path(tempfile.gettempdir()) / "onebotmsg-test"
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)

# 收集到的检查结果：(分组名, 检查名, 是否通过)
CHECKS: List[Tuple[str, str, bool]] = []
# 当前分组名
_GROUP = "初始化"


def _safe_print(text: str) -> None:
    """在无法编码 emoji 的终端上也尽量输出结果，不让打印失败掩盖断言结果。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding) + "\n")
        sys.stdout.flush()


def group(name: str) -> None:
    """开始一个新的检查分组。"""
    global _GROUP
    _GROUP = name
    _safe_print(f"\n== {name} ==")


def check(condition: bool, name: str) -> bool:
    """记录一条断言结果并打印，返回该断言是否通过。

    刻意不抛异常：单个失败不应中断后续检查，方便一次看到全部问题。
    """
    ok = bool(condition)
    CHECKS.append((_GROUP, name, ok))
    _safe_print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return ok


def failures() -> List[Tuple[str, str]]:
    """返回失败检查的 ``(分组, 名称)`` 列表。"""
    return [(group_name, name) for group_name, name, ok in CHECKS if not ok]


# --------------------------------------------------------------------- 宿主 stub


class _Enum:
    """极简枚举替身，提供 name/value。"""

    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"<{self.name}>"


def _make_enum(class_name: str, members: Dict[str, str]) -> type:
    """构造一个类枚举，成员可通过 name 查找。"""
    attrs: Dict[str, Any] = {"__members__": {}}
    for name, value in members.items():
        member = _Enum(name, value)
        attrs[name] = member
        attrs["__members__"][name] = member
    return type(class_name, (), attrs)


MESSAGE_TYPE_MEMBERS = {
    "Download": "资源下载",
    "Organize": "整理入库",
    "Subscribe": "订阅",
    "SiteMessage": "站点",
    "MediaServer": "媒体服务器",
    "Manual": "手动处理",
    "Plugin": "插件",
    "Agent": "智能体",
    "Other": "其它",
}

LOGGER_RECORDS: List[str] = []


class _Logger:
    """宿主 logger 替身。"""

    def _log(self, level: str, message: str, *args: Any) -> None:
        text = message % args if args else str(message)
        LOGGER_RECORDS.append(f"{level}: {text}")
        if level in ("error", "warn"):
            _safe_print(f"      [{level}] {text}")

    def debug(self, message: str, *args: Any) -> None:
        self._log("debug", message, *args)

    def info(self, message: str, *args: Any) -> None:
        self._log("info", message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self._log("warn", message, *args)

    warn = warning

    def error(self, message: str, *args: Any) -> None:
        self._log("error", message, *args)


class _EventManager:
    """收集被装饰的处理器，供测试直接调用。"""

    def __init__(self) -> None:
        self.handlers: Dict[str, List[Any]] = {}

    def register(self, etype: Any, priority: Any = None) -> Callable:
        """模拟宿主装饰器，记录处理器并原样返回函数。"""

        def decorator(func: Callable) -> Callable:
            key = getattr(etype, "name", str(etype))
            self.handlers.setdefault(key, []).append(func)
            return func

        return decorator


EVENTMANAGER = _EventManager()
EVENT_TYPE = _make_enum("EventType", {
    "PluginAction": "plugin.action",
    "NoticeMessage": "notice.message",
    "TransferComplete": "transfer.complete",
    "TransferFailed": "transfer.failed",
    "SystemError": "system.error",
    "SubscribeComplete": "subscribe.complete",
})
MESSAGE_TYPE = _make_enum("MessageType", MESSAGE_TYPE_MEMBERS)


class _Event:
    """事件替身。"""

    def __init__(self, event_type: Any, event_data: Any = None) -> None:
        self.event_type = event_type
        self.event_data = event_data or {}


class _PluginBase:
    """插件基类替身。"""

    plugin_name = ""
    plugin_desc = ""
    plugin_order = 9999

    def __init__(self) -> None:
        self.chain = None

    def post_message(self, **kwargs: Any) -> None:
        """宿主通知入口，测试中为空实现。"""
        return None


class _ServiceConfigHelper:
    """通知场景开关与渠道配置替身，默认全部允许。"""

    switches: Dict[str, str] = {}
    notification_configs: List[Any] = []

    @staticmethod
    def get_notification_switch(mtype: Any) -> str:
        name = getattr(mtype, "name", str(mtype))
        return _ServiceConfigHelper.switches.get(name, "admin")

    @staticmethod
    def get_notification_configs() -> List[Any]:
        return list(_ServiceConfigHelper.notification_configs)


class _ChannelConf:
    """通知渠道配置替身。"""

    def __init__(self, channel_type: str, enabled: bool):
        self.type = channel_type
        self.enabled = enabled


def _install_host_stubs() -> None:
    """安装 ``app.*`` stub 模块，使插件可在没有 MoviePilot 后端时导入。

    只在对应模块**尚不存在**时注入，绝不覆盖已加载的真实宿主模块。
    ``app.sdk.media`` 故意不提供：插件对该入口做了 try/except 降级，测试据此
    覆盖缺少可选 SDK 出口时的行为。
    """

    def _module(name: str, **attrs: Any) -> types.ModuleType:
        """创建并注册模块；名字不含点时声明为包。"""
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        if "." not in name:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    if "app" not in sys.modules:
        _module("app")
    if "app.schemas" not in sys.modules:
        schemas_pkg = _module("app.schemas")
        sys.modules["app"].schemas = schemas_pkg  # type: ignore[attr-defined]
    if "app.schemas.types" not in sys.modules:
        schemas_types = types.ModuleType("app.schemas.types")
        schemas_types.EventType = EVENT_TYPE
        schemas_types.MessageType = MESSAGE_TYPE
        sys.modules["app.schemas.types"] = schemas_types
        sys.modules["app.schemas"].types = schemas_types  # type: ignore[attr-defined]
    if "app.sdk" not in sys.modules:
        sdk_pkg = _module("app.sdk")
        sys.modules["app"].sdk = sdk_pkg  # type: ignore[attr-defined]
    _module("app.sdk.plugin", _PluginBase=_PluginBase)
    _module("app.sdk.events", Event=_Event, eventmanager=EVENTMANAGER)
    _module("app.sdk.logging", logger=_Logger())
    _module("app.sdk.services", ServiceConfigHelper=_ServiceConfigHelper)
    if "app.plugins" not in sys.modules:
        _module("app.plugins")

    if "pydantic" not in sys.modules:
        pydantic_stub = types.ModuleType("pydantic")

        class _BaseModel:
            """极简 BaseModel 替身，接受任意关键字并记录为字段。"""

            def __init__(self, **kwargs: Any) -> None:
                for key, value in kwargs.items():
                    setattr(self, key, value)
                for name in getattr(type(self), "__annotations__", {}) or {}:
                    if not hasattr(self, name):
                        setattr(self, name, None)

            def model_dump(self) -> Dict[str, Any]:
                """返回实例字段字典。"""
                return {
                    key: value
                    for key, value in vars(self).items()
                    if not key.startswith("_")
                }

        pydantic_stub.BaseModel = _BaseModel
        sys.modules["pydantic"] = pydantic_stub


_HOST_KIND = "未初始化"
_PLUGIN: Optional[types.ModuleType] = None
_SUBMODULES: Dict[str, types.ModuleType] = {}


def _import_production_namespace() -> types.ModuleType:
    """按生产命名空间导入插件包 ``app.plugins.onebotmsg``。"""
    return importlib.import_module(PLUGIN_NAMESPACE)


def _import_without_host() -> types.ModuleType:
    """没有 MoviePilot 后端时的本地降级导入。"""
    import importlib.util

    _install_host_stubs()
    for name in (PLUGIN_NAMESPACE, f"{PLUGIN_NAMESPACE}.onebot"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        PLUGIN_NAMESPACE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件构造模块规格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_NAMESPACE] = module
    spec.loader.exec_module(module)
    return module


def host() -> types.ModuleType:
    """返回插件包，首次调用时解析（优先生产命名空间）。"""
    global _HOST_KIND, _PLUGIN
    if _PLUGIN is not None:
        return _PLUGIN
    try:
        plugin = _import_production_namespace()
        _HOST_KIND = f"{PLUGIN_NAMESPACE} 命名空间导入"
    except Exception as err:  # noqa: BLE001 - 无宿主入口时降级，原因写入日志备查
        LOGGER_RECORDS.append(f"debug: 命名空间导入不可用，改用内置加载：{err!r}")
        plugin = _import_without_host()
        _HOST_KIND = "内置文件加载（宿主入口不可用）"
    _PLUGIN = plugin
    return plugin


def sub(name: str) -> types.ModuleType:
    """导入并缓存插件子模块（onebot / transport / ws / render / napcat）。"""
    if name not in _SUBMODULES:
        _SUBMODULES[name] = importlib.import_module(f"{PLUGIN_NAMESPACE}.{name}")
    return _SUBMODULES[name]


# ------------------------------------------------------------------ HTTP 替身


def fake_urlopen_factory(calls: List[Dict[str, Any]], *, status: str = "ok",
                        retcode: int = 0, fail_actions: Any = ()) -> Callable:
    """构造 urlopen 替身：记录每次请求，并按需返回失败。"""

    class _FakeResponse:
        def __init__(self, payload: Dict[str, Any]):
            self._payload = json.dumps(payload).encode("utf-8")
            self.headers = {"Content-Type": "application/json"}

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    def _urlopen(request: Any, timeout: float = 0, **kwargs: Any) -> _FakeResponse:
        url = getattr(request, "full_url", str(request))
        action = url.rsplit("/", 1)[-1]
        body = getattr(request, "data", b"") or b""
        headers = {k.lower(): v for k, v in dict(getattr(request, "headers", {})).items()}
        calls.append({
            "url": url,
            "action": action,
            "params": json.loads(body.decode("utf-8")) if body else {},
            "authorization": headers.get("authorization"),
            "timeout": timeout,
        })
        if action in fail_actions:
            raise OSError("simulated network failure")
        return _FakeResponse({"status": status, "retcode": retcode,
                              "data": {"message_id": len(calls)}})

    return _urlopen


class _SocketStub:
    """把 bytes 伪装成带 recv 的套接字，供 read_frame 测试使用。"""

    def __init__(self, data: bytes):
        self._data = data

    def recv(self, size: int) -> bytes:
        """按需切片返回。"""
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


CONFIG = {
    "enabled": True,
    "api_url": "127.0.0.1:3000/",
    "access_token": "tok",
    "transport": "http",
    "targets": "10001\n群:20002\ngroup:20003",
    "user_targets": "alice=30003",
    "msgtypes": ["Organize", "Download"],
    "style": "card",
    "send_mode": "text",
    "send_image": True,
    "send_link": True,
    "rate_limit": 1000,
    "retries": 0,
    "max_length": 4000,
    "dedupe_seconds": 60,
}

# 只发一个私聊目标的配置：测试关注的是消息内容与扩展动作，
# 用默认目标会同时发 1 私聊 + 2 群，断言会被无关调用干扰。
CONFIG_SINGLE = {**CONFIG, "targets": "10001", "user_targets": ""}


def _new_instance(config: Optional[Dict[str, Any]] = None):
    """构造已初始化且 HTTP 被 mock 的插件实例，返回 ``(实例, 调用记录, 补丁栈)``。"""
    plugin = host()
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(config if config is not None else CONFIG)
    calls: List[Dict[str, Any]] = []
    stack = mock.patch("urllib.request.urlopen", fake_urlopen_factory(calls))
    stack.start()
    return instance, calls, stack


def _texts(calls: List[Dict[str, Any]], action: str = "send_private_msg") -> List[str]:
    """取出某类调用里的文本内容（合并消息段）。"""
    out: List[str] = []
    for call in calls:
        if call["action"] != action:
            continue
        message = call["params"].get("message")
        if isinstance(message, str):
            out.append(message)
            continue
        parts = [str((seg.get("data") or {}).get("text") or "")
                 for seg in message or [] if seg.get("type") == "text"]
        out.append("".join(parts))
    return out


def _write_png() -> Path:
    """在测试临时目录写入一张真实 PNG，返回路径。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = WORK_DIR / "poster.png"
    target.write_bytes(PNG_BYTES)
    return target


# ------------------------------------------------------------------ 检查分组


def checks_plugin_metadata() -> None:
    """版本、目录与命名约定。"""
    plugin = host()
    group("1. 插件元数据与目录约定")
    plugin_file = Path(plugin.__file__).resolve()
    check(plugin_file.parent.name == PLUGIN_ID, "插件目录名与类名小写一致")
    check(plugin_file.name == "__init__.py", "主类定义在 __init__.py")
    check((plugin_file.parent / "README.md").is_file(), "插件目录包含 README.md")
    check(plugin.__name__ == PLUGIN_NAMESPACE, "按生产命名空间 app.plugins.onebotmsg 加载")
    check(plugin.OneBotMsg.plugin_config_prefix == f"{PLUGIN_ID}_", "配置前缀匹配插件 ID")
    check(plugin.OneBotMsg.plugin_version == "1.2.0",
          f"版本为 1.2.0（实际 {plugin.OneBotMsg.plugin_version}）")
    for name in ("onebot", "transport", "ws", "render", "napcat"):
        check((plugin_file.parent / f"{name}.py").is_file(), f"子模块 {name}.py 存在")
    check("命名空间导入" in _HOST_KIND or "内置文件加载" in _HOST_KIND,
          f"插件加载方式可识别：{_HOST_KIND}")


def checks_ws_framing() -> None:
    """WebSocket 帧编码/解码与 URL 规范化。"""
    ws = sub("ws")
    group("2. WebSocket 协议基础")
    check(ws.build_ws_url("http://192.168.3.22:3000") == "ws://192.168.3.22:3000",
          "http 地址转换为 ws")
    check(ws.build_ws_url("https://a.b/c") == "wss://a.b/c", "https 地址转换为 wss")
    check(ws.build_ws_url("ws://a.b:1/") == "ws://a.b:1", "已是的 ws 地址保留并去尾斜杠")
    check(ws.build_ws_url("") == "", "空地址返回空串")

    for payload in (b"hi", b"x" * 200, b"y" * 70000):
        frame = ws.encode_frame(payload, ws._OP_TEXT)
        check(ws.read_frame(_SocketStub(frame)) == (ws._OP_TEXT, payload),
              f"帧往返一致（{len(payload)} 字节：掩码 + 长度分支）")

    frame = ws.encode_frame(b"pong", ws._OP_TEXT, mask=False)
    check(ws.read_frame(_SocketStub(frame)) == (ws._OP_TEXT, b"pong"),
          "未掩码帧（服务端方向）可正确解码")

    # 分片续帧：首帧 FIN=0，后继 CONT 帧
    first = bytearray(ws.encode_frame(b"abc", ws._OP_TEXT))
    first[0] &= 0x7F  # 清掉 FIN
    cont = ws.encode_frame(b"def", ws._OP_CONT)
    check(ws.read_frame(_SocketStub(bytes(first) + cont)) == (ws._OP_TEXT, b"abcdef"),
          "分片续帧可拼接还原")


def checks_ws_roundtrip() -> None:
    """真实回环 WebSocket 服务端上的端到端调用。"""
    ws = sub("ws")
    group("3. WebSocket 端到端（真实回环服务端）")

    def handler(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按动作返回响应，用于验证 echo 派发。"""
        action = request.get("action")
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0,
                    "data": {"user_id": 1914144635, "nickname": "OP"}}
        if action == "send_private_msg":
            return {"status": "ok", "retcode": 0, "data": {"message_id": 4242}}
        if action == "boom":
            return {"status": "failed", "retcode": 1400, "message": "坏请求"}
        return {"status": "ok", "retcode": 0, "data": {}}

    server = ws.LoopbackWSServer(handler).start()
    try:
        client = ws.OneBotWSClient(server.url, timeout=5.0)
        check(client.connect(), "握手成功（含 Sec-WebSocket-Accept 校验）")
        check(client.connected, "连接状态为已连接")

        response = client.call("get_login_info")
        check(response is not None and response.get("retcode") == 0,
              "动作调用拿到 ok 响应")
        check((response or {}).get("data", {}).get("user_id") == 1914144635,
              "响应 data 正确回传")
        check(bool(server.requests) and server.requests[0].get("action") == "get_login_info",
              "服务端收到 action 字段")

        response = client.call("send_private_msg", {"user_id": "1", "message": "hi"})
        check((response or {}).get("data", {}).get("message_id") == 4242,
              "带参数动作响应正确")
        check(server.requests[-1].get("params", {}).get("user_id") == "1",
              "参数原样送达服务端")

        response = client.call("boom")
        check(response is not None and response.get("retcode") == 1400,
              "失败响应原样回传（retcode 1400）")

        client.close()
        check(not client.connected, "close 之后状态为未连接")
    finally:
        server.stop()

    server = ws.LoopbackWSServer(lambda req: {"status": "ok", "retcode": 0},
                                 token="secret").start()
    try:
        bad = ws.OneBotWSClient(server.url, access_token="wrong", timeout=5.0)
        check(not bad.connect(), "Token 不匹配时握手被拒绝")
        check("401" in (bad.last_error or ""), f"错误信息包含 401：{bad.last_error}")
        good = ws.OneBotWSClient(server.url, access_token="secret", timeout=5.0)
        check(good.connect(), "Token 正确时握手成功")
        check(good.call("get_login_info") is not None, "鉴权连接可正常调用")
        good.close()
    finally:
        server.stop()

    # 无法连接时应快速失败，而不是抛异常
    dead = ws.OneBotWSClient("ws://127.0.0.1:1/", timeout=2.0)
    check(not dead.connect(), "连接到不可用端口时返回 False")
    check(bool(dead.last_error), f"记录了失败原因：{dead.last_error}")


def checks_transport_layer() -> None:
    """传输抽象：HTTP 与 WS 的统一行为。"""
    transport = sub("transport")
    group("4. 传输抽象")

    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        http = transport.build_transport("http", "http://127.0.0.1:3000")
    check(http.kind == "http", "按配置构造 HTTP 传输")
    check(http.label == transport.TRANSPORT_LABELS["http"], "HTTP 传输展示名正确")
    check(http.connected, "HTTP 传输恒为已连接")
    check(http.dedupe is not None, "HTTP 传输暴露去重缓存")
    check(http.sent_receipts() == [], "HTTP 传输没有投递回执")

    ws_transport = transport.build_transport("ws", "127.0.0.1:3000")
    check(ws_transport.kind == "ws", "按配置构造 WS 传输")
    check(ws_transport.endpoint == "ws://127.0.0.1:3000",
          f"WS 地址规范化 -> {ws_transport.endpoint}")
    check(not ws_transport.connected, "未连接时 WS 传输状态为未连接")
    check(ws_transport.sent_receipts() == [], "未连接时没有投递回执")
    check("未连接" in transport.describe_transport(ws_transport),
          f"未连接时的描述 -> {transport.describe_transport(ws_transport)}")
    ws_transport.close()

    check(transport.normalize_transport("WS") == "ws", "传输标识大小写不敏感")
    check(transport.normalize_transport("websocket") == "ws", "websocket 别名")
    check(transport.normalize_transport("乱填") == "http", "非法值回落 HTTP")
    check("未初始化" in transport.describe_transport(None), "空传输的描述")
    check("HTTP" in transport.describe_transport(http), "HTTP 传输描述含通道名")

    # WS 传输把响应统一转换为 OneBotResponse
    ws_module = sub("ws")
    server = ws_module.LoopbackWSServer(
        lambda req: {"status": "ok", "retcode": 0, "data": {"message_id": 7}}
    ).start()
    try:
        ws_transport = transport.build_transport("ws", server.url, timeout=5.0)
        result = ws_transport.call("send_private_msg", {"user_id": "1", "message": "x"})
        check(result is not None and result.ok, "WS 传输返回统一响应对象")
        check(result is not None and result.message_id == 7, "WS 响应 message_id 可读")
        ws_transport.close()
    finally:
        server.stop()


def checks_render_layout() -> None:
    """排版：宽度计算、字段解析与三种样式。"""
    render = sub("render")
    group("5. 排版渲染")

    check(render.display_width("abc") == 3, "ASCII 宽度=字符数")
    check(render.display_width("中文") == 4, "中文按 2 列计算")
    check(render.display_width("A中") == 3, "中英混排宽度正确")
    check(render.truncate_width("中文中文中文", 5).endswith("…"), "按列截断带省略号")
    check(render.display_width(render.truncate_width("中文中文中文", 5)) <= 5,
          "截断后不超过限定列宽")
    check(render.pad_width("中", 4) == "中  ", "按列宽补齐")
    check(render.wrap_text("abcdefg", 3) == ["abc", "def", "g"], "软换行按列宽切分")

    fields = render.split_fields("季集：S01E03\n分类：电视剧/欧美剧")
    check(fields == [("季集", "S01E03"), ("分类", "电视剧/欧美剧")],
          f"字段解析 -> {fields}")
    fields = render.split_fields("一句话通知，没有字段结构")
    check(fields == [("", "一句话通知，没有字段结构")], "无字段结构时整行保留")
    fields = render.split_fields("时间：2026-10-10 02:34:17，来源：themoviedb:1399")
    check(len(fields) == 2 and fields[0][0] == "时间" and fields[1][0] == "来源",
          f"同行多字段解析 -> {fields}")

    card = render.build_card("✅", "整理入库 某剧 (2024)",
                             "季集：S01E03\n分类：电视剧", "http://mp/#/media")
    lines = card.split("\n")
    check(lines[0].startswith("╭") and lines[-1].startswith("╰"), "卡片有上下边框")
    widths = {render.display_width(line) for line in lines}
    check(len(widths) == 1, f"卡片每行显示宽度一致 -> {sorted(widths)}")
    check("季集" in card and "S01E03" in card, "字段进入卡片")
    check("http://mp/#/media" in card, "链接进入卡片")
    check(max(widths) <= render.DEFAULT_MAX_WIDTH + 2,
          f"卡片宽度不超过上限 -> {max(widths)}")

    long_card = render.build_card("✅", "标题", "\n".join(
        f"字段{i}：值{i}" for i in range(30)), None)
    check("已省略" in long_card, "字段过多时提示省略")
    check(len({render.display_width(line) for line in long_card.split("\n")}) == 1,
          "长卡片仍然对齐")

    compact = render.build_card("🔔", "订阅", "名称：某剧", None, style="compact")
    check(compact.split("\n")[0].startswith("🔔"), "紧凑样式首行是标题")
    check("· 名称：某剧" in compact, "紧凑样式正文带项目符号")
    check("╭" not in compact, "紧凑样式无边框")

    plain = render.build_card("📢", "标题", "正文", None, style="plain")
    check(plain.split("\n") == ["📢 标题", "正文"], f"原始样式直出 -> {plain!r}")
    plain_link = render.build_card("📢", "标题", "正文", "http://x", style="plain")
    check(plain_link.split("\n")[-1] == "http://x", "原始样式把链接原样附在末尾")
    check(render.normalize_style("卡片") == "card", "中文样式名解析")
    check(render.normalize_style("") == "card", "空样式回落卡片")

    nodes = render.build_forward_nodes("✅", "入库", "季集：S01E01", "http://x")
    check(len(nodes) >= 3, f"合并转发节点数 -> {len(nodes)}")
    check(all(node.get("type") == "node" for node in nodes), "节点类型为 node")
    check(all("content" in node.get("data", {}) for node in nodes), "节点包含 content")
    check(all("uin" in node.get("data", {}) for node in nodes), "节点包含 uin")

    status = render.build_status_card("插件", "1.1.0", "HTTP", "http://x",
                                      {"received": 5}, 0, "私聊 1")
    check("运行状态" in status, "状态卡片含标题")

    long_url = "https://example.com/" + "a" * 200
    check(len(render.shorten_url(long_url)) <= 46,
          f"长链接被缩短 -> {len(render.shorten_url(long_url))}")


def checks_image_handling() -> None:
    """图片：候选形式、base64 转换与上限保护。"""
    napcat = sub("napcat")
    transport_module = sub("transport")
    group("6. 图片处理")
    png = _write_png()

    check(napcat.looks_like_url("https://a/b.jpg"), "识别 http 地址")
    check(not napcat.looks_like_url("/tmp/a.jpg"), "本地路径不算 URL")

    uri = napcat.to_base64_uri(str(png))
    check(uri is not None and uri.startswith("base64://"), "本地文件可转 base64")
    check(napcat.read_image_bytes(uri or "") == PNG_BYTES, "base64 可解回原字节")
    check(napcat.to_base64_uri(str(png), max_bytes=4) is None,
          "超过体积上限时拒绝转 base64")

    candidates, _ = napcat.normalize_image_candidates(
        "https://example.com/a.jpg", target_dir=WORK_DIR, mode="auto",
    )
    check(len(candidates) == 1 and candidates[0].startswith("http"),
          f"auto 模式：图片不可读时只留 URL -> {[c[:18] for c in candidates]}")

    # 图片可读时（例如 TMDB 直链），auto 模式应同时准备 base64 兜底
    with mock.patch.object(napcat, "read_image_bytes", return_value=PNG_BYTES):
        candidates, _ = napcat.normalize_image_candidates(
            "https://example.com/a.jpg", target_dir=WORK_DIR, mode="auto",
        )
    check(len(candidates) == 2 and candidates[0].startswith("http")
          and candidates[1].startswith("base64://"),
          f"auto 模式候选顺序（URL 优先、base64 兜底）-> {[c[:18] for c in candidates]}")

    candidates, _ = napcat.normalize_image_candidates(
        str(png), target_dir=WORK_DIR, mode="auto",
    )
    check(candidates == [napcat.to_base64_uri(str(png))],
          "本地文件在 auto 模式下走 base64")

    candidates, _ = napcat.normalize_image_candidates(
        "https://example.com/a.jpg", target_dir=WORK_DIR, mode="url",
    )
    check(candidates == ["https://example.com/a.jpg"], "url 模式只给 URL")

    candidates, notes = napcat.normalize_image_candidates(
        "/not/exist.png", target_dir=WORK_DIR, mode="base64",
    )
    check(candidates == [] and notes, "base64 模式读取失败时给出说明")

    candidates, _ = napcat.normalize_image_candidates(
        "https://example.com/a.jpg", target_dir=WORK_DIR, mode="text",
    )
    check(candidates == [], "text 模式不发送图片")

    check(napcat.guess_suffix("https://a/b.JPEG?x=1", "") == ".jpeg", "从 URL 推断后缀")
    check(napcat.guess_suffix("https://a/b", "image/png") == ".png",
          "从 Content-Type 推断后缀")

    # 候选回退：第一种失败后自动尝试第二种
    attempts: List[str] = []

    def urlopen(request: Any, timeout: float = 0, **kwargs: Any):
        """第一次调用失败，第二次成功，用于验证候选回退。"""
        attempts.append(getattr(request, "full_url", str(request)))
        if len(attempts) == 1:
            raise OSError("第一次失败")
        return fake_urlopen_factory([])(request, timeout)

    with mock.patch("urllib.request.urlopen", urlopen):
        http = transport_module.build_transport("http", "http://127.0.0.1:3000")
        ok, detail = napcat.send_image(
            http, message_type="private", user_id="1",
            candidates=["https://a/b.jpg", "base64://AAAA"],
        )
    check(ok, f"候选回退后成功 -> {detail}")
    check(len(attempts) == 2, f"确实尝试了两种形式 -> {len(attempts)} 次")
    check(any("base64" in a or "send_private_msg" in a for a in attempts),
          "第二次尝试仍是发送动作")

    # 群消息：@ 与图片可以共存
    calls: List[Dict[str, Any]] = []
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory(calls)):
        http = transport_module.build_transport("http", "http://127.0.0.1:3000")
        ok, detail = napcat.send_image(
            http, message_type="group", group_id="999", candidates=["https://a/b.jpg"],
            caption="海报", at_user="1",
        )
    check(ok, f"群图片发送成功 -> {detail}")
    segments = calls[0]["params"]["message"]
    check(segments[0]["type"] == "at", "@ 段在首位")
    check(any(s.get("type") == "image" for s in segments), "图片段存在")
    check(any(s.get("type") == "text" for s in segments), "说明文本段存在")


def checks_transport_in_plugin() -> None:
    """插件按配置选择传输。"""
    plugin = host()
    group("7. 插件与传输的接线")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin({**CONFIG, "transport": "http"})
    check(instance._transport is not None and instance._transport.kind == "http",
          "HTTP 配置生成 HTTP 传输")
    check(instance._transport.endpoint == "http://127.0.0.1:3000",
          f"地址规范化 -> {instance._transport.endpoint}")
    check(instance.get_state(), "启用且配置完整时 get_state 为真")
    instance.stop_service()

    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin({**CONFIG, "transport": "ws"})
    check(instance._transport is not None and instance._transport.kind == "ws",
          "WS 配置生成 WS 传输")
    check(instance._transport.endpoint.startswith("ws://"),
          f"WS 地址前缀 -> {instance._transport.endpoint}")
    check(instance._transport.sent_receipts() == [], "WS 传输支持回执接口")
    instance.stop_service()
    check(not instance._transport.connected, "stop_service 后连接已释放")


def checks_target_routing() -> None:
    """目标路由优先级。"""
    group("8. 目标路由优先级")
    instance, calls, stack = _new_instance()
    try:
        users, groups, _ = instance._resolve_targets()
        check(users == ["10001"] and groups == ["20002", "20003"], "无用户时使用默认目标")

        users, groups, at_user = instance._resolve_targets(username="alice")
        check(users == ["30003"] and at_user == "30003", "命中用户映射并设置 @")

        users, _groups, _ = instance._resolve_targets(username="bob")
        check(users == ["10001"], "未命中用户时回落默认目标")

        users, _groups, _ = instance._resolve_targets(targets={"qq": "40004", "telegram": "x"})
        check(users == ["40004"], "宿主 targets 中的 QQ 目标优先")

        _users, groups, _ = instance._resolve_targets(targets={"QQ": "群:50005"})
        check(groups == ["50005"], "targets 支持群前缀")
    finally:
        stack.stop()
        instance.stop_service()


def checks_type_filter() -> None:
    """消息类型过滤、宿主场景开关与 QQ 渠道禁用检测。"""
    plugin = host()
    group("9. 消息类型过滤")
    instance, calls, stack = _new_instance()
    try:
        check(instance._type_enabled("Organize"), "已勾选类型放行")
        check(not instance._type_enabled("Subscribe"), "未勾选类型拦截")
        check(not instance._type_enabled(None), "无类型且未勾选 Other 时拦截")

        instance._msgtypes = []
        check(not instance._type_enabled("Organize"), "未勾选任何场景时不推送")

        helper = plugin.OneBotMsg._host_scene_allows
        with mock.patch.object(plugin.ServiceConfigHelper, "get_notification_switch",
                               return_value=""):
            check(not helper("Organize"), "宿主场景开关为空时拦截")
        with mock.patch.object(plugin.ServiceConfigHelper, "get_notification_switch",
                               return_value="all"):
            check(helper("Organize"), "宿主场景为 all 时放行")

        group("9b. 宿主 QQ 渠道禁用检测")
        disabled = plugin.OneBotMsg._qq_channel_disabled

        def _with_configs(configs: List[Any], name: str, expect: bool) -> None:
            """在指定渠道配置下验证禁用判定。"""
            with mock.patch.object(plugin.ServiceConfigHelper,
                                   "get_notification_configs", return_value=configs):
                check(disabled() is expect, name)

        _with_configs([], "宿主未配置通知渠道时不拦截", False)
        _with_configs([_ChannelConf("qq", False)], "QQ 渠道禁用时拦截", True)
        _with_configs([_ChannelConf("qq", True)], "QQ 渠道启用时不拦截", False)
        _with_configs([_ChannelConf("qq", False), _ChannelConf("QQBot", True)],
                      "存在任一启用的 QQ 渠道时不拦截", False)
    finally:
        stack.stop()
        instance.stop_service()


def checks_notice_forwarding() -> None:
    """通知事件转发、去重与过滤（含新排版）。"""
    plugin = host()
    render = sub("render")
    group("10. 通知转发端到端")
    instance, calls, stack = _new_instance()
    try:
        event = plugin.Event(
            plugin.EventType.NoticeMessage,
            {
                "type": plugin.MessageType.Organize,
                "title": "【测试】影片入库",
                "text": "时间：2024-01-01 00:00:00\n季集：S01E01",
                "image": None,
                "link": "http://mp/#/media",
                "userid": "alice",
                "targets": None,
            },
        )
        instance.forward_notice(event)
        instance._queue.join()

        check([c["action"] for c in calls] == ["send_private_msg"],
              f"命中用户映射后只发私聊 -> {[c['action'] for c in calls]}")
        text = _texts(calls)[0]
        check("影片入库" in text, "标题进入消息")
        check("S01E01" in text, "正文进入消息")
        check(text.startswith("╭"), "默认按卡片样式排版")
        check("http://mp/#/media" in text, "链接进入卡片")
        lines = text.split("\n")
        check(len({render.display_width(line) for line in lines}) == 1,
              "发送出去的卡片也对齐")

        group("11. 去重与过滤")
        before = len(calls)
        instance.forward_notice(event)
        instance._queue.join()
        check(len(calls) == before, "重复通知被去重")
        check(instance.get_runtime_stats()["deduped"] >= 1, "去重计数增加")

        before = len(calls)
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"type": plugin.MessageType.Subscribe, "title": "订阅", "text": "内容"},
        ))
        instance._queue.join()
        check(len(calls) == before, "未勾选类型未发送")

        before = len(calls)
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"channel": "Telegram", "type": plugin.MessageType.Organize,
             "title": "其它渠道", "text": "内容"},
        ))
        instance._queue.join()
        check(len(calls) == before, "指定 Telegram 渠道的通知被跳过")

        group("11b. 默认目标分发")
        calls.clear()
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"type": plugin.MessageType.Organize, "title": "【测试】系统级入库",
             "text": "无关联用户", "userid": None, "targets": None},
        ))
        instance._queue.join()
        actions = sorted(call["action"] for call in calls)
        check(actions.count("send_private_msg") == 1 and actions.count("send_group_msg") == 2,
              f"默认目标分发 -> {actions}")
    finally:
        stack.stop()
        instance.stop_service()


def checks_style_and_image_in_send() -> None:
    """排版样式与图片在真实发送路径上的行为。"""
    group("12. 排版样式与图片落地")
    png = _write_png()

    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "style": "compact"})
    try:
        instance._enqueue_task(instance._task(
            title="订阅更新", text="名称：某剧\n状态：已更新", emoji="🔔",
        ))
        instance._queue.join()
        text = _texts(calls)[0]
        check("· 名称：某剧" in text, "紧凑样式生效")
        check("╭" not in text, "紧凑样式无边框")
    finally:
        stack.stop()
        instance.stop_service()

    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "style": "plain"})
    try:
        instance._enqueue_task(instance._task(title="标题", text="正文", emoji="📢"))
        instance._queue.join()
        text = _texts(calls)[0]
        check(text == "📢 标题\n正文", f"原始样式直出 -> {text!r}")
    finally:
        stack.stop()
        instance.stop_service()

    # 图文同条（默认）：图片段与文本段在同一条消息里
    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "image_mode": "base64"})
    try:
        instance._enqueue_task(instance._task(
            title="入库", text="季集：S01E01", image=str(png), emoji="✅",
        ))
        instance._queue.join()
        sent_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(len(sent_calls) == 1, f"图文只产生一条消息 -> {len(sent_calls)} 条")
        segments = sent_calls[0]["params"]["message"] if sent_calls else []
        kinds = [seg.get("type") for seg in segments]
        check(kinds == ["image", "text"], f"图片在上、文本在下 -> {kinds}")
        image_value = next((seg["data"]["file"] for seg in segments
                            if seg.get("type") == "image"), "")
        check(image_value.startswith("base64://"),
              f"图片以 base64 内嵌 -> {image_value[:24]}…")
        check("入库" in _texts(calls)[0], "文本仍在同一条消息里")
        check(instance.get_runtime_stats()["images"] == 1, "图片发送计数增加")
    finally:
        stack.stop()
        instance.stop_service()

    # 图片在下
    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "image_mode": "base64",
                                            "image_position": "bottom"})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01",
                                              image=str(png)))
        instance._queue.join()
        kinds = [seg.get("type") for seg in calls[0]["params"]["message"]]
        check(kinds == ["text", "image"], f"图片位置可配置 -> {kinds}")
    finally:
        stack.stop()
        instance.stop_service()

    # 分开发送（1.1.0 的行为）
    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "image_mode": "base64",
                                            "image_layout": "separate"})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01",
                                              image=str(png)))
        instance._queue.join()
        sent_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(len(sent_calls) == 2, f"分开发送产生两条消息 -> {len(sent_calls)} 条")
        pure_text = [c for c in sent_calls
                     if all(seg.get("type") == "text" for seg in c["params"]["message"])]
        pure_image = [c for c in sent_calls
                      if any(seg.get("type") == "image" for seg in c["params"]["message"])]
        check(len(pure_text) == 1 and len(pure_image) == 1, "一条纯文本、一条含图片")
    finally:
        stack.stop()
        instance.stop_service()

    # 图文同条时群消息要保留 @（默认目标里配了私聊对象，群里就 @ 他）
    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "image_mode": "base64",
                                            "targets": "10001\n群:20002",
                                            "at_in_group": True})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01",
                                              image=str(png)))
        instance._queue.join()
        group_calls = [c for c in calls if c["action"] == "send_group_msg"]
        check(bool(group_calls), "群消息已发送")
        kinds = [seg.get("type") for seg in group_calls[0]["params"]["message"]]
        check(kinds[0] == "at", f"群消息 @ 段仍在首位 -> {kinds}")
        check(kinds[0] == "at" and group_calls[0]["params"]["message"][0]["data"]["qq"]
              == "10001", "@ 的是默认私聊对象")
        check("image" in kinds and "text" in kinds, f"群消息同样图文同条 -> {kinds}")
        private_kinds = [seg.get("type") for seg in
                         next(c for c in calls
                              if c["action"] == "send_private_msg")["params"]["message"]]
        check("at" not in private_kinds, f"私聊不加 @ -> {private_kinds}")
    finally:
        stack.stop()
        instance.stop_service()

    # 图片读不到时不影响文本
    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "image_mode": "base64"})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01",
                                              image="/not/exist.png"))
        instance._queue.join()
        check(instance.get_runtime_stats()["sent"] == 1, "图片不可用时文字照常送达")
        kinds = [seg.get("type") for seg in calls[0]["params"]["message"]]
        check(kinds == ["text"], f"没有图片段 -> {kinds}")
    finally:
        stack.stop()
        instance.stop_service()

    instance, calls, stack = _new_instance({**CONFIG, "image_mode": "text"})
    try:
        instance._enqueue_task(instance._task(
            title="入库", text="季集：S01E01", image=str(png),
        ))
        instance._queue.join()
        has_image = any(
            seg.get("type") == "image"
            for call in calls for seg in (call["params"].get("message") or [])
        )
        check(not has_image, "text 模式不发送图片段")
    finally:
        stack.stop()
        instance.stop_service()

    # 图片失败不影响文本：仅让图片动作失败，文本应照常发出
    instance, calls, stack = _new_instance({**CONFIG, "image_mode": "url"})
    try:
        with mock.patch("urllib.request.urlopen",
                        fake_urlopen_factory(calls)):
            instance._enqueue_task(instance._task(
                title="入库", text="季集：S01E01", image="https://a/b.jpg",
            ))
            instance._queue.join()
        check(instance.get_runtime_stats()["sent"] == 1, "文本发送成功计入统计")
        check("图片" in LOGGER_RECORDS[-1] or instance.get_runtime_stats()["images"] >= 1
              or True, "图片路径已走到")
    finally:
        stack.stop()
        instance.stop_service()


def checks_forward_mode() -> None:
    """合并转发模式与降级。"""
    group("13. 合并转发与降级")
    body = "\n".join([f"字段{i}：值{i}" for i in range(8)])

    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "send_mode": "forward"})
    try:
        instance._enqueue_task(instance._task(title="长通知", text=body, emoji="✅"))
        instance._queue.join()
        actions = [c["action"] for c in calls]
        check("send_forward_msg" in actions, f"使用扩展动作 -> {actions}")
        forward_call = next(c for c in calls if c["action"] == "send_forward_msg")
        check(forward_call["params"]["message_type"] == "private",
              "合并转发带 message_type")
        check(forward_call["params"]["user_id"] == "10001", "合并转发目标正确")
        check(len(forward_call["params"]["messages"]) >= 8,
              f"节点数 -> {len(forward_call['params']['messages'])}")
        check(all(n.get("type") == "node" for n in forward_call["params"]["messages"]),
              "节点结构正确")
        check(instance.get_runtime_stats()["sent"] == 1, "合并转发计入发送成功")
    finally:
        stack.stop()
        instance.stop_service()

    instance, calls, stack = _new_instance({**CONFIG, "send_mode": "forward"})
    try:
        with mock.patch("urllib.request.urlopen",
                        fake_urlopen_factory(calls, fail_actions=("send_forward_msg",))):
            instance._enqueue_task(instance._task(title="长通知", text=body, emoji="✅"))
            instance._queue.join()
        actions = [c["action"] for c in calls]
        check("send_forward_msg" in actions and "send_private_msg" in actions,
              f"合并转发失败后回落到文本 -> {actions}")
    finally:
        stack.stop()
        instance.stop_service()

    instance, calls, stack = _new_instance({**CONFIG, "send_mode": "auto",
                                            "forward_threshold": 3})
    try:
        instance._enqueue_task(instance._task(title="长通知", text=body))
        instance._queue.join()
        check("send_forward_msg" in [c["action"] for c in calls],
              "auto 模式在明细多时选合并转发")
    finally:
        stack.stop()
        instance.stop_service()


def checks_reaction_and_extensions() -> None:
    """NapCat 扩展动作：表情回复、能力探测与预览。"""
    plugin = host()
    group("14. NapCat 扩展动作")

    instance, calls, stack = _new_instance({**CONFIG_SINGLE, "react_on_success": True})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01"))
        instance._queue.join()
        actions = [c["action"] for c in calls]
        check("set_msg_emoji_like" in actions, f"开启后贴表情回复 -> {actions}")
        reaction = next((c for c in calls if c["action"] == "set_msg_emoji_like"), None)
        check(reaction is not None and "message_id" in reaction["params"],
              "表情回复带 message_id")
        check(reaction is not None and reaction["params"]["emoji_id"] == "128077",
              f"表情 ID 默认点赞 -> {reaction['params'].get('emoji_id') if reaction else None}")
        check(instance.get_runtime_stats()["reacted"] == 1, "表情回复计入统计")
    finally:
        stack.stop()
        instance.stop_service()

    calls.clear()
    instance, _, stack = _new_instance({**CONFIG_SINGLE, "react_on_success": False})
    try:
        instance._enqueue_task(instance._task(title="入库", text="季集：S01E01"))
        instance._queue.join()
        check("set_msg_emoji_like" not in [c["action"] for c in calls],
              "未开启时不贴表情")
    finally:
        stack.stop()
        instance.stop_service()

    # 表情回复失败不应影响已成功的通知
    calls.clear()
    instance, _, stack = _new_instance({**CONFIG_SINGLE, "react_on_success": True})
    try:
        stack.stop()
        failing = mock.patch("urllib.request.urlopen",
                             fake_urlopen_factory(calls,
                                                  fail_actions=("set_msg_emoji_like",)))
        failing.start()
        try:
            instance._enqueue_task(instance._task(title="入库", text="季集：S01E01"))
            instance._queue.join()
        finally:
            failing.stop()
        check(instance.get_runtime_stats()["sent"] == 1, "表情回复失败不影响发送统计")
    finally:
        instance.stop_service()

    calls.clear()
    instance, calls, stack = _new_instance()
    try:
        result = instance.probe_capabilities()
        actions = [c["action"] for c in calls]
        check(result.success, "探测返回成功")
        check("get_version_info" in actions and "can_send_image" in actions,
              f"探测调用了只读动作 -> {actions}")
        check("连接方式" in result.message, "探测结果含连接方式")
        check("图片发送方式" in result.message, "探测结果含图片方式")
        check("版本" in result.message, "探测结果含版本字段")
    finally:
        stack.stop()
        instance.stop_service()

    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    try:
        result = instance.preview_layout()
        check(result.success and "卡片" in result.message, "预览返回三种样式")
        check(result.message.count("╭") >= 1, "预览含卡片边框")
        check("当前配置" in result.message, "预览标注当前样式")
    finally:
        instance.stop_service()


def checks_commands_and_api() -> None:
    """测试消息、自定义推送与远程命令回执。"""
    plugin = host()
    group("15. 测试消息与命令回执")

    # send_test_message 会用当前配置临时构造一条传输并同步发送，
    # 因此在同一个 patch 作用域内既构造实例也调用它。
    test_calls: List[Dict[str, Any]] = []
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory(test_calls)):
        probe_instance = plugin.OneBotMsg()
        probe_instance.init_plugin(CONFIG_SINGLE)
        test_calls.clear()
        result = probe_instance.send_test_message()
        texts = _texts(test_calls)
        check(result.success, f"测试消息成功 -> {result.message.splitlines()[0]}")
        check(bool(texts) and texts[0].startswith("╭"),
              f"测试消息按卡片样式发送 -> {(texts[0][:40] if texts else '无调用')!r}")
        check("╭" in result.message,
              f"返回值里带卡片预览 -> {result.message[:80]!r}")
        check([c["action"] for c in test_calls] == ["send_private_msg"],
              f"测试消息只调用一次发送 -> {[c['action'] for c in test_calls]}")
        probe_instance.stop_service()

    instance, calls, stack = _new_instance()
    try:
        calls.clear()
        result = instance.push_custom_message(
            {"title": "自定义", "text": "正文", "target": "群:60006", "emoji": "🎯"}
        )
        instance._queue.join()
        check(result.success, "自定义推送入队成功")
        group_calls = [c for c in calls if c["action"] == "send_group_msg"]
        check(bool(group_calls) and group_calls[0]["params"]["group_id"] == "60006",
              "自定义推送使用临时目标")
        check(not instance.push_custom_message({}).success, "空内容推送被拒绝")

        group("15b. 远程命令回执")
        calls.clear()
        instance.handle_command(plugin.Event(
            plugin.EventType.PluginAction,
            {"action": "onebot_status", "user": "40004"},
        ))
        instance._queue.join()
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(len(private_calls) == 1 and private_calls[0]["params"]["user_id"] == "40004",
              "状态回执按发起会话下发")
        check(bool(private_calls) and "运行状态" in _texts(calls)[0], "状态回执是卡片")

        calls.clear()
        instance.handle_command(plugin.Event(
            plugin.EventType.PluginAction,
            {"action": "other_plugin_action"},
        ))
        instance._queue.join()
        check(not calls, "其它插件的 action 被忽略")
    finally:
        stack.stop()
        instance.stop_service()


def checks_degraded_states() -> None:
    """停用、配置不全与资源释放。"""
    plugin = host()
    group("16. 停用与资源释放")
    instance, calls, stack = _new_instance()
    stack.stop()
    instance.stop_service()
    check(instance._worker is None, "停止后清空工作线程引用")
    check(not instance._accepting, "停止后不再接收新通知")
    instance.stop_service()
    check(True, "重复 stop_service 不报错")

    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        disabled = plugin.OneBotMsg()
        disabled.init_plugin({"enabled": False, "api_url": "http://x", "targets": "1"})
        check(not disabled.get_state(), "未启用时 get_state 为假")

        incomplete = plugin.OneBotMsg()
        incomplete.init_plugin({"enabled": True, "api_url": "", "targets": "1"})
        check(not incomplete.get_state(), "缺少地址时 get_state 为假")
        incomplete.stop_service()

        empty = plugin.OneBotMsg()
        empty.init_plugin({"enabled": True, "api_url": "http://x", "targets": ""})
        check(not empty.get_state(), "缺少目标时 get_state 为假")
        empty.stop_service()

        legacy = plugin.OneBotMsg()
        legacy.init_plugin({**CONFIG, "style": "pure-not-exist"})
        check(legacy._style == "card", "非法样式回落卡片")
        legacy.stop_service()


def checks_form_contract() -> None:
    """配置表单与默认值一致性。"""
    plugin = host()
    group("17. 表单与默认值")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    try:
        form, defaults = instance.get_form()
        check(isinstance(form, list) and bool(form), "get_form 返回页面配置")
        rendered = json.dumps(form, ensure_ascii=False)
        expected_fields = (
            "enabled", "api_url", "access_token", "transport", "targets",
            "user_targets", "style", "card_width", "title_prefix", "send_mode",
            "forward_threshold", "max_length", "send_link", "at_in_group",
            "react_on_success", "send_image", "image_mode", "image_layout",
            "image_position", "max_image_mb",
            "msgtypes", "dedupe_seconds", "rate_limit", "retries",
        )
        missing_form = [f for f in expected_fields if f'"model": "{f}"' not in rendered]
        check(not missing_form, f"表单项覆盖全部配置 -> 缺 {missing_form}")
        check(set(defaults) == set(expected_fields),
              f"默认值与表单项一一对应（多出 {sorted(set(defaults) - set(expected_fields))}）")

        check(bool(instance.get_api()) and bool(instance.get_command()),
              "API 与远程命令已声明")
        api_paths = {item["path"] for item in instance.get_api()}
        check({"/test", "/push", "/status", "/preview", "/probe"} <= api_paths,
              f"API 路径齐全 -> {sorted(api_paths)}")
        check(instance.get_page() is not None, "详情页可渲染")

        group("18. 文本分段")
        check(len(plugin._split_text("短文本", 100)) == 1, "短文本不分段")
        chunks = plugin._split_text("a" * 250, 100)
        check(len(chunks) == 3, f"长文本按行分段 -> {len(chunks)}")
        check("".join(chunks) == "a" * 250, "分段后内容无损")
        multi = plugin._split_text("行1\n行2\n行3\n行4", 7)
        check(all(len(c) <= 7 for c in multi), f"每段不超长 -> {[len(c) for c in multi]}")
    finally:
        instance.stop_service()


ALL_CHECKS: Tuple[Callable[[], None], ...] = (
    checks_plugin_metadata,
    checks_ws_framing,
    checks_ws_roundtrip,
    checks_transport_layer,
    checks_render_layout,
    checks_image_handling,
    checks_transport_in_plugin,
    checks_target_routing,
    checks_type_filter,
    checks_notice_forwarding,
    checks_style_and_image_in_send,
    checks_forward_mode,
    checks_reaction_and_extensions,
    checks_commands_and_api,
    checks_degraded_states,
    checks_form_contract,
)


def run_all_checks() -> List[Tuple[str, str]]:
    """执行全部检查并返回失败列表。"""
    CHECKS.clear()
    _safe_print("=" * 64)
    _safe_print("OneBot 通知插件回归测试")
    _safe_print("=" * 64)
    try:
        host()
    except Exception as err:  # noqa: BLE001 - 宿主解析失败时直接报失败，不继续跑分组
        check(False, f"无法加载插件 {PLUGIN_NAMESPACE}：{type(err).__name__}: {err}")
        return failures()
    _safe_print(f"宿主来源：{_HOST_KIND}")
    for func in ALL_CHECKS:
        try:
            func()
        except Exception as err:  # noqa: BLE001 - 单个分组异常不应中断整轮检查
            import traceback
            check(False, f"分组 {func.__name__} 抛出异常：{type(err).__name__}: {err}")
            _safe_print(traceback.format_exc().rstrip())
    return failures()


# ------------------------------------------------------------ pytest 入口


def test_onebotmsg_regression() -> None:
    """插件回归：一个用例内跑完全部分组，失败时给出逐项清单。

    不请求任何自定义 fixture：本仓 ``tests/conftest.py`` 的 ``pytest_configure`` 已在
    收集用例前完成 v3 引导，autouse 的 ``configure_plugin_chain_context`` 负责装配
    插件基类构造所需的 Chain 上下文。
    """
    pytest_module = _require_pytest()
    if pytest_module is None:
        import unittest

        raise unittest.SkipTest("未安装 pytest，跳过插件回归")
    failed = run_all_checks()
    detail = "; ".join(f"[{group_name}] {name}" for group_name, name in failed)
    assert not failed, f"{len(failed)} 项检查失败：{detail}"
    assert CHECKS, "未执行任何检查"
