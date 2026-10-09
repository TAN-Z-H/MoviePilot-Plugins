"""OneBot 通知插件的回归测试。

测试目标（对齐 MoviePilot 插件仓约定）：

* 放在 ``tests/v3/onebotmsg/``，文件名与用例名可被 pytest 发现，由 ``tests/run.py``
  的独立 v3 会话执行；
* 通过**生产命名空间** ``app.plugins.onebotmsg`` 导入插件与 ``onebot`` 子模块，
  不把插件目录加入 ``sys.path``，避免同一源码被双重导入而重复注册事件；
* 只 mock HTTP 调用，不依赖公网、真实 NapCat 或真实数据库。

运行方式：

    <workspace>/.venv/bin/python -m pytest tests/v3/onebotmsg   # 仓库门禁
    <workspace>/.venv/bin/python tests/run.py                   # 全量回归
    python tests/v3/onebotmsg/test_plugin.py                    # 无宿主时的 stub 自检

缺少 MoviePilot 后端时（例如仅装了标准库的开发机），测试自动降级为内置 stub 自检，
保证纯逻辑仍可验证；正式提交与 CI 必须走真实宿主。

真实 V3 宿主的加载验证（插件安装、启停、重载、命令与 API 注册、QQ 实发）不属于
本文件的职责，必须在真实 MoviePilot V3 环境中单独完成。
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

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
_ONEBOT: Optional[types.ModuleType] = None


def _import_production_namespace() -> types.ModuleType:
    """按生产命名空间导入插件包 ``app.plugins.onebotmsg``。

    仓库 conftest 的 ``pytest_configure`` 会调用后端 ``prepare_v3_backend``，
    把 ``plugins.v3/`` 注入 ``app.plugins.__path__``，因此这里是 CI 与本地回归
    的真实路径，也是 ``tests/README.md`` 约定的导入方式。
    """
    return importlib.import_module(PLUGIN_NAMESPACE)


def _import_without_host() -> types.ModuleType:
    """没有 MoviePilot 后端时的本地降级导入。

    仓库约定插件目录不加入 ``sys.path``，因此仍以生产模块名
    ``app.plugins.onebotmsg`` 执行源码；只有 ``app.*`` 缺失时才安装 stub。
    """
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


def host() -> Tuple[types.ModuleType, types.ModuleType]:
    """返回 ``(插件包, onebot 子模块)``，首次调用时解析。

    优先使用生产命名空间；只有在没有 MoviePilot 后端时才降级加载，保证纯逻辑
    在开发机上仍可自检。
    """
    global _HOST_KIND, _PLUGIN, _ONEBOT
    if _PLUGIN is not None and _ONEBOT is not None:
        return _PLUGIN, _ONEBOT

    try:
        plugin = _import_production_namespace()
        _HOST_KIND = f"{PLUGIN_NAMESPACE} 命名空间导入"
    except Exception as err:  # noqa: BLE001 - 无宿主入口时降级，原因写入日志备查
        LOGGER_RECORDS.append(f"debug: 命名空间导入不可用，改用内置加载：{err!r}")
        plugin = _import_without_host()
        _HOST_KIND = "内置文件加载（宿主入口不可用）"

    onebot = importlib.import_module(f"{PLUGIN_NAMESPACE}.onebot")
    _PLUGIN, _ONEBOT = plugin, onebot
    return plugin, onebot


# ------------------------------------------------------------------ HTTP 替身


def fake_urlopen_factory(calls: List[Dict[str, Any]], *, status: str = "ok",
                        retcode: int = 0, fail_actions: Any = ()) -> Callable:
    """构造 urlopen 替身：记录每次请求，并按需返回失败。"""

    class _FakeResponse:
        def __init__(self, payload: Dict[str, Any]):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    def _urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
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


# ------------------------------------------------------- 宿主领域对象替身


class _FileItem:
    """FileItem 替身，提供 model_dump 与 path。"""

    def __init__(self, path: str):
        self.path = path

    def model_dump(self) -> Dict[str, Any]:
        return {"path": self.path, "name": self.path.rsplit("/", 1)[-1]}


class _TransferInfo:
    """TransferInfo 替身，字段与宿主 V3 保持一致。"""

    def __init__(self, **kwargs: Any):
        self.success = kwargs.get("success", True)
        self.file_count = kwargs.get("file_count", 0)
        self.total_size = kwargs.get("total_size", 0)
        self.transfer_type = kwargs.get("transfer_type")
        self.target_item = kwargs.get("target_item")
        self.fail_list = kwargs.get("fail_list", [])
        self.message = kwargs.get("message")
        self.failure_stage = kwargs.get("failure_stage")
        self.recovery_action = kwargs.get("recovery_action")

    def model_dump(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "transfer_type": self.transfer_type,
            "target_item": self.target_item.model_dump() if self.target_item else None,
            "fail_list": self.fail_list,
            "message": self.message,
            "failure_stage": self.failure_stage,
            "recovery_action": self.recovery_action,
        }


class _MetaInfo:
    """MetaInfo 替身。"""

    def __init__(self, name: str, season: int, episode: int):
        self.name = name
        self.begin_season = season
        self.begin_episode = episode

    def model_dump(self) -> Dict[str, Any]:
        return {"name": self.name, "begin_season": self.begin_season,
                "begin_episode": self.begin_episode}


class _MediaInfo:
    """MediaInfo 替身，携带 V3 媒体身份。"""

    def __init__(self, **kwargs: Any):
        self.title = kwargs.get("title")
        self.year = kwargs.get("year")
        self.media_source = kwargs.get("media_source")
        self.media_id = kwargs.get("media_id")

    def model_dump(self) -> Dict[str, Any]:
        return {"title": self.title, "year": self.year,
                "media_source": self.media_source, "media_id": self.media_id}


# ------------------------------------------------------------------ 检查分组


def checks_protocol_helpers() -> None:
    """onebot.py 的地址、目标与分段工具。"""
    _plugin, onebot = host()
    group("1. onebot.py 基础工具")
    check(onebot.normalize_base_url("127.0.0.1:3000") == "http://127.0.0.1:3000",
          "缺少协议头时自动补 http://")
    check(onebot.normalize_base_url("http://a.b:3000/") == "http://a.b:3000",
          "去除结尾斜杠")
    check(onebot.normalize_base_url("") == "", "空地址返回空串")

    users, groups = onebot.parse_qq_targets("123456\n群:987654\ngroup:222")
    check(users == ["123456"], f"裸数字按私聊解析 -> {users}")
    check(groups == ["987654", "222"], f"群前缀解析 -> {groups}")

    users, groups = onebot.parse_qq_targets("private:1,好友:2")
    check(users == ["1", "2"] and groups == [], f"私聊前缀解析 -> {users}/{groups}")

    mapping = onebot.parse_user_target_map('{"admin": "123456", "alice": ["1", "群:2"]}')
    check(mapping.get("admin") == ["123456"], "JSON 用户映射解析")
    check(mapping.get("alice") == ["1", "群:2"], "JSON 用户映射数组解析")

    mapping = onebot.parse_user_target_map("admin=123456\nalice=1,2")
    check(mapping.get("admin") == ["123456"], "行式用户映射解析")
    check(mapping.get("alice") == ["1", "2"], "行式用户映射多目标解析")

    check(onebot.parse_targets("1,2 3\n4") == ["1", "2 3", "4"],
          "目标列表按逗号/换行拆分")

    segments = [
        onebot.build_text_segment("a" * 10),
        onebot.build_image_segment("http://img/1.jpg"),
        onebot.build_text_segment("b" * 10),
    ]
    chunks = onebot.split_segments(segments, 15)
    check(len(chunks) == 2, f"分段数量 -> {len(chunks)}")
    check(onebot.join_segment_texts([s for c in chunks for s in c]) == "a" * 10 + "b" * 10,
          "分段后文本内容完整")
    check(len(onebot.split_segments([onebot.build_text_segment("x" * 25)], 10)) == 3,
          "超长单段硬切为 3 段")


def checks_rate_limit_and_dedupe() -> None:
    """令牌桶与去重缓存。"""
    _plugin, onebot = host()
    group("2. 限流与去重")
    bucket = onebot.TokenBucket(rate=1000, capacity=1)
    check(all(bucket.acquire(timeout=1) for _ in range(50)), "令牌桶高速率下持续放行")

    dedupe = onebot.DedupeCache(ttl=30, maxsize=4)
    check(dedupe.seen("k1") is False, "首次出现返回 False")
    check(dedupe.seen("k1") is True, "重复出现返回 True")
    for index in range(10):
        dedupe.seen(f"extra{index}")
    check(len(dedupe) <= 4, f"去重缓存不超过上限 -> {len(dedupe)}")


def checks_client_calls() -> None:
    """OneBot 客户端动作、鉴权、失败判定与重试。"""
    _plugin, onebot = host()
    group("3. 客户端动作调用")
    calls: List[Dict[str, Any]] = []
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory(calls)):
        client = onebot.OneBotClient("http://127.0.0.1:3000", access_token="tok",
                                     rate_limit=1000, rate_burst=1000)
        check(client.send_private_msg("123456", [onebot.build_text_segment("hi")]).ok,
              "私聊发送成功")
        check(client.send_group_msg("999", "hello").ok, "群发送成功")

    check(calls and calls[0]["url"] == "http://127.0.0.1:3000/send_private_msg",
          f"动作路径拼接 -> {calls[0]['url'] if calls else '无调用'}")
    check(calls and calls[0]["params"]["user_id"] == "123456", "user_id 以字符串下发")
    check(calls and calls[0]["authorization"] == "Bearer tok", "Access Token 以 Bearer 下发")
    check(len(calls) > 1 and calls[1]["params"]["group_id"] == "999", "group_id 以字符串下发")

    calls.clear()
    with mock.patch("urllib.request.urlopen",
                    fake_urlopen_factory(calls, status="failed", retcode=100)):
        client = onebot.OneBotClient("http://127.0.0.1:3000", retries=3,
                                     rate_limit=1000, rate_burst=1000)
        result = client.send_private_msg("1", "x")
    check(not result.ok and result.retcode == 100, "retcode!=0 判定失败")
    check(len(calls) == 1, f"协议失败不重试 -> 调用 {len(calls)} 次")

    calls.clear()
    with mock.patch("urllib.request.urlopen",
                    fake_urlopen_factory(calls, fail_actions=("send_private_msg",))), \
            mock.patch("time.sleep", lambda *_: None):
        client = onebot.OneBotClient("http://127.0.0.1:3000", retries=2,
                                     rate_limit=1000, rate_burst=1000)
        result = client.send_private_msg("1", "x")
    check(not result.ok, "网络失败判定失败")
    check(len(calls) == 3, f"网络失败按 retries 重试 -> 调用 {len(calls)} 次")
    check(not onebot.OneBotClient("").call("get_login_info").ok, "未配置地址直接失败")


def checks_group_at_segment() -> None:
    """多目标批量发送与群内 @。"""
    _plugin, onebot = host()
    group("4. 组合发送与 @")
    calls: List[Dict[str, Any]] = []
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory(calls)):
        client = onebot.OneBotClient("http://127.0.0.1:3000",
                                     rate_limit=1000, rate_burst=1000)
        results = client.send_notification(
            message=[onebot.build_text_segment("body")],
            user_ids=["1"], group_ids=["2"], at_user="1",
        )
    check(len(results) == 2 and all(r.ok for r in results), "多目标批量发送")
    group_calls = [c for c in calls if c["action"] == "send_group_msg"]
    check(bool(group_calls) and group_calls[0]["params"]["message"][0]["type"] == "at",
          "@ 段插入群消息首位")
    check(bool(group_calls) and group_calls[0]["params"]["message"][0]["data"]["qq"] == "1",
          "@ 目标正确")


CONFIG = {
    "enabled": True,
    "api_url": "127.0.0.1:3000/",
    "access_token": "tok",
    "targets": "10001\n群:20002\ngroup:20003",
    "user_targets": "alice=30003",
    "msgtypes": ["Organize", "Download"],
    "send_image": True,
    "send_link": True,
    "rate_limit": 50,
    "retries": 0,
    "max_length": 200,
    "dedupe_seconds": 60,
}


def checks_lifecycle() -> None:
    """插件生命周期与配置解析。"""
    plugin, onebot = host()
    group("5. 插件主类：生命周期与配置解析")
    OneBotMsg = plugin.OneBotMsg
    check(not inspect.isabstract(OneBotMsg), "插件类已实现全部抽象方法")

    instance = OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    check(instance.get_state(), "启用且配置完整时 get_state 为真")
    check(instance._private_users == ["10001"], f"私聊目标 -> {instance._private_users}")
    check(instance._groups == ["20002", "20003"], f"群目标 -> {instance._groups}")
    check(instance._user_targets.get("alice") == ["30003"], "用户映射解析入配置")
    check(instance._client is not None
          and instance._client.base_url == "http://127.0.0.1:3000", "客户端地址规范化")

    first_worker = instance._worker
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    check(instance._worker is not first_worker, "重复初始化更换工作线程")
    check(not (first_worker and first_worker.is_alive()), "旧工作线程已退出")
    instance.stop_service()

    check(isinstance(onebot, types.ModuleType), "onebot 子模块按包内相对导入解析")


def checks_target_routing() -> None:
    """目标路由优先级。"""
    plugin, _onebot = host()
    group("6. 目标路由优先级")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    try:
        users, groups, _ = instance._resolve_targets()
        check(users == ["10001"] and groups == ["20002", "20003"], "无用户时使用默认目标")

        users, groups, at_user = instance._resolve_targets(username="alice")
        check(users == ["30003"] and at_user == "30003", "命中用户映射并设置 @")

        users, _groups, _ = instance._resolve_targets(username="bob")
        check(users == ["10001"], "未命中用户时回落默认目标")

        users, _groups, _ = instance._resolve_targets(targets={"qq": "40004", "telegram": "x"})
        check(users == ["40004"], "宿主 targets 中的 QQ 目标优先")

        users, _groups, _ = instance._resolve_targets(targets={"telegram": "x"})
        check(users == ["10001"], "targets 无 QQ 键时回落默认目标")

        _users, groups, _ = instance._resolve_targets(targets={"QQ": "群:50005"})
        check(groups == ["50005"], "targets 支持群前缀")
    finally:
        instance.stop_service()


def checks_type_filter() -> None:
    """消息类型过滤、宿主场景开关与 QQ 渠道禁用检测。"""
    plugin, _onebot = host()
    group("7. 消息类型过滤")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    try:
        check(instance._type_enabled("Organize"), "已勾选类型放行")
        check(not instance._type_enabled("Subscribe"), "未勾选类型拦截")
        check(not instance._type_enabled(None), "无类型且未勾选 Other 时拦截")

        instance._msgtypes = []
        check(not instance._type_enabled("Organize"), "未勾选任何场景时不推送")
        check(not instance._type_enabled(None), "未勾选任何场景时其它通知也不推送")

        with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
            instance.init_plugin({**CONFIG, "msgtypes": []})
        check(instance._msgtypes == [], "显式清空 msgtypes 不被默认值覆盖")
        with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
            instance.init_plugin({k: v for k, v in CONFIG.items() if k != "msgtypes"})
        check(instance._msgtypes == list(plugin._DEFAULT_TYPES),
              "缺少 msgtypes 配置时回落到默认场景")
        with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
            instance.init_plugin(CONFIG)
        check(instance._msgtypes == ["Organize", "Download"], "配置恢复成功")

        helper = plugin.OneBotMsg._host_scene_allows
        # 场景开关与渠道配置都通过 mock 注入，真实宿主下也能确定性地验证判定分支。
        with mock.patch.object(plugin.ServiceConfigHelper, "get_notification_switch",
                               side_effect=lambda mtype: {"Organize": ""}.get(
                                   getattr(mtype, "name", str(mtype)), "admin")):
            check(not helper("Organize"), "宿主场景开关为空时拦截")
        with mock.patch.object(plugin.ServiceConfigHelper, "get_notification_switch",
                               return_value="all"):
            check(helper("Organize"), "宿主场景为 all 时放行")
        with mock.patch.object(plugin.ServiceConfigHelper, "get_notification_switch",
                               return_value="admin"):
            check(helper("Organize"), "宿主场景为 admin 时放行")

        group("7b. 宿主 QQ 渠道禁用检测")
        disabled = plugin.OneBotMsg._qq_channel_disabled

        def _with_configs(configs: List[Any], name: str, expect: bool) -> None:
            """在指定渠道配置下验证禁用判定。"""
            with mock.patch.object(plugin.ServiceConfigHelper,
                                   "get_notification_configs", return_value=configs):
                check(disabled() is expect, name)

        _with_configs([], "宿主未配置通知渠道时不拦截", False)
        _with_configs([_ChannelConf("telegram", False)], "只有其它渠道时不拦截", False)
        _with_configs([_ChannelConf("qq", True)], "QQ 渠道启用时不拦截", False)
        _with_configs([_ChannelConf("qq", False)], "QQ 渠道禁用时拦截", True)
        _with_configs([_ChannelConf("qq", False), _ChannelConf("telegram", True)],
                      "QQ 渠道全部禁用时拦截", True)
        _with_configs([_ChannelConf("qq", False), _ChannelConf("QQBot", True)],
                      "存在任一启用的 QQ 渠道时不拦截", False)
    finally:
        instance.stop_service()


def _new_instance(config: Dict[str, Any] | None = None):
    """构造一个已初始化且 HTTP 被 mock 的插件实例，返回 ``(实例, 调用记录, 补丁栈)``。"""
    plugin, onebot = host()
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(config if config is not None else CONFIG)
    calls: List[Dict[str, Any]] = []
    stack = mock.patch("urllib.request.urlopen", fake_urlopen_factory(calls))
    stack.start()
    instance._client = onebot.OneBotClient(
        "http://127.0.0.1:3000", access_token="tok",
        rate_limit=1000, rate_burst=1000, dedupe_ttl=60,
    )
    return instance, calls, stack


def checks_notice_forwarding() -> None:
    """通知事件端到端转发、去重与过滤。"""
    plugin, onebot = host()
    group("8. 通知事件转发（端到端）")
    instance, calls, stack = _new_instance()
    try:
        event = plugin.Event(
            plugin.EventType.NoticeMessage,
            {
                "type": plugin.MessageType.Organize,
                "title": "【测试】影片入库",
                "text": "时间：2024-01-01 00:00:00\n季集：S01E01",
                "image": "https://img/poster.jpg",
                "link": "http://mp/#/media",
                "userid": "alice",
                "targets": None,
            },
        )
        instance.forward_notice(event)
        instance._queue.join()

        actions = sorted(call["action"] for call in calls)
        check(actions == ["send_private_msg"], f"命中用户映射后只发私聊 -> {actions}")
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(bool(private_calls) and private_calls[0]["params"]["user_id"] == "30003",
              "使用映射中的 QQ 号")
        segments = private_calls[0]["params"]["message"] if private_calls else []
        text = onebot.join_segment_texts(segments)
        check("✅" in text, "入库通知带 ✅ 图标")
        check("影片入库" in text, "标题进入消息")
        check("S01E01" in text, "正文进入消息")
        check("http://mp/#/media" in text, "链接进入消息")
        check(any(seg.get("type") == "image" for seg in segments), "图片段存在")
        check(instance.get_runtime_stats()["sent"] == 1, "发送统计累加")

        group("9. 去重：同一条通知不重复发送")
        before = len(calls)
        instance.forward_notice(event)
        instance._queue.join()
        check(len(calls) == before, "重复通知被去重，未产生新调用")
        check(instance.get_runtime_stats()["deduped"] >= 1, "去重计数增加")

        group("8b. 无用户通知分发到全部默认目标")
        calls.clear()
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"type": plugin.MessageType.Organize, "title": "【测试】系统级入库",
             "text": "无关联用户", "image": None, "link": None,
             "userid": None, "targets": None},
        ))
        instance._queue.join()
        actions = sorted(call["action"] for call in calls)
        check(actions.count("send_private_msg") == 1 and actions.count("send_group_msg") == 2,
              f"默认目标分发（私聊 1 + 群 2）-> {actions}")
        group_ids = sorted(c["params"]["group_id"] for c in calls
                           if c["action"] == "send_group_msg")
        check(group_ids == ["20002", "20003"], "群消息发往配置的群号")

        group("10. 未勾选类型不发送")
        before = len(calls)
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"type": plugin.MessageType.Subscribe, "title": "订阅", "text": "内容"},
        ))
        instance._queue.join()
        check(len(calls) == before, "未勾选类型未发送")

        group("11. 指定其它渠道的消息跳过")
        before = len(calls)
        instance.forward_notice(plugin.Event(
            plugin.EventType.NoticeMessage,
            {"channel": "Telegram", "type": plugin.MessageType.Organize,
             "title": "其它渠道", "text": "内容"},
        ))
        instance._queue.join()
        check(len(calls) == before, "指定 Telegram 渠道的通知被跳过")
    finally:
        stack.stop()
        instance.stop_service()


def checks_transfer_events() -> None:
    """整理完成/失败事件的结构化通知。"""
    plugin, onebot = host()
    group("12. 整理失败事件（dict 载荷）")
    instance, calls, stack = _new_instance()
    try:
        instance.on_transfer_failed(plugin.Event(
            plugin.EventType.TransferFailed,
            {
                "transferinfo": {"message": "目标路径不可写", "file_count": 3,
                                 "target_path": "/media/movies/x.mkv"},
                "meta": {"name": "某剧", "begin_season": 2, "begin_episode": 5},
                "mediainfo": {"title": "某剧", "year": "2024",
                              "media_source": "themoviedb", "media_id": "12345"},
            },
        ))
        instance._queue.join()
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        text = onebot.join_segment_texts(private_calls[0]["params"]["message"]) if private_calls else ""
        check("整理失败" in text, "失败标题正确")
        check("S02E05" in text, f"季集格式化正确 -> {text[:60]!r}")
        check("themoviedb:12345" in text, "媒体身份进入消息")
        check("目标路径不可写" in text, "失败原因进入消息")

        group("13. 无标题整理事件不发送")
        before = len(calls)
        instance._client.clear_dedupe()
        instance.on_transfer_complete(plugin.Event(
            plugin.EventType.TransferComplete, {"transferinfo": {"file_count": 1}}
        ))
        instance._queue.join()
        check(len(calls) == before, "缺少标题时不构造通知")

        group("13b. 真实宿主对象形态（pydantic 模型）")
        calls.clear()
        instance._client.clear_dedupe()
        instance.on_transfer_complete(plugin.Event(
            plugin.EventType.TransferComplete,
            {
                "transferinfo": _TransferInfo(
                    file_count=2, total_size=5 * 1024 ** 3, transfer_type="link",
                    target_item=_FileItem("/media/TV/Show/S01/E01.mkv"),
                ),
                "meta": _MetaInfo("真实对象剧集", 3, 7),
                "mediainfo": _MediaInfo(title="真实对象剧集", year="2025",
                                        media_source="themoviedb", media_id="999"),
            },
        ))
        instance._queue.join()
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        text = onebot.join_segment_texts(private_calls[0]["params"]["message"]) if private_calls else ""
        check("真实对象剧集" in text, "从 pydantic 模型读取标题")
        check("S03E07" in text, f"从 pydantic 模型读取季集 -> {text[:60]!r}")
        check("5.00 GB" in text, "体积格式化")
        check("/media/TV/Show/S01/E01.mkv" in text, "从 target_item.path 读取目标路径")
        check("方式：link" in text, "整理方式进入消息")

        calls.clear()
        instance._client.clear_dedupe()
        instance.on_transfer_failed(plugin.Event(
            plugin.EventType.TransferFailed,
            {
                "transferinfo": _TransferInfo(
                    success=False, file_count=1, message="目标存储不可写",
                    failure_stage="write_target", recovery_action="检查媒体库目录权限",
                    fail_list=["a.mkv"],
                ),
                "meta": _MetaInfo("失败剧集", 1, 2),
                "mediainfo": _MediaInfo(title="失败剧集", year="2025",
                                        media_source="themoviedb", media_id="111"),
            },
        ))
        instance._queue.join()
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        text = onebot.join_segment_texts(private_calls[0]["params"]["message"]) if private_calls else ""
        check("阶段：write_target" in text, "失败阶段进入消息")
        check("目标存储不可写" in text, "失败原因进入消息")
        check("建议：检查媒体库目录权限" in text, "建议动作进入消息")
        check("失败文件：1 个" in text, "失败文件数进入消息")
    finally:
        stack.stop()
        instance.stop_service()


def checks_api_and_commands() -> None:
    """测试消息、自定义推送与远程命令回执。"""
    plugin, onebot = host()
    group("14. 测试消息与自定义推送")
    instance, calls, stack = _new_instance()
    try:
        calls.clear()
        result = instance.send_test_message()
        check(result.success and result.targets == 3,
              f"测试消息发往全部默认目标 -> {result.message}")
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(bool(private_calls) and "OneBot 通知测试" in onebot.join_segment_texts(
            private_calls[0]["params"]["message"]), "测试消息内容正确")

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

        group("14b. 远程命令回执")
        calls.clear()
        instance.handle_command(plugin.Event(
            plugin.EventType.PluginAction,
            {"action": "onebot_status", "user": "40004"},
        ))
        instance._queue.join()
        private_calls = [c for c in calls if c["action"] == "send_private_msg"]
        check(len(private_calls) == 1 and private_calls[0]["params"]["user_id"] == "40004",
              "命令回执按发起会话下发")
        check(bool(private_calls) and "OneBot 通知插件运行中" in onebot.join_segment_texts(
            private_calls[0]["params"]["message"]), "状态回执内容正确")

        calls.clear()
        instance.handle_command(plugin.Event(
            plugin.EventType.PluginAction,
            {"action": "onebot_status", "group_id": "70007"},
        ))
        instance._queue.join()
        group_calls = [c for c in calls if c["action"] == "send_group_msg"]
        check(len(group_calls) == 1 and group_calls[0]["params"]["group_id"] == "70007",
              "群内命令回执发回原群")

        calls.clear()
        instance.handle_command(plugin.Event(
            plugin.EventType.PluginAction, {"action": "other_plugin_action"},
        ))
        instance._queue.join()
        check(not calls, "其它插件的 action 被忽略")
    finally:
        stack.stop()
        instance.stop_service()


def checks_degraded_states() -> None:
    """停用、配置不全与资源释放。"""
    plugin, _onebot = host()
    group("15. 停用与资源释放")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    instance.stop_service()
    check(instance._worker is None, "停止后清空工作线程引用")
    check(not instance._accepting, "停止后不再接收新通知")
    instance.stop_service()
    check(True, "重复 stop_service 不报错")

    disabled = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        disabled.init_plugin({"enabled": False, "api_url": "http://x", "targets": "1"})
    check(not disabled.get_state(), "未启用时 get_state 为假")

    incomplete = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        incomplete.init_plugin({"enabled": True, "api_url": "", "targets": "1"})
    check(not incomplete.get_state(), "缺少接口地址时 get_state 为假")
    incomplete.stop_service()

    empty = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        empty.init_plugin({"enabled": True, "api_url": "http://x", "targets": ""})
    check(not empty.get_state(), "缺少目标时 get_state 为假")
    empty.stop_service()


def checks_form_contract() -> None:
    """配置表单与详情页元数据一致性。"""
    plugin, onebot = host()
    group("16. 表单元数据一致性")
    instance = plugin.OneBotMsg()
    with mock.patch("urllib.request.urlopen", fake_urlopen_factory([])):
        instance.init_plugin(CONFIG)
    try:
        form, defaults = instance.get_form()
        check(isinstance(form, list) and bool(form), "get_form 返回页面配置")
        model_keys = set(defaults.keys())
        for required in ("enabled", "api_url", "access_token", "targets", "msgtypes"):
            check(required in model_keys, f"默认配置包含 {required}")
        rendered = json.dumps(form, ensure_ascii=False)
        for model_field in ("enabled", "api_url", "access_token", "targets",
                            "user_targets", "msgtypes", "send_image", "send_link",
                            "at_in_group", "title_prefix", "max_length",
                            "dedupe_seconds", "rate_limit", "retries"):
            check(f'"model": "{model_field}"' in rendered, f"表单项绑定 {model_field}")
        check(bool(instance.get_api()) and bool(instance.get_command()),
              "API 与远程命令已声明")
        check(instance.get_page() is not None, "详情页可渲染")

        group("17. 消息长度截断")
        instance._max_length = 120
        segments = instance._build_segments(
            emoji="📢", title="标题", text="长" * 500, image=None, link="http://a"
        )
        text = onebot.join_segment_texts(segments)
        check(len(text) < 400, f"超长正文被截断 -> {len(text)}")
        check("..." in text, "截断带省略号")
    finally:
        instance.stop_service()


def checks_plugin_metadata() -> None:
    """版本一致性与目录命名约定。"""
    plugin, _onebot = host()
    group("18. 插件元数据与目录约定")
    plugin_file = Path(plugin.__file__).resolve()
    check(plugin_file.parent.name == PLUGIN_ID, "插件目录名与类名小写一致")
    check(plugin_file.name == "__init__.py", "主类定义在 __init__.py")
    check((plugin_file.parent / "README.md").is_file(), "插件目录包含 README.md")
    check(plugin.__name__ == PLUGIN_NAMESPACE, "按生产命名空间 app.plugins.onebotmsg 加载")
    check(plugin.OneBotMsg.plugin_config_prefix == f"{PLUGIN_ID}_",
          "配置前缀与插件 ID 匹配")
    check(bool(plugin.OneBotMsg.plugin_version), "声明了 plugin_version")
    check("命名空间导入" in _HOST_KIND or "内置文件加载" in _HOST_KIND,
          f"插件加载方式可识别：{_HOST_KIND}")


ALL_CHECKS: Tuple[Callable[[], None], ...] = (
    checks_plugin_metadata,
    checks_protocol_helpers,
    checks_rate_limit_and_dedupe,
    checks_client_calls,
    checks_group_at_segment,
    checks_lifecycle,
    checks_target_routing,
    checks_type_filter,
    checks_notice_forwarding,
    checks_transfer_events,
    checks_api_and_commands,
    checks_degraded_states,
    checks_form_contract,
)


def run_all_checks() -> List[Tuple[str, str]]:
    """执行全部检查并返回失败列表。"""
    CHECKS.clear()
    _safe_print("=" * 64)
    _safe_print("OneBot 通知插件回归测试")
    _safe_print("=" * 64)
    # 先解析宿主，后续分组都复用同一份模块与实例语义。
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


def main() -> int:
    """独立运行入口：打印逐项结果并返回退出码。"""
    failed = run_all_checks()
    passed = len(CHECKS) - len(failed)
    _safe_print("\n" + "=" * 64)
    if failed:
        _safe_print(f"结果：{passed}/{len(CHECKS)} 通过，{len(failed)} 项失败")
        for group_name, name in failed:
            _safe_print(f"  - [{group_name}] {name}")
        return 1
    _safe_print(f"结果：全部 {len(CHECKS)} 项检查通过")
    return 0


# ------------------------------------------------------------ pytest 入口


def test_onebotmsg_regression() -> None:
    """插件回归：一个用例内跑完全部分组，失败时给出逐项清单。

    不请求任何自定义 fixture：本仓 ``tests/conftest.py`` 的 ``pytest_configure`` 已在
    收集用例前完成 v3 引导（隔离 CONFIG_DIR、建表、把 ``plugins.v3/`` 注入
    ``app.plugins.__path__``），autouse 的 ``configure_plugin_chain_context`` 负责装配
    插件基类构造所需的 Chain 上下文。
    """
    failed = run_all_checks()
    detail = "; ".join(f"[{group_name}] {name}" for group_name, name in failed)
    assert not failed, f"{len(failed)} 项检查失败：{detail}"
    assert CHECKS, "未执行任何检查"


if __name__ == "__main__":
    raise SystemExit(main())
