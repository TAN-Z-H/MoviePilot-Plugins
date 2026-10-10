"""MoviePilot V3 插件：OneBot v11（NapCat）消息通知。

插件职责：

1. 订阅宿主 ``EventType.NoticeMessage``，把 MoviePilot 全部通知（入库/整理、
   下载、订阅、站点、媒体服务器、插件、系统错误等）转发到 QQ；
2. 订阅 ``TransferComplete`` / ``TransferFailed`` 等事件，在通知之外补充结构化
   上下文（分类、季集、文件数、体积、目标路径、失败原因等）；
3. 支持两种 OneBot v11 传输：正向 HTTP 接口与正向 WebSocket 长连接
   （见 :mod:`.transport`），并可通过 NapCat 扩展 API 发送合并转发、贴表情回复、
   上传群文件（见 :mod:`.napcat`）；
4. 通知文本按卡片样式排版，并按显示宽度对齐（见 :mod:`.render`）；
5. 图片支持 URL / base64 / 本地路径多种落地方式，并带自动降级；
6. 内置发送队列、限流、去重与重试，避免消息轰炸与 NapCat 风控；
7. 提供远程命令与插件 API，便于在 QQ 里查询状态、在外部系统主动推送。

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

from .napcat import (
    IMAGE_LAYOUT_ATTACH,
    IMAGE_LAYOUT_LABELS,
    IMAGE_LAYOUT_SEPARATE,
    IMAGE_MODE_AUTO,
    IMAGE_MODE_LABELS,
    IMAGE_MODE_TEXT,
    IMAGE_POSITION_BOTTOM,
    IMAGE_POSITION_LABELS,
    IMAGE_POSITION_TOP,
    can_send_image,
    forward_node,
    image_segment,
    normalize_image_candidates,
    probe_extensions,
    segments_text,
    send_forward,
    send_group_notice,
    send_image,
    set_reaction,
    text_segment,
)
from .onebot import (
    OneBotResponse,
    parse_qq_targets,
    parse_user_target_map,
    shorten,
)
from .render import (
    DEFAULT_MAX_WIDTH,
    STYLE_CARD,
    STYLE_COMPACT,
    STYLE_LABELS,
    STYLE_MINIMAL,
    STYLE_PLAIN,
    build_card,
    build_forward_nodes,
    build_status_card,
    build_test_card,
    normalize_style,
)
from .transport import (
    TRANSPORT_HTTP,
    TRANSPORT_LABELS,
    TRANSPORT_WS,
    Transport,
    build_transport,
    describe_transport,
    normalize_transport,
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

# 长通知超过该行数时，合并转发模式更合适（QQ 折叠展示，不刷屏）。
_FORWARD_HINT_ROWS = 5

# 发送方式
SEND_MODE_TEXT = "text"
SEND_MODE_FORWARD = "forward"
SEND_MODE_LABELS = {
    SEND_MODE_TEXT: "单条消息（卡片文本）",
    SEND_MODE_FORWARD: "合并转发（NapCat 扩展，长通知更整洁）",
}


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
    plugin_desc = "通过 OneBot v11（NapCat）向 QQ 私聊/群聊推送 MoviePilot 通知，支持 HTTP / WebSocket 两种传输与 NapCat 扩展 API。"
    # 插件图标：使用 NapCat 官方 Logo 的完整 HTTPS 地址。
    # 插件市场索引与插件类声明同一个字段，两者必须保持同一形式。
    plugin_icon = "https://raw.githubusercontent.com/NapNeko/NapCatQQ/main/logo.png"
    # 插件版本
    plugin_version = "1.2.0"
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
    # 连接方式与地址、鉴权
    _transport_kind: str = TRANSPORT_HTTP
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
    _image_mode: str = IMAGE_MODE_AUTO
    _image_layout: str = IMAGE_LAYOUT_ATTACH
    _image_position: str = IMAGE_POSITION_TOP
    _max_image_mb: float = 3.0
    _send_link: bool = True
    _at_in_group: bool = False
    _title_prefix: str = ""
    _style: str = STYLE_CARD
    _send_mode: str = SEND_MODE_TEXT
    _forward_threshold: int = _FORWARD_HINT_ROWS
    _react_on_success: bool = False
    _max_length: int = _DEFAULT_MAX_LENGTH
    _card_width: int = DEFAULT_MAX_WIDTH
    _dedupe_seconds: int = 20
    _rate_limit: float = 3.0
    # 失败重试次数
    _retries: int = 2

    # 运行期资源
    _transport: Optional[Transport] = None
    _queue: "queue.Queue[Dict[str, Any]]" = None  # type: ignore[assignment]
    _worker: Optional[threading.Thread] = None
    _stop_event: threading.Event = None  # type: ignore[assignment]
    _lifecycle_lock: threading.Lock = None  # type: ignore[assignment]
    _accepting: bool = False
    # 运行统计
    _stats: Dict[str, int] = None  # type: ignore[assignment]
    # 最近一次成功投递的消息 ID（用于贴表情回复等扩展动作）
    _last_message_id: Optional[Union[str, int]] = None
    # NapCat 能力探测结果（延迟查询，避免每次发送都请求）
    _capabilities: Dict[str, Any] = None  # type: ignore[assignment]

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
        self._last_message_id = None
        self._capabilities = {}
        self._stats = {
            "received": 0,
            "sent": 0,
            "failed": 0,
            "dropped": 0,
            "filtered": 0,
            "deduped": 0,
            # WS 通道收到 message_sent 的次数，代表"确实发出去了"的消息数
            "confirmed": 0,
            "images": 0,
            "images_fallback": 0,
            "reacted": 0,
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
        self._transport_kind = normalize_transport(config.get("transport"))
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
        self._image_mode = str(config.get("image_mode") or IMAGE_MODE_AUTO).strip() \
            or IMAGE_MODE_AUTO
        self._image_layout = _as_choice(
            config.get("image_layout"), IMAGE_LAYOUT_LABELS, IMAGE_LAYOUT_ATTACH,
        )
        self._image_position = _as_choice(
            config.get("image_position"), IMAGE_POSITION_LABELS, IMAGE_POSITION_TOP,
        )
        self._max_image_mb = _as_float(config.get("max_image_mb"), 3.0)
        self._send_link = bool(config.get("send_link", True))
        self._at_in_group = bool(config.get("at_in_group"))
        self._title_prefix = str(config.get("title_prefix") or "")
        self._style = normalize_style(config.get("style"))
        self._send_mode = str(config.get("send_mode") or SEND_MODE_TEXT).strip() \
            or SEND_MODE_TEXT
        self._forward_threshold = _as_int(config.get("forward_threshold"),
                                          _FORWARD_HINT_ROWS)
        self._react_on_success = bool(config.get("react_on_success"))
        self._max_length = _as_int(config.get("max_length"), _DEFAULT_MAX_LENGTH)
        self._card_width = _as_int(config.get("card_width"), DEFAULT_MAX_WIDTH)
        self._dedupe_seconds = _as_int(config.get("dedupe_seconds"), 20)
        self._rate_limit = _as_float(config.get("rate_limit"), 3.0)
        self._retries = _as_int(config.get("retries"), 2)

        # WS 通道在收到 message_sent 时记录回执，用于确认「消息真的发出去了」。
        self._transport = build_transport(
            kind=self._transport_kind,
            url=self._api_url,
            access_token=self._access_token,
            timeout=12.0,
            retries=self._retries,
            rate_limit=self._rate_limit,
            rate_burst=max(self._rate_limit * 2, 2.0),
            dedupe_ttl=max(self._dedupe_seconds, 0),
            on_event=self._on_transport_event,
        )

        if not self._enabled:
            logger.info("OneBot v11 通知插件未启用")
            return

        self._start_worker()
        logger.info(
            f"OneBot v11 通知插件已启用：通道={TRANSPORT_LABELS.get(self._transport_kind)}，"
            f"地址={self._api_url or '未配置'}，"
            f"私聊目标={len(self._private_users)}，群目标={len(self._groups)}，"
            f"用户映射={len(self._user_targets)}，排版={self._style}"
        )

    def _on_transport_event(self, event: Dict[str, Any]) -> None:
        """处理 WS 通道推送的事件。

        目前只关心 ``message_sent``（自己发出的消息）：把它记为一次真实投递，
        并缓存消息 ID 供后续贴表情回复使用。
        """
        if not isinstance(event, dict):
            return
        if event.get("post_type") != "message_sent":
            return
        message_id = event.get("message_id")
        if message_id is not None:
            self._last_message_id = message_id
        self._bump("confirmed")

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
        """停止插件并释放队列、线程与连接，可安全重复调用。"""
        self._stop_worker()
        self._accepting = False
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception as err:  # noqa: BLE001
                logger.debug(f"关闭传输失败：{err}")
            dedupe = getattr(transport, "dedupe", None)
            if dedupe is not None:
                try:
                    dedupe.clear()
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"清理去重缓存失败：{err}")
        logger.info("OneBot v11 通知插件已停止")

    def close(self) -> None:
        """兼容新版宿主的第一阶段关闭钩子。"""
        self._stop_worker()
        self._accepting = False
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass

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
        """执行一条发送任务并记录统计。

        一条任务可能包含文本、图片与链接：文本按配置的排版样式渲染，图片按
        「URL → base64」候选顺序尝试，链接按开关附带。
        """
        transport = self._transport
        if transport is None:
            return
        user_ids = task.get("user_ids") or []
        group_ids = task.get("group_ids") or []
        at_user = task.get("at_user") if self._at_in_group else None
        title = task.get("title") or "MoviePilot 通知"
        text = task.get("text")
        link = task.get("link")
        emoji = task.get("emoji") or ""
        image = task.get("image") if self._send_image else None
        mode = task.get("send_mode") or self._send_mode

        if not user_ids and not group_ids:
            self._bump("dropped")
            logger.warning(f"OneBot 通知没有可用目标，已丢弃：{title}")
            return

        # 明细行数够多时，合并转发比一坨长文本更整洁；未显式配置时按行数自动选择。
        if mode == "auto":
            rows = len([line for line in str(text or "").split("\n") if line.strip()])
            mode = SEND_MODE_FORWARD if rows >= max(self._forward_threshold, 1) \
                else SEND_MODE_TEXT
        if mode not in (SEND_MODE_TEXT, SEND_MODE_FORWARD):
            mode = SEND_MODE_TEXT

        if task.get("raw"):
            # 成品文本（命令回执/测试卡片）：按原文发送，不再排版。
            sent, detail = self._send_text_to_targets(
                transport, str(text or ""), user_ids, group_ids, at_user,
            )
        elif mode == SEND_MODE_FORWARD:
            sent, detail = self._dispatch_forward(
                transport, emoji=emoji, title=title, text=text, link=link,
                user_ids=user_ids, group_ids=group_ids, at_user=at_user,
            )
        else:
            sent, detail = self._dispatch_card(
                transport, emoji=emoji, title=title, text=text, link=link,
                image=image, user_ids=user_ids, group_ids=group_ids, at_user=at_user,
            )

        if sent:
            self._bump("sent")
            logger.info(f"OneBot 通知已发送：{title}（{detail}）")
            self._maybe_react(transport)
        else:
            self._bump("failed")
            logger.warn(f"OneBot 通知发送失败：{title}；{detail}")

    def _maybe_react(self, transport: Transport) -> None:
        """按配置给刚发出的消息贴一个表情回复。

        这是 NapCat 扩展动作，老版本可能不支持；失败只记 debug，绝不影响通知本身。
        需要消息 ID，因此只有能拿到 message_id 的通道（HTTP 响应或 WS 回执）才有效。
        """
        if not self._react_on_success:
            return
        message_id = self._last_message_id
        if message_id is None:
            return
        ok, detail = set_reaction(transport, message_id)
        if ok:
            self._bump("reacted")
        else:
            logger.debug(f"表情回复未生效：{detail}")

    def _dispatch_card(self, transport: Transport, *, emoji: str, title: str,
                       text: Optional[str], link: Optional[str],
                       image: Optional[str], user_ids: List[str],
                       group_ids: List[str], at_user: Optional[str],
                       allow_image_fallback: bool = True) -> Tuple[bool, str]:
        """按卡片样式发送：文本 + 可选图片 + 可选链接。

        图片有两种呈现方式（配置 ``image_layout``）：

        * ``attach``（默认）：图片段与文本段**放进同一条消息**，QQ 会把它们显示在
          同一个气泡里，海报就成了通知的一部分，比「先一条文字、再一条图片」紧凑得多；
        * ``separate``：先发文本、再单独发图片（1.1.0 的行为）。

        inline 失败时自动退回 separate，保证图片和文字都不会丢。
        """
        card = build_card(
            emoji=emoji,
            title=title,
            text=text,
            link=link if self._send_link else None,
            style=self._style,
            prefix=self._title_prefix,
            max_width=self._card_width,
        )
        chunks = _split_text(card, self._max_length)

        image_files: List[str] = []
        image_note = ""
        if image:
            image_files, image_note = self._image_candidates(image)

        detail_parts: List[str] = []
        inline_ok = False
        if image_files and self._image_layout == IMAGE_LAYOUT_ATTACH:
            inline_ok, inline_detail = self._send_inline(
                transport, title=title, chunks=chunks, image_file=image_files[0],
                user_ids=user_ids, group_ids=group_ids, at_user=at_user,
            )
            if inline_ok:
                self._bump("images")
                detail_parts.append(f"图文同条（{inline_detail}）")
            else:
                logger.debug(f"图文同条失败，改为分开发送：{inline_detail}")

        if not inline_ok:
            results: List[OneBotResponse] = []
            for index, chunk in enumerate(chunks):
                body = chunk if len(chunks) == 1 else f"({index + 1}/{len(chunks)})\n{chunk}"
                results.extend(self._send_text_once(transport, body, user_ids, group_ids,
                                                    at_user))
            failed = [result for result in results if not result.ok]
            if not results:
                return False, "没有产生任何调用"
            if failed:
                errors = [f"{r.action}: {r.error}" for r in failed[:3]]
                return False, (f"成功 {len(results) - len(failed)}/{len(results)}；"
                               f"{'; '.join(errors)}")
            detail_parts.append(f"{len(results)} 个目标")

            if image_files:
                image_ok, image_detail = self._send_image_to_targets(
                    transport, image=image, candidates=image_files,
                    user_ids=user_ids, group_ids=group_ids,
                )
                if image_ok:
                    self._bump("images")
                    detail_parts.append(image_detail)
                else:
                    detail_parts.append(f"图片未发送（{image_detail}）")
                    if allow_image_fallback and self._style != STYLE_PLAIN:
                        logger.debug(f"图片发送失败：{image_detail}")
        elif image_note:
            detail_parts.append(image_note)
        return True, "；".join(detail_parts)

    def _image_candidates(self, image: str) -> Tuple[List[str], str]:
        """把图片来源解析为按优先级排序的候选发送值。"""
        target_dir = None
        try:
            target_dir = self.get_data_path() / "images"
        except Exception as err:  # noqa: BLE001 - 数据目录不可用时仍可走 URL/base64
            logger.debug(f"获取插件数据目录失败，图片仅支持 URL/base64：{err}")
        try:
            max_bytes = int(max(self._max_image_mb, 0.1) * 1024 * 1024)
        except (TypeError, ValueError):
            max_bytes = 3 * 1024 * 1024
        candidates, notes = normalize_image_candidates(
            image, target_dir=target_dir, mode=self._image_mode, max_bytes=max_bytes,
        )
        return candidates, "；".join(notes)

    def _send_text_once(self, transport: Transport, body: str,
                        user_ids: List[str], group_ids: List[str],
                        at_user: Optional[str]) -> List[OneBotResponse]:
        """把一段成品文本发给所有目标，返回每次调用的结果。"""
        results: List[OneBotResponse] = []
        for user_id in user_ids:
            result = transport.call("send_private_msg", {
                "user_id": str(user_id),
                "message": [text_segment(body)],
            })
            if result is not None:
                results.append(result)
                if result.ok and result.message_id is not None:
                    self._last_message_id = result.message_id
        for group_id in group_ids:
            segments: List[Dict[str, Any]] = []
            if at_user:
                segments.append({"type": "at", "data": {"qq": str(at_user)}})
            segments.append(text_segment(body))
            result = transport.call("send_group_msg", {
                "group_id": str(group_id),
                "message": segments,
            })
            if result is not None:
                results.append(result)
                if result.ok and result.message_id is not None:
                    self._last_message_id = result.message_id
        return results

    def _send_inline(self, transport: Transport, *, title: str, chunks: List[str],
                     image_file: str, user_ids: List[str], group_ids: List[str],
                     at_user: Optional[str]) -> Tuple[bool, str]:
        """把图片与文本放进同一条消息发送。

        只在**单段文本**时拼接：多段说明内容超长，图片挂在第一段反而割裂；
        此时交给调用方回退为分开发送。
        """
        if not image_file:
            return False, "没有可用的图片形式"
        if len(chunks) != 1:
            return False, f"文本被分成 {len(chunks)} 段，不适合图文同条"
        body = chunks[0]
        image_seg = image_segment(image_file)
        text_seg = text_segment(body)

        def _segments(at: bool) -> List[Dict[str, Any]]:
            """按配置顺序组装消息段；``at`` 为真时把 @ 段放在最前。"""
            head: List[Dict[str, Any]] = []
            if at and at_user:
                head.append({"type": "at", "data": {"qq": str(at_user)}})
            pair = ([image_seg, text_seg] if self._image_position == IMAGE_POSITION_TOP
                    else [text_seg, image_seg])
            return head + pair

        results: List[OneBotResponse] = []
        for user_id in user_ids:
            result = transport.call("send_private_msg", {
                "user_id": str(user_id),
                "message": _segments(False),
            })
            if result is not None:
                results.append(result)
                if result.ok and result.message_id is not None:
                    self._last_message_id = result.message_id
        for group_id in group_ids:
            # 这里必须传 @ 的 QQ 号本身，不能传布尔值——否则段里会变成 {"qq": "True"}。
            result = transport.call("send_group_msg", {
                "group_id": str(group_id),
                "message": _segments(bool(at_user)),
            })
            if result is not None:
                results.append(result)
                if result.ok and result.message_id is not None:
                    self._last_message_id = result.message_id

        if not results:
            return False, "没有产生任何调用"
        failed = [result for result in results if not result.ok]
        if failed:
            errors = [f"{r.action}: {r.error}" for r in failed[:2]]
            return False, f"成功 {len(results) - len(failed)}/{len(results)}；{'; '.join(errors)}"
        form = "base64" if image_file.lower().startswith(("base64://", "data:")) else (
            "URL" if image_file.lower().startswith(("http://", "https://")) else "本地路径"
        )
        return True, f"{len(results)} 个目标 · 图片按 {form}"

    def _send_text_to_targets(self, transport: Transport, text: str,
                              user_ids: List[str], group_ids: List[str],
                              at_user: Optional[str] = None) -> Tuple[bool, str]:
        """把一段**已渲染好**的文本原样发给所有目标。

        命令回执、测试消息这类内容本身就是成品卡片，不能再套一层排版
        （``build_card`` 需要标题或正文，直接传成品会被判空丢弃）。
        """
        body = str(text or "")
        if not body:
            return False, "内容为空"
        chunks = _split_text(body, self._max_length)
        results: List[OneBotResponse] = []
        for index, chunk in enumerate(chunks):
            payload = chunk if len(chunks) == 1 else f"({index + 1}/{len(chunks)})\n{chunk}"
            for user_id in user_ids:
                result = transport.call("send_private_msg", {
                    "user_id": str(user_id),
                    "message": [text_segment(payload)],
                })
                if result is not None:
                    results.append(result)
                    if result.ok and result.message_id is not None:
                        self._last_message_id = result.message_id
            for group_id in group_ids:
                segments: List[Dict[str, Any]] = []
                if at_user:
                    segments.append({"type": "at", "data": {"qq": str(at_user)}})
                segments.append(text_segment(payload))
                result = transport.call("send_group_msg", {
                    "group_id": str(group_id),
                    "message": segments,
                })
                if result is not None:
                    results.append(result)
                    if result.ok and result.message_id is not None:
                        self._last_message_id = result.message_id
        if not results:
            return False, "没有产生任何调用"
        failed = [result for result in results if not result.ok]
        if failed:
            errors = [f"{r.action}: {r.error}" for r in failed[:3]]
            return False, f"成功 {len(results) - len(failed)}/{len(results)}；{'; '.join(errors)}"
        return True, f"{len(results)} 个目标"

    def _send_image_to_targets(self, transport: Transport, *, image: str,
                              user_ids: List[str], group_ids: List[str],
                              candidates: Optional[List[str]] = None,
                              ) -> Tuple[bool, str]:
        """把图片按候选形式发给所有目标，任一形式成功即算成功。

        ``candidates`` 已由调用方解析时直接复用，避免重复下载同一张图片。
        """
        notes: List[str] = []
        if candidates is None:
            candidates, note = self._image_candidates(image)
            notes.append(note) if note else None
        if not candidates:
            return False, "；".join(filter(None, notes)) or "没有可用的图片形式"

        outcomes: List[str] = list(filter(None, notes))
        for index, file_value in enumerate(candidates):
            ok_all = True
            for user_id in user_ids:
                ok, detail = send_image(
                    transport, message_type="private", user_id=user_id,
                    candidates=[file_value],
                )
                outcomes.append(detail)
                ok_all = ok_all and ok
            for group_id in group_ids:
                ok, detail = send_image(
                    transport, message_type="group", group_id=group_id,
                    candidates=[file_value],
                )
                outcomes.append(detail)
                ok_all = ok_all and ok
            if ok_all:
                if index > 0:
                    self._bump("images_fallback")
                return True, "；".join(outcomes[-max(len(user_ids) + len(group_ids), 1):])
        return False, "；".join(outcomes[-3:])

    def _dispatch_forward(self, transport: Transport, *, emoji: str, title: str,
                          text: Optional[str], link: Optional[str],
                          user_ids: List[str], group_ids: List[str],
                          at_user: Optional[str]) -> Tuple[bool, str]:
        """以合并转发形式发送（NapCat 扩展动作）。

        该动作需要 NapCat 支持；若返回失败，自动降级为卡片文本，保证通知不断。
        """
        nodes = build_forward_nodes(
            emoji=emoji, title=title, text=text,
            link=link if self._send_link else None,
            bot_name=self.plugin_name, bot_uin="10000",
        )
        if not nodes:
            return False, "没有可发送的内容"
        results: List[str] = []
        ok_count = 0
        total = 0
        for user_id in user_ids:
            total += 1
            ok, detail = send_forward(
                transport, message_type="private", nodes=nodes, user_id=user_id,
            )
            ok_count += 1 if ok else 0
            results.append(detail)
        for group_id in group_ids:
            total += 1
            # 合并转发节点里已含标题，群里再 @ 一次会多一条消息，因此这里不重复 @。
            ok, detail = send_forward(
                transport, message_type="group", nodes=nodes, group_id=group_id,
            )
            ok_count += 1 if ok else 0
            results.append(detail)
        if total and ok_count == total:
            return True, f"合并转发 {ok_count} 个目标"
        logger.debug(f"合并转发失败，降级为卡片文本：{results[:2]}")
        sent, detail = self._dispatch_card(
            transport, emoji=emoji, title=title, text=text, link=link,
            image=None, user_ids=user_ids, group_ids=group_ids, at_user=at_user,
        )
        return sent, f"合并转发不可用，已降级：{detail}"

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
        dedupe = getattr(self._transport, "dedupe", None)
        if dedupe is not None and dedupe.seen(dedupe_key):
            self._bump("deduped")
            logger.debug(f"检测到重复通知，已跳过：{title}")
            return

        self._enqueue_task(self._task(
            title=title or "MoviePilot 通知",
            text=text,
            emoji=_type_emoji(type_name),
            image=str(data.get("image")) if data.get("image") else None,
            link=str(data.get("link")) if data.get("link") else None,
            userid=data.get("userid"),
            username=data.get("username"),
            targets=data.get("targets"),
        ))

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
        title_text = f"{'整理入库' if success else '整理失败'} {heading}"
        if not lines:
            lines.append("宿主持久化整理任务已完成")

        # 标题单独带 emoji，正文只放字段行，卡片渲染时会自动对齐。
        self._enqueue_task(self._task(
            title=title_text,
            text="\n".join(lines),
            emoji=emoji,
            link=str(info.get("link") or "") or None,
            image=_first_value(info, "image", "poster", "backdrop")
            or str(media.get("poster_path") or media.get("backdrop_path") or "") or None,
        ))

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
            # 回落到默认目标时，若配了默认私聊对象，就 @ 他——否则群里看不出这条是给谁的。
            at_user = users[0] if users else None

        return _unique(users), _unique(groups), at_user

    # ------------------------------------------------------------ 消息构造

    def _task(self, *, title: str, text: Optional[str] = None, emoji: str = "",
              link: Optional[str] = None, image: Optional[str] = None,
              send_mode: Optional[str] = None,
              userid: Optional[Union[str, int]] = None,
              username: Optional[Union[str, int]] = None,
              targets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造一条待发送任务。

        任务只保存原始素材（标题/正文/图片/链接），真正的排版在发送时按当前配置
        完成：这样改排版样式或图片方式不需要重新入队，也让两种传输共用同一份渲染。
        """
        return {
            "title": str(title or "MoviePilot 通知"),
            "text": str(text or ""),
            "emoji": str(emoji or ""),
            "link": str(link) if link else None,
            "image": str(image) if image else None,
            "send_mode": send_mode,
            "userid": userid,
            "username": username,
            "targets": targets,
        }

    def _enqueue_task(self, task: Dict[str, Any]) -> None:
        """解析目标后把任务放入队列。"""
        title = task.get("title") or "MoviePilot 通知"
        if not self._accepting:
            self._bump("dropped")
            logger.debug(f"插件未处于接收状态，丢弃通知：{title}")
            return
        users, groups, at_user = self._resolve_targets(
            task.get("userid"), task.get("username"), task.get("targets"),
        )
        if not users and not groups:
            self._bump("dropped")
            logger.warning(f"没有可用的 QQ 目标，通知未发送：{title}")
            return
        task.update({
            "user_ids": users,
            "group_ids": groups,
            "at_user": at_user,
            "enqueued_at": time.time(),
        })
        # 兼容旧字段名：部分调用方仍读取 segments。
        task.setdefault("segments", [])
        self._queue_put(task, title)

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
            card = self._status_card()
            title = "OneBot 通知运行状态"
            text = None
        else:
            result = self.send_test_message()
            card = result.message
            title = "OneBot 通知插件"
            text = None

        # 回执优先发回下命令的会话，无法识别时使用插件配置的目标。
        users, groups, _ = self._resolve_targets()
        sender_user = data.get("user") or data.get("userid")
        sender_group = data.get("group_id")
        if sender_user or sender_group:
            users = [str(sender_user)] if sender_user else []
            groups = [str(sender_group)] if sender_group else []

        self._enqueue_to(
            title=title,
            text=card,
            emoji="🧩",
            user_ids=users,
            group_ids=groups,
            raw=True,
        )

    def _enqueue_to(self, title: str, user_ids: List[str], group_ids: List[str],
                    text: Optional[str] = None,
                    emoji: str = "", raw: bool = False) -> None:
        """把一条已经确定目标的消息放入发送队列。

        ``raw=True`` 表示 ``text`` 已是渲染好的成品（如状态卡片），发送时按原始
        样式直出，不再套一层卡片边框。
        """
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
            "text": str(text or ""),
            "emoji": emoji,
            "link": None,
            "image": None,
            "send_mode": SEND_MODE_TEXT,
            # 已是成品卡片：发送时按原始文本直出，不再套排版。
            "raw": bool(raw),
            "user_ids": user_ids,
            "group_ids": group_ids,
            "at_user": None,
            "segments": [],
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
        """生成纯文本运行状态摘要（供日志与 API 使用）。"""
        stats = self._stats or {}
        return (
            "OneBot 通知插件运行中\n"
            f"通道：{describe_transport(self._transport)}\n"
            f"私聊目标：{len(self._private_users)} 个；群目标：{len(self._groups)} 个；"
            f"用户映射：{len(self._user_targets)} 条\n"
            f"队列：{self._queue.qsize()} 待发送\n"
            f"统计：接收 {stats.get('received', 0)}，"
            f"发送成功 {stats.get('sent', 0)}，失败 {stats.get('failed', 0)}，"
            f"过滤 {stats.get('filtered', 0)}，去重 {stats.get('deduped', 0)}，"
            f"丢弃 {stats.get('dropped', 0)}，"
            f"图片 {stats.get('images', 0)}（降级 {stats.get('images_fallback', 0)}），"
            f"投递回执 {stats.get('confirmed', 0)}"
        )

    def _status_card(self) -> str:
        """生成一条可直接发送的状态卡片。"""
        stats = self._stats or {}
        targets = f"私聊 {len(self._private_users)} / 群 {len(self._groups)} / 映射 {len(self._user_targets)}"
        return build_status_card(
            plugin_name=self.plugin_name,
            version=self.plugin_version,
            transport_label=TRANSPORT_LABELS.get(self._transport_kind, self._transport_kind),
            endpoint=self._api_url,
            stats=stats,
            queue_size=self._queue.qsize(),
            targets=targets,
            style=self._style,
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
            {
                "path": "/preview",
                "endpoint": self.preview_layout,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "预览三种排版样式的渲染结果（不发送）",
                "response_model": PushResult,
            },
            {
                "path": "/probe",
                "endpoint": self.probe_capabilities,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "探测 NapCat 版本与图片发送能力",
                "response_model": PushResult,
            },
        ]

    def send_test_message(self, payload: Optional[dict] = None) -> PushResult:
        """发送一条测试消息，验证连接方式、目标与排版是否正确。

        测试同步发送（不复用队列），这样页面上点一下就能立刻看到真实结果；
        返回的 ``message`` 里带上渲染后的卡片，便于直接在界面上核对排版。
        """
        if not self._enabled:
            return PushResult(success=False, message="插件未启用")
        users, groups, _at_user = self._resolve_targets()
        if not self._api_url:
            return PushResult(success=False, message="未配置 OneBot 地址")
        if not users and not groups:
            return PushResult(success=False, message="未配置任何 QQ 目标")

        # 用当前配置临时构造传输，避免与工作线程争用同一条长连接。
        probe = build_transport(
            kind=self._transport_kind, url=self._api_url,
            access_token=self._access_token, timeout=12.0, retries=0,
            rate_limit=self._rate_limit,
        )
        transport_label = TRANSPORT_LABELS.get(self._transport_kind, self._transport_kind)
        target_summary = f"私聊 {len(users)} / 群 {len(groups)}"
        card = build_test_card(
            plugin_name=self.plugin_name,
            version=self.plugin_version,
            transport_label=transport_label,
            target_summary=target_summary,
            now_text=time.strftime("%Y-%m-%d %H:%M:%S"),
            style=self._style,
        )
        try:
            sent, detail = self._send_text_to_targets(probe, card, users, groups)
            if not sent:
                # 单条模式失败时，尝试用合并转发兜底，顺便验证扩展动作是否可用。
                nodes = build_forward_nodes(
                    "✅", "OneBot 通知测试",
                    f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    None, bot_name=self.plugin_name,
                )
                forward_ok = False
                for user_id in users:
                    ok, _ = send_forward(probe, message_type="private",
                                         nodes=nodes, user_id=user_id)
                    forward_ok = forward_ok or ok
                for group_id in groups:
                    ok, _ = send_forward(probe, message_type="group",
                                         nodes=nodes, group_id=group_id)
                    forward_ok = forward_ok or ok
                if forward_ok:
                    return PushResult(
                        success=True,
                        message=(f"单条消息发送失败（{shorten(detail, 200)}），"
                                 f"已改用合并转发成功"),
                        targets=len(users) + len(groups),
                    )
                return PushResult(
                    success=False,
                    message=f"测试消息发送失败：{detail}",
                    targets=0,
                )
            return PushResult(
                success=True,
                message=f"测试消息发送成功（{detail}）\n\n{card}",
                targets=len(users) + len(groups),
            )
        finally:
            probe.close()

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
        # 调用方可临时指定发送方式与排版样式，未提供时沿用插件配置。
        send_mode = payload.get("send_mode")
        style = payload.get("style")

        # 允许调用方临时覆盖目标，未提供时使用插件配置。
        override = payload.get("target")
        if override:
            users, groups = parse_qq_targets(override)
        else:
            users, groups, _ = self._resolve_targets()

        if not users and not groups:
            return PushResult(success=False, message="没有可用的 QQ 目标")

        task = self._task(
            title=title or "MoviePilot 通知",
            text=text,
            emoji=emoji,
            image=image if self._send_image else None,
            link=link if self._send_link else None,
            send_mode=str(send_mode) if send_mode else None,
        )
        task.update({
            "user_ids": users,
            "group_ids": groups,
            "at_user": None,
            "segments": [],
            "enqueued_at": time.time(),
        })
        if style:
            task["style"] = normalize_style(style)

        if self._accepting:
            try:
                self._queue.put_nowait(task)
                return PushResult(success=True,
                                  message="通知已加入发送队列",
                                  targets=len(users) + len(groups))
            except queue.Full:
                return PushResult(success=False, message="发送队列已满，请稍后重试")

        # 停用态下仍允许手动推送：直接同步发送一次。
        probe = build_transport(
            kind=self._transport_kind, url=self._api_url,
            access_token=self._access_token, retries=0,
            rate_limit=self._rate_limit,
        )
        try:
            sent, detail = self._dispatch_card(
                transport=probe, emoji=emoji, title=title or "MoviePilot 通知",
                text=text, link=task.get("link"), image=task.get("image"),
                user_ids=users, group_ids=groups, at_user=None,
            )
            return PushResult(success=sent,
                              message=f"直接发送完成：{detail}",
                              targets=len(users) + len(groups) if sent else 0)
        finally:
            probe.close()

    def get_runtime_status(self, payload: Optional[dict] = None) -> PushResult:
        """返回插件运行状态摘要。"""
        return PushResult(
            success=True,
            message=self._status_text(),
            targets=len(self._private_users) + len(self._groups),
        )

    # ----------------------------------------------------------- 表单与页面

    @staticmethod
    def _field(model: str, label: str, *, kind: str = "VTextField",
               placeholder: Optional[str] = None, hint: Optional[str] = None,
               items: Optional[List[dict]] = None, multiple: bool = False,
               rows: Optional[int] = None, field_type: Optional[str] = None,
               min_value: Optional[float] = None,
               md: int = 6) -> dict:
        """构造一个表单项（列 + 组件）。

        用工厂方法而不是逐项手写，是为了让新增配置项只需一行——这个插件的配置项
        已经接近 20 个，手写 JSON 很容易漏字段或写错层级。
        """
        props: Dict[str, Any] = {"model": model, "label": label}
        if placeholder is not None:
            props["placeholder"] = placeholder
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        if items is not None:
            props["items"] = items
        if multiple:
            props["multiple"] = True
            props["chips"] = True
            props["clearable"] = True
        if rows is not None:
            props["rows"] = rows
        if field_type:
            props["type"] = field_type
        if min_value is not None:
            props["min"] = min_value
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": kind, "props": props}],
        }

    @staticmethod
    def _section(title: str, hint: str, *fields: dict) -> dict:
        """把若干字段归到一个带说明的区块里，便于在一个长表单中定位。"""
        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {"component": "VAlert",
                         "props": {"type": "info", "variant": "tonal",
                                   "density": "compact",
                                   "text": f"{title}：{hint}"}},
                    ],
                },
                *fields,
            ],
        }

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
        transport_options = [
            {"title": TRANSPORT_LABELS[TRANSPORT_HTTP] + "（默认，无需额外配置）",
             "value": TRANSPORT_HTTP},
            {"title": TRANSPORT_LABELS[TRANSPORT_WS] + "（需在 NapCat 开启 WS 服务器）",
             "value": TRANSPORT_WS},
        ]
        style_options = [{"title": STYLE_LABELS[key], "value": key} for key in STYLE_LABELS]
        image_options = [{"title": IMAGE_MODE_LABELS[key], "value": key}
                         for key in IMAGE_MODE_LABELS]
        image_layout_options = [{"title": IMAGE_LAYOUT_LABELS[key], "value": key}
                                for key in IMAGE_LAYOUT_LABELS]
        image_position_options = [{"title": IMAGE_POSITION_LABELS[key], "value": key}
                                  for key in IMAGE_POSITION_LABELS]
        send_mode_options = [{"title": SEND_MODE_LABELS[SEND_MODE_TEXT],
                              "value": SEND_MODE_TEXT},
                             {"title": SEND_MODE_LABELS[SEND_MODE_FORWARD],
                              "value": SEND_MODE_FORWARD},
                             {"title": "自动（明细多时用合并转发）", "value": "auto"}]

        form: List[dict] = [{
            "component": "VForm",
            "content": [
                self._section(
                    "基础", "总开关与消息目标；目标里群号必须带前缀（群:/group:）",
                    self._field("enabled", "启用插件", kind="VSwitch", md=4),
                    self._field("api_url", "OneBot 地址",
                                placeholder="http://127.0.0.1:3000",
                                hint="NapCat 的 HTTP 或 WS 服务器地址", md=4),
                    self._field("access_token", "Access Token",
                                placeholder="与 NapCat 一致，未设置留空",
                                field_type="password", md=4),
                    self._field(
                        "transport", "连接方式", kind="VSelect", items=transport_options,
                        hint="WS 长连接可收到投递回执，且往返更快", md=4,
                    ),
                    self._field("targets", "默认推送目标", kind="VTextarea", rows=3, md=8,
                                placeholder="123456789\n群:987654321",
                                hint="裸数字按私聊；群号必须加 群: 或 group: 前缀"),
                    self._field("user_targets", "用户定向推送（可选）", kind="VTextarea",
                                rows=3, md=12,
                                placeholder="admin=123456789\nalice=111111,群:222222",
                                hint="按 MoviePilot 用户名定向，未命中回落默认目标"),
                ),
                self._section(
                    "排版", "决定通知在 QQ 里长什么样",
                    self._field("style", "排版样式", kind="VSelect", items=style_options,
                                md=4),
                    self._field("card_width", "卡片最大宽度（列）",
                                placeholder=str(DEFAULT_MAX_WIDTH), field_type="number",
                                min_value=20, md=4),
                    self._field("title_prefix", "标题前缀", placeholder="例如 [MoviePilot]",
                                md=4),
                    self._field("send_mode", "发送方式", kind="VSelect",
                                items=send_mode_options, md=4),
                    self._field("forward_threshold", "自动转合并转发的明细行数",
                                placeholder=str(_FORWARD_HINT_ROWS), field_type="number",
                                md=4),
                    self._field("max_length", "单条消息最大长度",
                                placeholder=str(_DEFAULT_MAX_LENGTH), field_type="number",
                                md=4),
                    self._field("send_link", "附带链接", kind="VSwitch", md=3),
                    self._field("at_in_group", "群消息 @ 用户", kind="VSwitch", md=3),
                    self._field("react_on_success", "发送成功后贴表情回复",
                                kind="VSwitch", md=3,
                                hint="使用 NapCat set_msg_emoji_like，失败自动忽略"),
                    self._field("send_image", "附带海报图片", kind="VSwitch", md=3),
                ),
                self._section(
                    "图片", "图片发不出来时优先改这里：base64 内嵌不依赖网络可达性",
                    self._field("image_mode", "图片发送方式", kind="VSelect",
                                items=image_options, md=4),
                    self._field("image_layout", "图文组合方式", kind="VSelect",
                                items=image_layout_options, md=4,
                                hint="「图文同条」把海报嵌进通知气泡里，最紧凑"),
                    self._field("image_position", "同条时图片位置", kind="VSelect",
                                items=image_position_options, md=4),
                    self._field("max_image_mb", "base64 体积上限（MB）",
                                placeholder="3", field_type="number", min_value=0.1, md=6,
                                hint="超过上限会退回 URL 方式"),
                ),
                self._section(
                    "推送范围与稳定性", "控制推送哪些场景、以及发送节奏",
                    self._field("msgtypes", "推送的消息类型", kind="VSelect",
                                items=type_options, multiple=True, md=12,
                                hint="未勾选任何场景时不推送通知"),
                    self._field("dedupe_seconds", "去重时间窗口（秒）", placeholder="20",
                                field_type="number", md=4),
                    self._field("rate_limit", "每秒最大发送条数", placeholder="3",
                                field_type="number", md=4),
                    self._field("retries", "失败重试次数", placeholder="2",
                                field_type="number", md=4),
                ),
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
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
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "secondary",
                                        "variant": "tonal",
                                        "prepend-icon": "mdi-eye-outline",
                                        "onclick": (
                                            "function(e) { window.MoviePilotAPI.get("
                                            "'plugin/OneBotMsg/preview').then(function(r) {"
                                            " alert((r && r.message) || '无预览') })"
                                            ".catch(function(err) { console.error(err);"
                                            " alert('预览失败') }) }"
                                        ),
                                    },
                                    "text": "预览排版效果",
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "info",
                                        "variant": "tonal",
                                        "prepend-icon": "mdi-access-point-check",
                                        "onclick": (
                                            "function(e) { window.MoviePilotAPI.get("
                                            "'plugin/OneBotMsg/probe').then(function(r) {"
                                            " alert((r && r.message) || '无结果') })"
                                            ".catch(function(err) { console.error(err);"
                                            " alert('探测失败') }) }"
                                        ),
                                    },
                                    "text": "探测 NapCat 能力",
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
                                            "连接方式：HTTP 正向接口最简单；正向 WebSocket "
                                            "需要在 NapCat「网络配置」里开启 WebSocket 服务器，"
                                            "地址可用同一端口的 ws:// 形式。图片发不出来时把"
                                            "「图片发送方式」改为 base64 内嵌最稳。"
                                        ),
                                    },
                                }
                            ],
                        }
                    ],
                },
            ],
        }]
        return form, {
            "enabled": False,
            "api_url": "http://127.0.0.1:3000",
            "access_token": "",
            "transport": TRANSPORT_HTTP,
            "targets": "",
            "user_targets": "",
            "msgtypes": list(_DEFAULT_TYPES),
            "style": STYLE_CARD,
            "card_width": DEFAULT_MAX_WIDTH,
            "send_mode": SEND_MODE_TEXT,
            "forward_threshold": _FORWARD_HINT_ROWS,
            "title_prefix": "",
            "send_image": True,
            "image_mode": IMAGE_MODE_AUTO,
            "image_layout": IMAGE_LAYOUT_ATTACH,
            "image_position": IMAGE_POSITION_TOP,
            "max_image_mb": 3,
            "send_link": True,
            "at_in_group": False,
            "react_on_success": False,
            "max_length": _DEFAULT_MAX_LENGTH,
            "dedupe_seconds": 20,
            "rate_limit": 3,
            "retries": 2,
        }

    def _preview_card(self) -> str:
        """用示例数据渲染一张卡片，供配置页预览排版效果（不发送）。"""
        sample = "\n".join([
            "季集：S01E03",
            "分类：电视剧/欧美剧",
            "来源：themoviedb:1399",
            "文件数：3",
            "体积：2.41 GB",
            "目标：/media/TV/Game of Thrones/Season 01",
            "方式：link",
        ])
        return build_card(
            emoji="✅",
            title="整理入库 权力的游戏 (2011)",
            text=sample,
            link="http://moviepilot.local:3000/#/media",
            style=self._style,
            prefix=self._title_prefix,
            max_width=self._card_width,
            footer=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def preview_layout(self, payload: Optional[dict] = None) -> PushResult:
        """返回当前排版配置的渲染结果（含样式对照），不产生任何发送。"""
        previews = []
        for style in (STYLE_CARD, STYLE_COMPACT, STYLE_PLAIN):
            card = build_card(
                emoji="✅",
                title="整理入库 权力的游戏 (2011)",
                text="季集：S01E03\n分类：电视剧/欧美剧\n来源：themoviedb:1399",
                link="http://moviepilot.local:3000/#/media",
                style=style,
                prefix=self._title_prefix,
                max_width=self._card_width,
            )
            marker = " ← 当前配置" if style == self._style else ""
            previews.append(f"【{STYLE_LABELS[style]}】{marker}\n{card}")
        return PushResult(success=True,
                          message="\n\n".join(previews),
                          targets=0)

    def probe_capabilities(self, payload: Optional[dict] = None) -> PushResult:
        """探测 NapCat 版本与图片发送能力，用于排查"发不出去"。"""
        if not self._api_url:
            return PushResult(success=False, message="未配置 OneBot 地址")
        probe = build_transport(
            kind=self._transport_kind, url=self._api_url,
            access_token=self._access_token, retries=0,
        )
        try:
            report = probe_extensions(probe)
        finally:
            probe.close()
        lines = [
            f"连接方式：{TRANSPORT_LABELS.get(self._transport_kind, self._transport_kind)}",
            f"地址：{self._api_url}",
            f"NapCat 版本：{report.get('version') or '未知'}",
            f"实现名称：{report.get('app_name') or '未知'}",
            f"允许发送图片：{report.get('can_send_image')}",
            f"图片发送方式：{IMAGE_MODE_LABELS.get(self._image_mode, self._image_mode)}",
        ]
        if report.get("version") is None:
            lines.append("提示：未能取到版本信息，通常是地址/Token 不正确，或服务未启动。")
        elif report.get("can_send_image") is False:
            lines.append("提示：NapCat 报告当前账号不能发送图片，图片会被跳过。")
        return PushResult(success=True, message="\n".join(lines), targets=0)

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


def _as_choice(value: Any, allowed: Dict[str, Any], default: str) -> str:
    """把配置值限制在给定取值集合内；非法值回落到默认，避免脏配置让发送失败。"""
    text = str(value or "").strip().lower()
    return text if text in allowed else default


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


def _split_text(text: str, limit: int) -> List[str]:
    """按长度上限切分长文本。

    卡片是按行拼装的，直接按字符切会把边框切断、内容错位，因此这里优先按行边界
    切分；单行本身超长时才硬切。
    """
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return [value] if value else []
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in value.split("\n"):
        if len(line) > limit:
            # 先结算已累积的行，再对超长行硬切。
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            for offset in range(0, len(line), limit):
                chunks.append(line[offset:offset + limit])
            continue
        if current and current_len + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _unique(items: List[str]) -> List[str]:
    """保持顺序去重。"""
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result
