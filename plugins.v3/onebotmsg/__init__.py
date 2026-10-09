"""MoviePilot V3 插件：OneBot v11（NapCat）消息通知。

插件职责：

1. 订阅宿主 ``EventType.NoticeMessage``，把 MoviePilot 全部通知（入库/整理、
   下载、订阅、站点、媒体服务器、插件、系统错误等）转发到 QQ；
2. 订阅 ``TransferComplete`` / ``TransferFailed`` / ``SubscribeComplete`` 等事件，
   在通知之外补充结构化上下文（分类、季集、文件数、来源站点等）；
3. 通过 OneBot v11 正向 HTTP 接口（NapCat 默认端口 3000）下发私聊与群聊消息，
   支持图片与链接，可选群内 @；
4. 内置发送队列、限流、去重与重试，避免消息轰炸与 NapCat 风控；
5. 提供远程命令与插件 API，便于在 QQ 里查询状态、在外部系统主动推送。

宿主合同来自 MoviePilot V3：
``app.sdk.plugin._PluginBase``、``app.sdk.events``、``app.sdk.services``、
``app.sdk.logging``、``app.schemas.types.MessageType`` 与 ``EventType``。
"""

import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel

from app.schemas.types import EventType, MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import ServiceConfigHelper

try:  # pragma: no cover - 兼容宿主未暴露该入口的早期 V3 版本
    from app.sdk.media import resolve_media_identity
except Exception:  # noqa: BLE001
    resolve_media_identity = None  # type: ignore[assignment]

from .onebot import (
    OneBotClient,
    OneBotResponse,
    build_image_segment,
    build_text_segment,
    parse_qq_targets,
    parse_user_target_map,
    shorten,
    split_segments,
)


# 消息类型 -> 展示名与 emoji。键使用 MessageType 成员名，便于配置跨版本稳定。
_MESSAGE_TYPE_STYLE: Dict[str, Tuple[str, str]] = {
    "Download": ("资源下载", "📥"),
    "Organize": ("整理入库", "✅"),
    "Subscribe": ("订阅", "🔔"),
    "SiteMessage": ("站点", "🌐"),
    "MediaServer": ("媒体服务器", "🎬"),
    "Manual": ("手动处理", "🛠️"),
    "Plugin": ("插件", "🧩"),
    "Agent": ("智能体", "🤖"),
    "Other": ("其它", "📢"),
}

# 默认开启的消息类型（枚举成员名）。
_DEFAULT_TYPES = ["Organize", "Download", "Subscribe", "Manual", "SiteMessage", "Plugin"]

# 发送队列上限。超出时丢弃最旧的消息，避免宿主事件线程被长期阻塞。
_QUEUE_MAXSIZE = 512

# 单条通知文本的默认截断长度，避免超出 QQ 单条消息限制。
_DEFAULT_MAX_LENGTH = 1200

# 队列工作线程的轮询步长（秒），用于及时响应停止请求。
_WORKER_IDLE_STEP = 0.5


class PushResult(BaseModel):
    """手动推送接口的返回模型。"""

    success: bool
    message: str
    targets: int = 0


class OneBotMsg(_PluginBase):
    """OneBot v11 通知插件。"""

    # 插件名称
    plugin_name = "OneBot v11 通知"
    # 插件描述
    plugin_desc = "通过 OneBot v11（NapCat）向 QQ 私聊/群聊推送 MoviePilot 通知，支持入库、下载、订阅、站点、系统错误等场景。"
    # 插件图标：使用 NapCat 官方 Logo 的完整 HTTPS 地址。
    # 插件市场索引与插件类声明同一个字段，两者必须保持同一形式。
    plugin_icon = "https://raw.githubusercontent.com/NapNeko/NapCatQQ/main/logo.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "TANZZZ"
    # 作者主页
    author_url = "https://github.com/NapNeko/NapCatQQ"
    # 插件配置项ID前缀
    plugin_config_prefix = "onebotmsg_"
    # 加载顺序（通知类插件需要早于业务事件加载）
    plugin_order = 25
    # 可使用的用户级别：仅管理员
    auth_level = 1

    # ------------------------------------------------------------------ 状态

    # 插件是否启用
    _enabled: bool = False
    # OneBot 接口地址与鉴权
    _api_url: str = ""
    _access_token: str = ""
    # 目标：私聊 QQ 号与群号
    _private_users: List[str] = []
    _groups: List[str] = []
    # MoviePilot 用户名 -> QQ 目标映射
    _user_targets: Dict[str, List[str]] = {}
    # 页面配置的原始目标文本，用于回显
    _targets_text: str = ""
    _user_map_text: str = ""
    # 内容开关
    _msgtypes: List[str] = []
    _send_image: bool = True
    _send_link: bool = True
    _at_in_group: bool = False
    _title_prefix: str = ""
    _max_length: int = _DEFAULT_MAX_LENGTH
    _dedupe_seconds: int = 20
    _rate_limit: float = 3.0
    # 失败重试次数
    _retries: int = 2

    # 运行期资源
    _client: Optional[OneBotClient] = None
    _queue: "queue.Queue[Dict[str, Any]]" = None  # type: ignore[assignment]
    _worker: Optional[threading.Thread] = None
    _stop_event: threading.Event = None  # type: ignore[assignment]
    _lifecycle_lock: threading.Lock = None  # type: ignore[assignment]
    _accepting: bool = False
    # 运行统计
    _stats: Dict[str, int] = None  # type: ignore[assignment]

    def __init__(self) -> None:
        """建立实例级生命周期状态，避免热重载复用旧的队列与线程引用。"""
        super().__init__()
        self._reset_runtime()

    def _reset_runtime(self) -> None:
        """初始化运行期容器；可在 stop_service 后安全重复调用。"""
        self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._worker = None
        self._accepting = False
        self._stats = {
            "received": 0,
            "sent": 0,
            "failed": 0,
            "dropped": 0,
            "filtered": 0,
            "deduped": 0,
        }

    # ------------------------------------------------------------- 生命周期

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置并启动发送队列。

        必须允许重复调用：宿主保存配置、热重载、创建插件分身都会重新执行。
        """
        config = config or {}
        self._stop_worker()
        self._reset_runtime()

        self._enabled = bool(config.get("enabled"))
        self._api_url = str(config.get("api_url") or "").strip()
        self._access_token = str(config.get("access_token") or "").strip()
        self._targets_text = str(config.get("targets") or "")
        self._user_map_text = str(config.get("user_targets") or "")
        # 兼容旧配置：分别配置私聊与群聊时优先使用显式字段。
        self._private_users = _as_list(config.get("private_users"))
        self._groups = _as_list(config.get("groups"))
        if not self._private_users and not self._groups and self._targets_text:
            self._private_users, self._groups = parse_qq_targets(self._targets_text)
        self._user_targets = parse_user_target_map(self._user_map_text)

        # 未配置过 msgtypes（None）时使用默认场景；显式清空列表表示按「全部通知
        # 都不推送」处理，不回落到默认值，避免用户的关闭意图被静默覆盖。
        raw_msgtypes = config.get("msgtypes")
        self._msgtypes = list(_DEFAULT_TYPES) if raw_msgtypes is None \
            else _as_list(raw_msgtypes)
        self._send_image = bool(config.get("send_image", True))
        self._send_link = bool(config.get("send_link", True))
        self._at_in_group = bool(config.get("at_in_group"))
        self._title_prefix = str(config.get("title_prefix") or "")
        self._max_length = _as_int(config.get("max_length"), _DEFAULT_MAX_LENGTH)
        self._dedupe_seconds = _as_int(config.get("dedupe_seconds"), 20)
        self._rate_limit = _as_float(config.get("rate_limit"), 3.0)
        self._retries = _as_int(config.get("retries"), 2)

        self._client = OneBotClient(
            base_url=self._api_url,
            access_token=self._access_token,
            retries=self._retries,
            rate_limit=self._rate_limit,
            rate_burst=max(self._rate_limit * 2, 2.0),
            dedupe_ttl=max(self._dedupe_seconds, 0),
        )

        if not self._enabled:
            logger.info("OneBot v11 通知插件未启用")
            return

        self._start_worker()
        logger.info(
            f"OneBot v11 通知插件已启用：接口={self._api_url or '未配置'}，"
            f"私聊目标={len(self._private_users)}，群目标={len(self._groups)}，"
            f"用户映射={len(self._user_targets)}"
        )

    def get_state(self) -> bool:
        """返回插件是否处于可用状态。

        仅有开关而不填目标或接口时不算可用，避免配置半成品时静默丢消息。
        """
        return bool(
            self._enabled
            and self._api_url
            and (self._private_users or self._groups or self._user_targets)
        )

    def stop_service(self) -> None:
        """停止插件并释放队列、线程与去重缓存，可安全重复调用。"""
        self._stop_worker()
        self._accepting = False
        if self._client is not None:
            try:
                self._client.clear_dedupe()
            except Exception as err:  # noqa: BLE001
                logger.debug(f"清理去重缓存失败：{err}")
        logger.info("OneBot v11 通知插件已停止")

    def close(self) -> None:
        """兼容新版宿主的第一阶段关闭钩子。"""
        self._stop_worker()
        self._accepting = False

    def _start_worker(self) -> None:
        """启动发送队列工作线程。"""
        with self._lifecycle_lock:
            self._stop_event.clear()
            self._accepting = True
            worker = threading.Thread(
                target=self._worker_loop,
                name="OneBotMsgSender",
                daemon=True,
            )
            self._worker = worker
        worker.start()

    def _stop_worker(self) -> None:
        """请求工作线程退出并等待其收尾。"""
        with self._lifecycle_lock:
            self._accepting = False
            worker = self._worker
            self._worker = None
            stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        if worker is not None and worker.is_alive() \
                and worker is not threading.current_thread():
            worker.join(timeout=5.0)

    def _worker_loop(self) -> None:
        """串行消费发送队列，保证消息顺序并天然限速。"""
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=_WORKER_IDLE_STEP)
            except queue.Empty:
                continue
            try:
                self._dispatch_task(task)
            except Exception as err:  # noqa: BLE001
                # 单条消息失败不应终止整个工作线程。
                self._bump("failed")
                logger.error(f"OneBot 通知发送异常：{err}")
            finally:
                self._queue.task_done()

    def _dispatch_task(self, task: Dict[str, Any]) -> None:
        """执行一条发送任务并记录统计。"""
        client = self._client
        if client is None:
            return
        segments = task.get("segments") or []
        user_ids = task.get("user_ids") or []
        group_ids = task.get("group_ids") or []
        at_user = task.get("at_user")
        title = task.get("title") or "MoviePilot 通知"

        if not user_ids and not group_ids:
            self._bump("dropped")
            logger.warning(f"OneBot 通知没有可用目标，已丢弃：{title}")
            return

        chunks = split_segments(segments, self._max_length)
        results: List[OneBotResponse] = []
        for index, chunk in enumerate(chunks):
            # 多段时在段落首行标注序号，便于在聊天窗口中区分。
            payload: Union[str, List[Dict[str, Any]]] = chunk
            if len(chunks) > 1 and isinstance(chunk, list) and chunk:
                chunk = list(chunk)
                chunk[0] = build_text_segment(
                    f"({index + 1}/{len(chunks)}) {_segment_text(chunk[0])}"
                )
                payload = chunk
            results.extend(client.send_notification(
                message=payload,
                user_ids=user_ids,
                group_ids=group_ids,
                at_user=at_user if self._at_in_group else None,
            ))

        ok_count = sum(1 for result in results if result.ok)
        if results and ok_count == len(results):
            self._bump("sent")
            logger.info(f"OneBot 通知已发送：{title}（{len(results)} 个目标）")
        else:
            self._bump("failed")
            errors = [f"{r.action}: {r.error}" for r in results if not r.ok]
            logger.warn(
                f"OneBot 通知部分或全部发送失败：{title}；"
                f"成功 {ok_count}/{len(results)}；{'; '.join(errors[:3])}"
            )

    def _bump(self, key: str, delta: int = 1) -> None:
        """累加运行统计。"""
        if self._stats is None:
            return
        self._stats[key] = self._stats.get(key, 0) + delta

    # --------------------------------------------------------- 事件与转发

    @eventmanager.register(EventType.NoticeMessage)
    def forward_notice(self, event: Event) -> None:
        """把宿主通知转发到 QQ。

        ``event_data`` 结构来自 ``MessageChain.post_message``：
        ``{channel, source, mtype, ctype, title, text, image, link, userid,
        username, targets, ... , type}``，其中 ``type`` 与 ``mtype`` 相同。
        """
        if not self.get_state():
            return
        data = event.event_data or {}
        if not isinstance(data, dict):
            return

        self._bump("received")

        # 指定了其它渠道的消息不再重复转发，避免与具名渠道重复推送。
        channel = data.get("channel")
        if channel is not None:
            channel_value = getattr(channel, "value", channel)
            # 允许 QQ 渠道的消息继续走本插件，其余具名渠道跳过。
            if channel_value not in ("QQ", "QQBot", "qqbot"):
                logger.debug(f"消息已指定渠道 {channel_value}，OneBot 通知跳过")
                return

        mtype = data.get("type") or data.get("mtype")
        type_name = _type_name(mtype)
        if not self._type_enabled(type_name):
            self._bump("filtered")
            logger.debug(f"消息类型 {type_name} 未开启转发")
            return

        title = str(data.get("title") or "").strip()
        text = str(data.get("text") or "").strip()
        if not title and not text:
            logger.debug("通知标题与正文均为空，跳过转发")
            return

        # 同一事件可能因为路由（admin/user/all）被派发多次，这里做短期去重。
        dedupe_key = _dedupe_key(type_name, title, text, data.get("image"))
        client = self._client
        if client is not None and client.dedupe.seen(dedupe_key):
            self._bump("deduped")
            logger.debug(f"检测到重复通知，已跳过：{title}")
            return

        image = data.get("image") if self._send_image else None
        link = data.get("link") if self._send_link else None

        segments = self._build_segments(
            emoji=_type_emoji(type_name),
            title=title,
            text=text,
            image=str(image) if image else None,
            link=str(link) if link else None,
        )

        self._enqueue(
            title=title or "MoviePilot 通知",
            segments=segments,
            userid=data.get("userid"),
            username=data.get("username"),
            targets=data.get("targets"),
        )

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """整理完成时补充一条结构化入库通知（含分类与季集信息）。

        宿主在整理完成后会自行发送 ``Organize`` 类型通知，本方法只负责在
        事件携带了宿主通知未覆盖的字段时补一条精简卡片；未开启 ``Organize``
        类型或缺少必要信息时静默跳过，避免重复刷屏。
        """
        self._handle_transfer_event(event, success=True)

    @eventmanager.register(EventType.TransferFailed)
    def on_transfer_failed(self, event: Event) -> None:
        """整理失败时推送一条提醒，便于及时人工介入。"""
        self._handle_transfer_event(event, success=False)

    def _handle_transfer_event(self, event: Event, success: bool) -> None:
        """把整理事件转换为通知卡片。"""
        if not self.get_state() or not self._type_enabled("Organize"):
            return
        data = event.event_data
        if not isinstance(data, dict) or not data:
            return

        info = data.get("transferinfo") or data.get("transfer_info") or {}
        meta = data.get("meta") or {}
        media = data.get("mediainfo") or data.get("media_info") or {}
        if not isinstance(info, dict):
            # TransferInfo 是 pydantic 模型，统一转换为字典后按字段读取。
            info = _object_to_dict(info)
        if not isinstance(media, dict):
            media = _object_to_dict(media)
        if not isinstance(meta, dict):
            meta = _object_to_dict(meta)

        title = str(media.get("title") or meta.get("name") or "").strip()
        year = str(media.get("year") or meta.get("year") or "").strip()
        heading = f"{title}{f' ({year})' if year else ''}".strip()
        if not heading:
            # 没有可用标题时不擅自构造通知，交由宿主原生通知处理。
            return

        lines: List[str] = []
        season = meta.get("begin_season")
        episode = meta.get("begin_episode")
        if season is not None or episode is not None:
            lines.append(f"季集：{_season_episode(season, episode)}")
        category = media.get("category") or media.get("library_category")
        if category:
            lines.append(f"分类：{category}")
        media_source, media_id = _media_identity(media, meta)
        if media_source and media_id:
            lines.append(f"来源：{media_source}:{media_id}")
        file_count = info.get("file_count")
        if file_count:
            lines.append(f"文件数：{file_count}")
        total_size = info.get("total_size")
        if total_size:
            lines.append(f"体积：{_format_size(total_size)}")
        target_path = _first_value(
            info, "target_path", "target", "target_item", "target_diritem"
        )
        if target_path:
            lines.append(f"目标：{shorten(str(target_path), 140)}")
        transfer_type = info.get("transfer_type")
        if transfer_type:
            lines.append(f"方式：{transfer_type}")

        # 失败场景补充阶段、原因与建议动作，便于直接照做。
        stage = info.get("failure_stage")
        if stage and not success:
            lines.append(f"阶段：{stage}")
        error = info.get("message") or info.get("error")
        if error and not success:
            lines.append(f"原因：{shorten(str(error), 200)}")
        recovery = info.get("recovery_action")
        if recovery and not success:
            lines.append(f"建议：{shorten(str(recovery), 160)}")
        fail_list = info.get("fail_list")
        if fail_list and not success:
            lines.append(f"失败文件：{len(fail_list)} 个")

        emoji = "✅" if success else "❌"
        title_text = f"{emoji} {'整理入库' if success else '整理失败'} {heading}"
        if not lines:
            lines.append("宿主持久化整理任务已完成")

        # 标题已包含状态图标，不再重复插入 emoji。
        segments = self._build_segments(
            emoji="",
            title=title_text,
            text="\n".join(lines),
            image=None,
            link=str(info.get("link") or "") or None,
        )
        self._enqueue(
            title=title_text,
            segments=segments,
            userid=None,
            username=None,
            targets=None,
        )

    # ------------------------------------------------------------ 目标解析

    def _resolve_targets(
        self,
        userid: Optional[Union[str, int]] = None,
        username: Optional[Union[str, int]] = None,
        targets: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str], Optional[str]]:
        """解析本次通知的私聊与群聊目标。

        优先级：

        1. 消息自带的 ``targets`` 中 QQ 渠道的绑定（用户在用户管理里配置的 QQ 号）；
        2. 插件配置的用户名 -> QQ 目标映射；
        3. 插件配置的全局默认目标。

        返回 ``(私聊列表, 群聊列表, @ 的 QQ 号)``。
        """
        users: List[str] = []
        groups: List[str] = []
        at_user: Optional[str] = None

        # 1. 宿主路由传入的渠道目标
        raw_target = _pick_target(targets)
        if raw_target:
            target_users, target_groups = parse_qq_targets(raw_target)
            users.extend(target_users)
            groups.extend(target_groups)

        # 2. 插件内的用户映射（username 优先，其次 userid）
        if not users and not groups:
            for key in (username, userid):
                if key is None:
                    continue
                mapped = self._user_targets.get(str(key))
                if not mapped:
                    continue
                mapped_users, mapped_groups = parse_qq_targets(mapped)
                users.extend(mapped_users)
                groups.extend(mapped_groups)
                at_user = mapped_users[0] if mapped_users else None
                break

        # 3. 全局默认目标
        if not users and not groups:
            users.extend(self._private_users)
            groups.extend(self._groups)

        return _unique(users), _unique(groups), at_user

    def _enqueue(self, title: str, segments: List[Dict[str, Any]],
                 userid: Optional[Union[str, int]],
                 username: Optional[Union[str, int]],
                 targets: Optional[Dict[str, Any]]) -> None:
        """解析目标后把一条通知放入发送队列。"""
        if not self._accepting:
            self._bump("dropped")
            logger.debug(f"插件未处于接收状态，丢弃通知：{title}")
            return

        users, groups, at_user = self._resolve_targets(userid, username, targets)
        if not users and not groups:
            self._bump("dropped")
            logger.warning(f"没有可用的 QQ 目标，通知未发送：{title}")
            return

        task = {
            "title": title,
            "segments": segments,
            "user_ids": users,
            "group_ids": groups,
            "at_user": at_user,
            "enqueued_at": time.time(),
        }
        self._queue_put(task, title)

    # ------------------------------------------------------------ 消息构造

    def _build_segments(self, emoji: str, title: str, text: Optional[str],
                        image: Optional[str], link: Optional[str],
                        max_text: int = 0) -> List[Dict[str, Any]]:
        """构造 OneBot 消息段列表：标题、正文、图片、链接。"""
        prefix = f"{emoji} " if emoji else ""
        heading = f"{prefix}{title}".strip()
        if self._title_prefix:
            heading = f"{self._title_prefix} {heading}".strip()

        lines: List[str] = [heading]
        body = str(text or "").strip()
        if body:
            limit = max_text or self._max_length
            # 为标题与链接预留空间，避免整段消息超长被 NapCat 拒绝。
            lines.append(shorten(body, max(limit - len(heading) - 64, 120)))
        if link:
            lines.append(f"🔗 {link}")

        segments: List[Dict[str, Any]] = [build_text_segment("\n".join(lines))]
        if image:
            segments.append(build_image_segment(image))
        return segments

    # --------------------------------------------------------------- 命令

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册插件远程命令。"""
        return [
            {
                "cmd": "/onebot_test",
                "event": EventType.PluginAction,
                "desc": "OneBot通知：发送测试消息",
                "category": "插件命令",
                "data": {"action": "onebot_test"},
            },
            {
                "cmd": "/onebot_status",
                "event": EventType.PluginAction,
                "desc": "OneBot通知：查看运行状态",
                "category": "插件命令",
                "data": {"action": "onebot_status"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """处理本插件注册的远程命令，并回执执行结果。"""
        data = event.event_data or {}
        if not isinstance(data, dict):
            return
        action = data.get("action")
        if action not in ("onebot_test", "onebot_status"):
            return
        if not self.get_state():
            logger.warning(f"OneBot 通知插件未启用，忽略命令 {action}")
            return

        if action == "onebot_status":
            text = self._status_text()
        else:
            text = self.send_test_message().message

        # 回执优先发回下命令的会话，无法识别时使用插件配置的目标。
        users, groups, _ = self._resolve_targets()
        sender_user = data.get("user") or data.get("userid")
        sender_group = data.get("group_id")
        if sender_user or sender_group:
            users = [str(sender_user)] if sender_user else []
            groups = [str(sender_group)] if sender_group else []

        self._enqueue_to(
            title="OneBot 通知插件",
            segments=[build_text_segment(f"🧩 {text}")],
            user_ids=users,
            group_ids=groups,
        )

    def _enqueue_to(self, title: str, segments: List[Dict[str, Any]],
                    user_ids: List[str], group_ids: List[str]) -> None:
        """把一条已经确定目标的消息放入发送队列。"""
        if not self._accepting:
            self._bump("dropped")
            logger.debug(f"插件未处于接收状态，丢弃通知：{title}")
            return
        if not user_ids and not group_ids:
            self._bump("dropped")
            logger.warning(f"没有可用的 QQ 目标，通知未发送：{title}")
            return
        task = {
            "title": title,
            "segments": segments,
            "user_ids": user_ids,
            "group_ids": group_ids,
            "at_user": None,
            "enqueued_at": time.time(),
        }
        self._queue_put(task, title)

    def _queue_put(self, task: Dict[str, Any], title: str) -> None:
        """入队并在队列满时丢弃最旧任务后重试，绝不阻塞调用线程。"""
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self._bump("dropped")
                logger.warning(f"发送队列已满，丢弃最旧通知：{dropped.get('title')}")
            except queue.Empty:  # pragma: no cover - 竞态兜底
                pass
            try:
                self._queue.put_nowait(task)
            except queue.Full:  # pragma: no cover - 竞态兜底
                self._bump("dropped")
                logger.warning(f"发送队列繁忙，通知被丢弃：{title}")

    def _status_text(self) -> str:
        """生成运行状态摘要。"""
        stats = self._stats or {}
        return (
            "OneBot 通知插件运行中\n"
            f"接口：{self._api_url or '未配置'}\n"
            f"私聊目标：{len(self._private_users)} 个；群目标：{len(self._groups)} 个；"
            f"用户映射：{len(self._user_targets)} 条\n"
            f"队列：{self._queue.qsize()} 待发送\n"
            f"统计：接收 {stats.get('received', 0)}，"
            f"发送成功 {stats.get('sent', 0)}，失败 {stats.get('failed', 0)}，"
            f"过滤 {stats.get('filtered', 0)}，去重 {stats.get('deduped', 0)}，"
            f"丢弃 {stats.get('dropped', 0)}"
        )

    # ---------------------------------------------------------------- API

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API。"""
        return [
            {
                "path": "/test",
                "endpoint": self.send_test_message,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "发送测试通知",
                "response_model": PushResult,
            },
            {
                "path": "/push",
                "endpoint": self.push_custom_message,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "推送自定义通知",
                "response_model": PushResult,
            },
            {
                "path": "/status",
                "endpoint": self.get_runtime_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询插件运行状态",
                "response_model": PushResult,
            },
        ]

    def send_test_message(self, payload: Optional[dict] = None) -> PushResult:
        """发送一条测试消息，验证接口与目标配置是否正确。"""
        if not self._enabled:
            return PushResult(success=False, message="插件未启用")
        users, groups, at_user = self._resolve_targets()
        if not self._api_url:
            return PushResult(success=False, message="未配置 OneBot 接口地址")
        if not users and not groups:
            return PushResult(success=False, message="未配置任何 QQ 目标")

        # 测试消息直接同步发送，便于页面立即看到真实结果。
        client = OneBotClient(
            base_url=self._api_url,
            access_token=self._access_token,
            retries=self._retries,
            rate_limit=self._rate_limit,
        )
        text = (
            "✅ OneBot 通知测试\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"发送者：MoviePilot {self.plugin_name} v{self.plugin_version}"
        )
        results = client.send_notification(
            message=[build_text_segment(text)],
            user_ids=users,
            group_ids=groups,
            at_user=at_user if self._at_in_group else None,
        )
        ok_count = sum(1 for result in results if result.ok)
        if results and ok_count == len(results):
            return PushResult(success=True,
                              message=f"测试消息发送成功，共 {ok_count} 个目标",
                              targets=ok_count)
        errors = "; ".join(f"{r.action}: {r.error}" for r in results if not r.ok)
        return PushResult(
            success=False,
            message=f"测试消息发送失败（成功 {ok_count}/{len(results)}）：{errors[:300]}",
            targets=ok_count,
        )

    def push_custom_message(self, payload: Optional[dict] = None) -> PushResult:
        """推送自定义通知，供工作流或外部系统调用。"""
        if not self.get_state():
            return PushResult(success=False, message="插件未启用或配置不完整")
        payload = payload or {}
        title = str(payload.get("title") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not title and not text:
            return PushResult(success=False, message="title 与 text 不能同时为空")

        image = str(payload.get("image") or "").strip() or None
        link = str(payload.get("link") or "").strip() or None
        emoji = str(payload.get("emoji") or "📢").strip()

        # 允许调用方临时覆盖目标，未提供时使用插件配置。
        override = payload.get("target")
        if override:
            users, groups = parse_qq_targets(override)
        else:
            users, groups, _ = self._resolve_targets()

        if not users and not groups:
            return PushResult(success=False, message="没有可用的 QQ 目标")

        segments = self._build_segments(
            emoji=emoji,
            title=title or "MoviePilot 通知",
            text=text,
            image=image if self._send_image else None,
            link=link if self._send_link else None,
        )
        task = {
            "title": title or "自定义通知",
            "segments": segments,
            "user_ids": users,
            "group_ids": groups,
            "at_user": None,
            "enqueued_at": time.time(),
        }
        if self._accepting:
            try:
                self._queue.put_nowait(task)
                return PushResult(success=True,
                                  message="通知已加入发送队列",
                                  targets=len(users) + len(groups))
            except queue.Full:
                return PushResult(success=False, message="发送队列已满，请稍后重试")

        # 停用态下仍允许手动推送：直接同步发送一次。
        client = OneBotClient(
            base_url=self._api_url,
            access_token=self._access_token,
            retries=self._retries,
            rate_limit=self._rate_limit,
        )
        results = client.send_notification(segments, user_ids=users, group_ids=groups)
        ok_count = sum(1 for result in results if result.ok)
        return PushResult(success=ok_count > 0,
                          message=f"直接发送完成，成功 {ok_count}/{len(results)}",
                          targets=ok_count)

    def get_runtime_status(self, payload: Optional[dict] = None) -> PushResult:
        """返回插件运行状态摘要。"""
        return PushResult(
            success=True,
            message=self._status_text(),
            targets=len(self._private_users) + len(self._groups),
        )

    # ----------------------------------------------------------- 表单与页面

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面与默认配置。"""
        type_options = [
            {
                "title": f"{_MESSAGE_TYPE_STYLE.get(name, (name, ''))[1]} "
                         f"{_MESSAGE_TYPE_STYLE.get(name, (name, ''))[0]}",
                "value": name,
            }
            for name in _MESSAGE_TYPE_STYLE
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_image",
                                            "label": "附带海报图片",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_url",
                                            "label": "OneBot 接口地址",
                                            "placeholder": "http://127.0.0.1:3000",
                                            "hint": "NapCat WebUI 中「网络配置 - HTTP 服务器」的地址与端口",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "access_token",
                                            "label": "Access Token",
                                            "placeholder": "与 NapCat 配置保持一致，未设置则留空",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "targets",
                                            "label": "默认推送目标",
                                            "placeholder": "123456789\n群:987654321",
                                            "hint": "每行一个；裸 QQ 号按私聊发送，群号必须加前缀，例如 group:987654321 或 群:987654321",
                                            "persistent-hint": True,
                                            "rows": 3,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "user_targets",
                                            "label": "用户定向推送（可选）",
                                            "placeholder": "admin=123456789\nalice=111111,群:222222",
                                            "hint": "按 MoviePilot 用户名定向推送；未命中的用户回落到默认目标",
                                            "persistent-hint": True,
                                            "rows": 3,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "msgtypes",
                                            "label": "推送的消息类型",
                                            "items": type_options,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_link",
                                            "label": "附带链接",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "at_in_group",
                                            "label": "群消息 @ 用户",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "title_prefix",
                                            "label": "标题前缀",
                                            "placeholder": "例如 [MoviePilot]",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_length",
                                            "label": "单条消息最大长度",
                                            "placeholder": "1200",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "dedupe_seconds",
                                            "label": "去重时间窗口（秒）",
                                            "placeholder": "20",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "rate_limit",
                                            "label": "每秒最大发送条数",
                                            "placeholder": "3",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retries",
                                            "label": "失败重试次数",
                                            "placeholder": "2",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "使用前请在 NapCat 的「网络配置」中启用 HTTP 服务器"
                                                "（默认 127.0.0.1:3000）并配置 Access Token，"
                                                "然后点击下方按钮验证连通性。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "color": "primary",
                                            "variant": "tonal",
                                            "prepend-icon": "mdi-send-check",
                                            "onclick": (
                                                "function(e) { window.MoviePilotAPI.post("
                                                "'plugin/OneBotMsg/test', {}).then(function(r) {"
                                                " alert((r && r.message) || '已发送') })"
                                                ".catch(function(err) { console.error(err);"
                                                " alert('发送失败，请查看日志') }) }"
                                            ),
                                        },
                                        "text": "发送测试通知",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "api_url": "http://127.0.0.1:3000",
            "access_token": "",
            "targets": "",
            "user_targets": "",
            "msgtypes": list(_DEFAULT_TYPES),
            "send_image": True,
            "send_link": True,
            "at_in_group": False,
            "title_prefix": "",
            "max_length": _DEFAULT_MAX_LENGTH,
            "dedupe_seconds": 20,
            "rate_limit": 3,
            "retries": 2,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面，展示运行状态与使用说明。"""
        stats = self._stats or {}
        return [
            {
                "component": "VCard",
                "props": {"class": "mb-4", "variant": "tonal"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {"component": "VIcon",
                             "props": {"icon": "mdi-qqchat", "class": "mr-2"}},
                            "运行状态",
                        ],
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {"component": "VAlert",
                             "props": {"type": "info" if self.get_state() else "warning",
                                       "variant": "tonal",
                                       "text": self._status_text()}},
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"class": "mb-4", "variant": "tonal"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {"component": "VIcon",
                             "props": {"icon": "mdi-help-circle-outline", "class": "mr-2"}},
                            "使用说明",
                        ],
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {"component": "VAlert",
                             "props": {
                                 "type": "info",
                                 "variant": "tonal",
                                 "text": (
                                     "1. NapCat 中启用 HTTP 服务器并记录地址与 Token；"
                                     "2. 在本插件填入接口地址、Token 与推送目标；"
                                     "3. 勾选需要推送的消息类型后保存配置；"
                                     "4. 点击「发送测试通知」确认 QQ 能收到消息。"
                                     "远程命令：/onebot_test、/onebot_status。"
                                 ),
                             }},
                        ],
                    },
                ],
            },
        ]

    # ------------------------------------------------------------- 内部工具

    def _type_enabled(self, type_name: Optional[str]) -> bool:
        """判断某消息类型是否允许推送。

        只推送配置中勾选的场景；未勾选任何场景时表示不推送通知，避免用户在
        页面上清空选择后仍被消息打扰。同时尊重宿主的全局通知场景开关。
        """
        if not type_name:
            # 未标注类型的通知按「其它」处理，需要显式勾选。
            type_name = "Other"
        if type_name not in self._msgtypes:
            return False
        if self._qq_channel_disabled():
            logger.debug("宿主 QQ 通知渠道处于禁用状态，OneBot 通知跳过转发")
            return False
        return self._host_scene_allows(type_name)

    @staticmethod
    def _qq_channel_disabled() -> bool:
        """检测宿主是否配置了处于禁用状态的 QQ 通知渠道。

        宿主的通知派发不按渠道过滤，禁用状态由各通知模块自行判断。若用户在
        MoviePilot 中显式关闭了 QQ 渠道，通常意味着不希望消息发到 QQ，此时
        插件不再转发；完全没有配置 QQ 渠道时不影响本插件。
        """
        try:
            configs = ServiceConfigHelper.get_notification_configs()
        except Exception as err:  # noqa: BLE001
            logger.debug(f"读取宿主通知渠道配置失败：{err}")
            return False
        for config in configs or []:
            channel_type = str(getattr(config, "type", "") or "").strip().lower()
            if channel_type not in ("qq", "qqbot", "onebot"):
                continue
            # 只要存在启用的 QQ 渠道就继续转发，全部禁用时才跳过。
            if bool(getattr(config, "enabled", False)):
                return False
        # 存在 QQ 渠道但没有任何一个启用。
        return any(
            str(getattr(config, "type", "") or "").strip().lower()
            in ("qq", "qqbot", "onebot")
            for config in (configs or [])
        )

    @staticmethod
    def _host_scene_allows(type_name: str) -> bool:
        """查询宿主通知场景开关，读取失败时按允许处理。

        宿主通过 ``NotificationSwitchs`` 保存每个场景的目标范围（all/user/admin），
        场景不存在时 ``get_notification_switch`` 默认返回 ``admin`` 而不是关闭，
        因此这里只在明确读到空值时拦截。
        """
        try:
            member = MessageType.__members__.get(type_name)
            if member is None:
                return True
            scope = ServiceConfigHelper.get_notification_switch(member)
            if scope is None:
                return True
            return bool(str(scope).strip())
        except Exception as err:  # noqa: BLE001
            logger.debug(f"读取宿主通知场景开关失败，按允许处理：{err}")
            return True

    def get_runtime_stats(self) -> Dict[str, int]:
        """返回运行统计快照，供测试与详情页使用。"""
        return dict(self._stats or {})


def _as_list(value: Any) -> List[str]:
    """把配置值规范化为去重的字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        items = [str(value).strip()]
    result: List[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _as_int(value: Any, default: int) -> int:
    """安全地把配置值转为整数。"""
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """安全地把配置值转为浮点数。"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _type_name(mtype: Any) -> Optional[str]:
    """从通知类型字段解析出稳定的枚举成员名。"""
    if mtype is None:
        return None
    if isinstance(mtype, MessageType):
        return mtype.name
    if isinstance(mtype, str):
        # 可能是成员名（Download）或展示值（资源下载）。
        value = mtype.strip()
        if not value:
            return None
        if value in MessageType.__members__:
            return value
        for member in MessageType:
            if member.value == value:
                return member.name
        return value
    name = getattr(mtype, "name", None)
    return str(name) if name else None


def _type_emoji(type_name: Optional[str]) -> str:
    """返回消息类型对应的 emoji。"""
    if not type_name:
        return "📢"
    return _MESSAGE_TYPE_STYLE.get(type_name, ("", "📢"))[1]


def _dedupe_key(type_name: Optional[str], title: str, text: str,
                image: Any) -> str:
    """构造通知去重键。

    宿主会按 admin/user/all 路由把同一条通知派发多次，键包含类型、标题、
    正文与图片即可稳定识别重复项。
    """
    return "|".join([
        str(type_name or ""),
        (title or "").strip(),
        (text or "").strip()[:200],
        str(image or ""),
    ])


def _pick_target(targets: Any) -> Optional[str]:
    """从宿主路由的 targets 中取出本渠道的目标。

    ``targets`` 形如 ``{"telegram": "-100123", "qq": "123456"}``，
    这里依次匹配常见键名，兼容大小写与渠道枚举值。
    """
    if not targets:
        return None
    if isinstance(targets, str):
        return targets
    if not isinstance(targets, dict):
        return None
    for key in ("qq", "QQ", "QQBot", "qqbot", "onebot", "OneBot"):
        value = targets.get(key)
        if value:
            return str(value)
    return None


def _season_episode(season: Any, episode: Any) -> str:
    """格式化季集信息，缺失的部分自动省略。"""
    parts: List[str] = []
    if season not in (None, ""):
        try:
            parts.append(f"S{int(season):02d}")
        except (TypeError, ValueError):
            parts.append(f"S{season}")
    if episode not in (None, ""):
        try:
            parts.append(f"E{int(episode):02d}")
        except (TypeError, ValueError):
            parts.append(f"E{episode}")
    return "".join(parts) or "-"


def _media_identity(media: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """按 V3 合同解析媒体身份（media_source + media_id）。"""
    source = media.get("media_source") or meta.get("media_source")
    media_id = media.get("media_id") or meta.get("media_id")
    if source and media_id and resolve_media_identity is not None:
        try:
            resolved_source, resolved_id = resolve_media_identity(
                media_source=source, media_id=media_id
            )
            if resolved_source and resolved_id:
                return str(getattr(resolved_source, "value", resolved_source)), str(resolved_id)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"解析媒体身份失败：{err}")
    if source and media_id:
        return str(getattr(source, "value", source)), str(media_id)
    return None, None


def _format_size(size: Any) -> str:
    """把字节数格式化为便于阅读的体积文本。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size or "")
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.2f} {units[index]}"


def _first_value(data: Dict[str, Any], *keys: str) -> Optional[str]:
    """按顺序取第一个可读值。

    ``TransferInfo`` 的目标路径藏在 ``target_item`` / ``target_diritem`` 这类
    FileItem 子对象里，需要逐层取 ``path`` 字段。
    """
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, dict):
            nested = value.get("path") or value.get("name")
            if nested:
                return str(nested)
            continue
        return str(value)
    return None


def _object_to_dict(value: Any) -> Dict[str, Any]:
    """把 pydantic 模型或普通对象转换为字典，失败时返回空字典。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for method in ("model_dump", "dict", "to_dict"):
        func = getattr(value, method, None)
        if callable(func):
            try:
                result = func()
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                continue
    # 退化为按公开属性读取，避免领域对象导致通知构造失败。
    result: Dict[str, Any] = {}
    for attr in ("title", "year", "type", "category", "media_source", "media_id",
                 "tmdb_id", "tmdbid", "douban_id", "name", "begin_season",
                 "begin_episode", "season", "episode", "target_path", "file_count"):
        attr_value = getattr(value, attr, None)
        if attr_value is not None:
            result[attr] = attr_value
    return result


def _segment_text(segment: Dict[str, Any]) -> str:
    """读取文本消息段的文本内容。"""
    if isinstance(segment, dict) and segment.get("type") == "text":
        return str((segment.get("data") or {}).get("text") or "")
    return ""


def _unique(items: List[str]) -> List[str]:
    """保持顺序去重。"""
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result
