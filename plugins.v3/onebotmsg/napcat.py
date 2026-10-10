"""NapCat 扩展 API 与消息段封装。

这里覆盖两类东西：

1. **消息段构造**：文本 / 图片 / @ / 合并转发节点。全部只依赖 OneBot v11 消息段
   结构，缩进与换行都在这里统一处理。
2. **动作调用**：除标准 OneBot 动作（``send_private_msg`` 等）之外的 NapCat 扩展，
   以及图片发送的多种落地方式。所有函数都接收 :class:`~.transport.Transport`，
   因此 HTTP 与 WebSocket 两种传输共用同一套实现。

扩展动作的字段名与语义取自 NapCat 官方接口文档（``doc.napneko.icu/develop/api/doc``）：

* ``set_msg_emoji_like``：``message_id`` + ``emoji_id``（表情回复）
* ``send_forward_msg``：``message_type``（private/group）+ 目标 id + ``messages``（node 数组）
* ``can_send_image``：无参数，返回是否允许发送图片
* ``_send_group_notice``：``group_id`` + ``content``
* ``upload_group_file``：``group_id`` + ``file`` + ``name``
"""

import base64
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .transport import Transport

# 图片数据的多种来源
IMAGE_MODE_AUTO = "auto"
IMAGE_MODE_URL = "url"
IMAGE_MODE_BASE64 = "base64"
IMAGE_MODE_PATH = "path"
IMAGE_MODE_TEXT = "text"

IMAGE_MODE_LABELS = {
    IMAGE_MODE_AUTO: "自动（先 URL，失败转 base64）",
    IMAGE_MODE_URL: "仅 URL 外链",
    IMAGE_MODE_BASE64: "仅 base64 内嵌（最可靠）",
    IMAGE_MODE_PATH: "仅本地路径（需共享目录）",
    IMAGE_MODE_TEXT: "不发送图片，只留链接",
}

# base64 内嵌的默认上限（字节）。QQ 单条消息对 base64 图片有长度限制，
# 超过后 NapCat/服务端会拒收，因此超限时自动退化为发链接。
DEFAULT_MAX_IMAGE_BYTES = 3 * 1024 * 1024

# 图片下载超时（秒）
IMAGE_DOWNLOAD_TIMEOUT = 15.0

# 表情回复常用 ID（NapCat 使用 QQ 表情 ID 字符串）
EMOJI_THUMBS_UP = "128077"
EMOJI_OK = "128076"
EMOJI_MUSIC = "127925"
EMOJI_ROCKET = "128640"

# 图片来源：http(s) / base64:// / data: / file:// / 本地路径
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_BASE64_RE = re.compile(r"^base64://", re.IGNORECASE)
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?;base64,", re.IGNORECASE)


# --------------------------------------------------------------------- 消息段


def text_segment(text: Any) -> Dict[str, Any]:
    """构造文本消息段。"""
    return {"type": "text", "data": {"text": str(text)}}


def image_segment(file: str) -> Dict[str, Any]:
    """构造图片消息段。

    ``file`` 支持 NapCat 的全部资源形式：http(s) URL、``base64://``、
    ``data:`` URL、``file://`` 标识与本地绝对路径。
    """
    return {"type": "image", "data": {"file": str(file)}}


def at_segment(qq: Union[str, int]) -> Dict[str, Any]:
    """构造 @ 消息段。"""
    return {"type": "at", "data": {"qq": str(qq)}}


def reply_segment(message_id: Union[str, int]) -> Dict[str, Any]:
    """构造回复消息段。"""
    return {"type": "reply", "data": {"id": str(message_id)}}


def forward_node(name: str, content: Sequence[Dict[str, Any]],
                 uin: Union[str, int] = "10000") -> Dict[str, Any]:
    """构造合并转发中的单个节点。

    NapCat 同时接受 ``user_id`` 与 ``uin`` 两种写法，这里都带上以兼容不同版本。
    """
    return {
        "type": "node",
        "data": {
            "name": str(name or "MoviePilot"),
            "uin": str(uin),
            "user_id": str(uin),
            "content": list(content),
        },
    }


def node_from_text(name: str, text: str, uin: Union[str, int] = "10000") -> Dict[str, Any]:
    """用一段纯文本构造合并转发节点。"""
    return forward_node(name, [text_segment(text)], uin=uin)


def segments_text(segments: Sequence[Dict[str, Any]]) -> str:
    """拼接消息段中的全部文本，供日志与测试断言使用。"""
    parts: List[str] = []
    for segment in segments:
        if isinstance(segment, dict) and segment.get("type") == "text":
            parts.append(str((segment.get("data") or {}).get("text") or ""))
    return "".join(parts)


# ------------------------------------------------------------- 图片预处理


def looks_like_url(value: str) -> bool:
    """判断是否是 http(s) 地址。"""
    return bool(_URL_RE.match(str(value or "").strip()))


def looks_like_remote(value: str) -> bool:
    """判断是否需要先下载才能得到本地文件（http/https/data URL）。"""
    text = str(value or "").strip()
    return bool(_URL_RE.match(text) or _DATA_URL_RE.match(text))


def guess_suffix(url: str, content_type: str = "") -> str:
    """按 Content-Type 或 URL 推断图片后缀。"""
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime.startswith("image/"):
        suffix = mimetypes.guess_extension(mime) or ".jpg"
        return ".jpg" if suffix == ".jpe" else suffix
    path = str(url or "").split("?", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return suffix
    return ".jpg"


def download_image(url: str, target_dir: Path, timeout: float = IMAGE_DOWNLOAD_TIMEOUT,
                   referer: Optional[str] = None) -> Optional[Path]:
    """把图片下载到插件数据目录，返回本地路径。

    豆瓣 / TMDB 等图源对 ``Referer`` 或 ``User-Agent`` 有要求，这里统一带上浏览器
    UA，并在需要时补 ``Referer``。下载失败返回 None，由调用方决定降级方式。
    """
    text = str(url or "").strip()
    if not text:
        return None
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    elif "doubanio.com" in text:
        headers["Referer"] = "https://movie.douban.com/"
    try:
        request = urllib.request.Request(text, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None
    if not content:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    # 用内容哈希命名，同一张海报重复下载时自然复用。
    import hashlib

    digest = hashlib.sha1(content).hexdigest()[:16]
    target = target_dir / f"{digest}{guess_suffix(text, content_type)}"
    try:
        if not target.exists():
            target.write_bytes(content)
    except OSError:
        return None
    return target


def read_image_bytes(source: str) -> Optional[bytes]:
    """读取图片字节，支持本地路径、``base64://``、``data:`` 与 http(s)。"""
    text = str(source or "").strip()
    if not text:
        return None
    if _BASE64_RE.match(text):
        try:
            return base64.b64decode(text[len("base64://"):], validate=False)
        except (ValueError, TypeError):
            return None
    data_match = _DATA_URL_RE.match(text)
    if data_match:
        try:
            return base64.b64decode(text[data_match.end():], validate=False)
        except (ValueError, TypeError):
            return None
    if _URL_RE.match(text):
        try:
            request = urllib.request.Request(text, headers={
                "User-Agent": "MoviePilot-OneBotMsg/1.0",
            })
            with urllib.request.urlopen(request, timeout=IMAGE_DOWNLOAD_TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            return None
    path = Path(text)
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        return None
    return None


def to_base64_uri(source: str,
                  max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> Optional[str]:
    """把图片转换为 ``base64://`` 形式。

    返回 None 表示无法转换（读不到内容或超过体积上限）——调用方据此降级为发链接，
    而不是让整条消息发送失败。
    """
    content = read_image_bytes(source)
    if not content:
        return None
    if max_bytes and len(content) > max_bytes:
        return None
    return "base64://" + base64.b64encode(content).decode("ascii")


def normalize_image_candidates(image: Optional[str],
                               target_dir: Optional[Path] = None,
                               mode: str = IMAGE_MODE_AUTO,
                               max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
                               ) -> Tuple[List[str], List[str]]:
    """按配置把图片源转换为候选发送值列表。

    :return: ``(候选值列表, 说明列表)``；候选值为 ``image`` 段的 ``file`` 取值或
        空列表（表示本次不发图片）
    """
    text = str(image or "").strip()
    if not text:
        return [], []
    mode = str(mode or IMAGE_MODE_AUTO).strip().lower()
    notes: List[str] = []

    if mode == IMAGE_MODE_TEXT:
        return [], ["图片按配置仅保留链接"]

    if mode == IMAGE_MODE_URL:
        if looks_like_url(text):
            return [text], []
        return [], ["仅 URL 模式但图片来源不是 http(s) 地址"]

    if mode == IMAGE_MODE_PATH:
        if target_dir is not None and looks_like_remote(text):
            local = download_image(text, target_dir)
            if local is None:
                return [], ["本地路径模式：图片下载失败"]
            notes.append(f"已下载到 {local}")
            return [str(local)], notes
        if looks_like_url(text):
            return [], ["本地路径模式但无法下载图片"]
        return [text], []

    if mode == IMAGE_MODE_BASE64:
        uri = to_base64_uri(text, max_bytes=max_bytes)
        if uri is None:
            return [], ["base64 模式：图片过大或读取失败，已降级为链接"]
        return [uri], ["以 base64 内嵌"]

    # auto：优先 URL（省流量、最快），失败时由调用方用 base64 重试。
    candidates: List[str] = []
    if looks_like_url(text):
        candidates.append(text)
    uri = to_base64_uri(text, max_bytes=max_bytes)
    if uri is not None:
        candidates.append(uri)
    elif looks_like_url(text):
        notes.append("图片超过 base64 体积上限，仅尝试 URL 方式")
    if not candidates:
        # 既不是 URL 也读不到内容：退化为把地址当文本展示。
        return [], ["图片无法读取，已降级为链接文本"]
    return candidates, notes


# ------------------------------------------------------------------ 动作调用


def send_image(transport: Optional[Transport], *, message_type: str,
               user_id: Optional[Union[str, int]] = None,
               group_id: Optional[Union[str, int]] = None,
               image: str = "",
               caption: str = "",
               at_user: Optional[Union[str, int]] = None,
               candidates: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
    """发送图片消息，按候选值逐个尝试。

    :param candidates: 已排序的 ``file`` 取值列表（URL / base64 / 路径）
    :return: ``(是否成功, 说明)``
    """
    if transport is None:
        return False, "未初始化传输"
    files = list(candidates or ([image] if image else []))
    if not files:
        return False, "没有可用的图片形式"
    errors: List[str] = []
    for index, file_value in enumerate(files):
        segments: List[Dict[str, Any]] = []
        if at_user and message_type == "group":
            segments.append(at_segment(at_user))
        if caption:
            head = f" {caption}" if at_user and message_type == "group" else caption
            segments.append(text_segment(head))
        segments.append(image_segment(file_value))
        action = "send_group_msg" if message_type == "group" else "send_private_msg"
        params: Dict[str, Any] = {"message": segments}
        if message_type == "group":
            params["group_id"] = str(group_id)
        else:
            params["user_id"] = str(user_id)
        result = transport.call(action, params)
        if result is not None and result.ok:
            form = "URL" if looks_like_url(file_value) else (
                "base64" if file_value.lower().startswith(("base64://", "data:")) else "本地路径"
            )
            return True, f"图片已按 {form} 形式发送"
        reason = result.error if result is not None else (transport.last_error or "无响应")
        errors.append(f"第 {index + 1} 种形式失败：{reason}")
    return False, "；".join(errors[:3])


def send_forward(transport: Optional[Transport], *, message_type: str,
                 nodes: Sequence[Dict[str, Any]],
                 user_id: Optional[Union[str, int]] = None,
                 group_id: Optional[Union[str, int]] = None) -> Tuple[bool, str]:
    """以「合并转发」形式发送多条内容（NapCat 扩展动作 ``send_forward_msg``）。

    相比一坨长文本，合并转发把每条内容独立成节点，在 QQ 里折叠成一张卡片，
    是长通知最整洁的呈现方式。
    """
    if transport is None:
        return False, "未初始化传输"
    if not nodes:
        return False, "没有可转发的节点"
    params: Dict[str, Any] = {
        "message_type": message_type,
        "messages": list(nodes),
    }
    if message_type == "group":
        params["group_id"] = str(group_id)
    else:
        params["user_id"] = str(user_id)
    result = transport.call("send_forward_msg", params)
    if result is not None and result.ok:
        return True, "已按合并转发发送"
    reason = result.error if result is not None else (transport.last_error or "无响应")
    return False, f"合并转发失败：{reason}"


def set_reaction(transport: Optional[Transport], message_id: Union[str, int],
                 emoji_id: str = EMOJI_THUMBS_UP) -> Tuple[bool, str]:
    """给指定消息贴一个表情回复（NapCat 扩展动作 ``set_msg_emoji_like``）。

    :return: ``(是否支持并成功, 说明)``；返回 False 通常意味着该版本 NapCat 不支持，
    调用方应静默降级。
    """
    if transport is None:
        return False, "未初始化传输"
    result = transport.call("set_msg_emoji_like", {
        "message_id": str(message_id),
        "emoji_id": str(emoji_id),
    })
    if result is not None and result.ok:
        return True, "已贴表情回复"
    reason = result.error if result is not None else (transport.last_error or "无响应")
    return False, f"表情回复不可用：{reason}"


def can_send_image(transport: Optional[Transport]) -> Optional[bool]:
    """查询当前账号是否允许发送图片（NapCat 标准动作 ``can_send_image``）。"""
    if transport is None:
        return None
    data = transport.call_data("can_send_image")
    if isinstance(data, dict):
        value = data.get("yes")
        if isinstance(value, bool):
            return value
    return None


def send_group_notice(transport: Optional[Transport], group_id: Union[str, int],
                      content: str) -> Tuple[bool, str]:
    """发送群公告（NapCat 扩展动作 ``_send_group_notice``）。"""
    if transport is None:
        return False, "未初始化传输"
    result = transport.call("_send_group_notice", {
        "group_id": str(group_id),
        "content": str(content),
    })
    if result is not None and result.ok:
        return True, "群公告已发送"
    reason = result.error if result is not None else (transport.last_error or "无响应")
    return False, f"群公告发送失败：{reason}"


def upload_group_file(transport: Optional[Transport], group_id: Union[str, int],
                      file_path: str, name: Optional[str] = None) -> Tuple[bool, str]:
    """上传群文件（NapCat 标准动作 ``upload_group_file``）。

    ``file`` 需要是 NapCat 能读到的本地路径，因此只在 MoviePilot 与 NapCat
    共享了同一目录时可用。
    """
    if transport is None:
        return False, "未初始化传输"
    path = Path(str(file_path))
    if not path.is_file():
        return False, f"文件不存在或不可读：{file_path}"
    result = transport.call("upload_group_file", {
        "group_id": str(group_id),
        "file": str(path),
        "name": name or path.name,
    })
    if result is not None and result.ok:
        return True, "群文件已上传"
    reason = result.error if result is not None else (transport.last_error or "无响应")
    return False, f"群文件上传失败：{reason}"


def poke(transport: Optional[Transport], user_id: Union[str, int],
         group_id: Optional[Union[str, int]] = None) -> Tuple[bool, str]:
    """戳一戳（``send_poke`` 会自动按是否带 ``group_id`` 走群聊/私聊）。"""
    if transport is None:
        return False, "未初始化传输"
    params: Dict[str, Any] = {"user_id": str(user_id)}
    if group_id is not None:
        params["group_id"] = str(group_id)
    result = transport.call("send_poke", params)
    if result is not None and result.ok:
        return True, "已发送戳一戳"
    reason = result.error if result is not None else (transport.last_error or "无响应")
    return False, f"戳一戳失败：{reason}"


def probe_extensions(transport: Optional[Transport]) -> Dict[str, Any]:
    """探测 NapCat 扩展能力，供详情页展示。

    只做只读动作（``get_version_info`` / ``can_send_image``），不产生任何副作用。
    """
    report: Dict[str, Any] = {
        "version": None,
        "app_name": None,
        "can_send_image": None,
    }
    if transport is None:
        return report
    info = transport.call_data("get_version_info")
    if isinstance(info, dict):
        report["version"] = info.get("version")
        report["app_name"] = info.get("app_name")
    report["can_send_image"] = can_send_image(transport)
    return report
