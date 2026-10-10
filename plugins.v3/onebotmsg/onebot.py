"""OneBot v11 协议客户端。

只依赖标准库实现，避免给 MoviePilot 宿主引入额外 Python 依赖。

协议依据 NapCat 文档（https://doc.napneko.icu/onebot/network 与 /onebot/segment）：

* 正向 HTTP：``POST {base_url}/{action}``，请求体为动作参数，响应为
  ``{"status": "ok"|"failed", "retcode": 0, "data": {...}, "message": "..."}``。
* 鉴权：``Authorization: Bearer <access_token>``。
* 消息段：``{"type": "text", "data": {"text": "..."}}``、
  ``{"type": "image", "data": {"file": "<url>"}}``、
  ``{"type": "at", "data": {"qq": "<qq号>"}}``。

客户端自身不启动线程，线程与生命周期由插件主类负责，方便插件停用时收口。
"""

import json
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# 单个动作的最长等待时间（秒）。NapCat 本地部署时响应通常远快于该值，
# 设置上限是为了避免事件处理器被网络故障长期占用。
DEFAULT_TIMEOUT = 12.0

# 单次请求的重试次数（不含首次尝试）。
DEFAULT_RETRIES = 2

# 重试基础等待时间（秒），实际等待为 base * 2**n 并叠加随机抖动。
RETRY_BASE_DELAY = 0.6


class OneBotError(RuntimeError):
    """OneBot 调用失败，携带动作名与失败原因。"""

    def __init__(self, message: str, action: Optional[str] = None,
                 retcode: Optional[int] = None):
        """记录动作名与协议返回码，便于日志定位。"""
        super().__init__(message)
        self.action = action
        self.retcode = retcode


class OneBotResponse:
    """一次动作调用的结构化结果。"""

    __slots__ = ("action", "ok", "retcode", "status", "message", "data", "error")

    def __init__(self, action: str, ok: bool, retcode: Optional[int] = None,
                 status: Optional[str] = None, message: Optional[str] = None,
                 data: Optional[Any] = None, error: Optional[str] = None):
        """保存协议字段，``ok`` 表示业务是否成功。"""
        self.action = action
        self.ok = ok
        self.retcode = retcode
        self.status = status
        self.message = message
        self.data = data
        self.error = error

    @property
    def message_id(self) -> Optional[Union[str, int]]:
        """返回协议 data 中的消息 ID，缺失时返回 None。"""
        if isinstance(self.data, dict):
            return self.data.get("message_id")
        return None

    def __repr__(self) -> str:
        """返回便于日志阅读的简要描述。"""
        return (
            f"<OneBotResponse action={self.action} ok={self.ok} "
            f"retcode={self.retcode} error={self.error}>"
        )


class TokenBucket:
    """线程安全的令牌桶限流器。

    NapCat 对 API 调用有频率限制，短时间内批量推送通知容易被风控或丢消息，
    因此在客户端侧做一次主动限流。
    """

    def __init__(self, rate: float, capacity: float):
        """rate 为每秒补充的令牌数，capacity 为桶容量。"""
        self._rate = max(float(rate), 0.0)
        self._capacity = max(float(capacity), 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, rate: float, capacity: Optional[float] = None) -> None:
        """运行期调整限流参数，用于配置热更新。"""
        with self._lock:
            self._rate = max(float(rate), 0.0)
            if capacity is not None:
                self._capacity = max(float(capacity), 1.0)
            self._tokens = min(self._tokens, self._capacity)

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取一个令牌，超时未获取到返回 False。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                if self._rate <= 0:
                    # 限流被关闭（rate<=0）时直接放行，避免把通知永久阻塞。
                    return True
                wait = (1.0 - self._tokens) / self._rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(max(min(wait, 1.0), 0.01))

    def _refill_locked(self) -> None:
        """按经过的时间补充令牌；调用方必须持有锁。"""
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)


class DedupeCache:
    """短期去重缓存，用于压掉宿主重复派发的同一条通知。"""

    def __init__(self, ttl: float = 30.0, maxsize: int = 512):
        """ttl 为条目存活秒数，maxsize 为条目上限（下限 1）。"""
        self._ttl = max(float(ttl), 0.0)
        self._maxsize = max(int(maxsize), 1)
        self._entries: Dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        """首次出现返回 False 并登记；TTL 内重复出现返回 True。"""
        if not key or self._ttl <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            if key in self._entries:
                self._entries[key] = now
                return True
            self._entries[key] = now
            if len(self._entries) > self._maxsize:
                # 超限时按登记时间淘汰最旧的一批，避免缓存无限增长。
                overflow = len(self._entries) - self._maxsize
                for stale_key, _ in sorted(self._entries.items(), key=lambda kv: kv[1])[:overflow]:
                    self._entries.pop(stale_key, None)
            return False

    def __len__(self) -> int:
        """返回当前条目数，便于测试与诊断。"""
        with self._lock:
            self._purge_locked(time.monotonic())
            return len(self._entries)

    def clear(self) -> None:
        """清空去重缓存。"""
        with self._lock:
            self._entries.clear()

    def _purge_locked(self, now: float) -> None:
        """移除过期条目；调用方必须持有锁。"""
        expired = [k for k, ts in self._entries.items() if now - ts > self._ttl]
        for key in expired:
            self._entries.pop(key, None)


def normalize_base_url(url: Optional[str]) -> str:
    """规范化 OneBot HTTP 接口地址。

    支持用户直接粘贴 NapCat WebUI 中的地址，允许缺少协议头、结尾多写斜杠。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = f"http://{text}"
    return text.rstrip("/")


def build_text_segment(text: str) -> Dict[str, Any]:
    """构造 OneBot v11 文本消息段。"""
    return {"type": "text", "data": {"text": str(text)}}


def build_image_segment(file: str) -> Dict[str, Any]:
    """构造 OneBot v11 图片消息段。

    NapCat 的 ``image.file`` 支持 URL、本地路径和 Base64，这里使用 URL 或本地路径。
    """
    return {"type": "image", "data": {"file": str(file)}}


def build_at_segment(qq: Union[str, int]) -> Dict[str, Any]:
    """构造 OneBot v11 @消息段。"""
    return {"type": "at", "data": {"qq": str(qq)}}


class OneBotClient:
    """OneBot v11 正向 HTTP 客户端。"""

    def __init__(self, base_url: str, access_token: str = "",
                 timeout: float = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                 rate_limit: float = 3.0, rate_burst: float = 6.0,
                 dedupe_ttl: float = 30.0):
        """建立客户端；本对象可在线程间共享，配置变更由 set_config 生效。"""
        self._lock = threading.Lock()
        self._base_url = normalize_base_url(base_url)
        self._access_token = str(access_token or "").strip()
        self._timeout = max(float(timeout), 1.0)
        self._retries = max(int(retries), 0)
        self._bucket = TokenBucket(rate_limit, rate_burst)
        self._dedupe = DedupeCache(ttl=dedupe_ttl)

    # ------------------------------------------------------------------ 配置

    @property
    def base_url(self) -> str:
        """返回当前规范化后的接口地址。"""
        return self._base_url

    @property
    def dedupe(self) -> DedupeCache:
        """返回去重缓存，供插件对同一事件做幂等判断。"""
        return self._dedupe

    def set_config(self, base_url: Optional[str] = None,
                   access_token: Optional[str] = None,
                   timeout: Optional[float] = None,
                   retries: Optional[int] = None,
                   rate_limit: Optional[float] = None,
                   rate_burst: Optional[float] = None) -> None:
        """热更新客户端配置，未传入的项保持不变。"""
        with self._lock:
            if base_url is not None:
                self._base_url = normalize_base_url(base_url)
            if access_token is not None:
                self._access_token = str(access_token or "").strip()
            if timeout is not None:
                self._timeout = max(float(timeout), 1.0)
            if retries is not None:
                self._retries = max(int(retries), 0)
            if rate_limit is not None:
                self._bucket.set_rate(rate_limit, rate_burst)

    def clear_dedupe(self) -> None:
        """清空去重缓存。"""
        self._dedupe.clear()

    # ------------------------------------------------------------------ 传输

    def call(self, action: str, params: Optional[Dict[str, Any]] = None,
             retries: Optional[int] = None) -> OneBotResponse:
        """调用一个 OneBot 动作并返回结构化结果。

        网络错误会按指数退避重试；协议层失败（retcode != 0）不重试，
        因为这类失败通常由参数或权限问题导致，重试没有意义。
        """
        if not self._base_url:
            return OneBotResponse(action, False, error="未配置 OneBot 接口地址")

        payload = dict(params or {})
        remaining = self._retries if retries is None else max(int(retries), 0)
        last_error: Optional[str] = None

        for attempt in range(remaining + 1):
            if not self._bucket.acquire(timeout=self._timeout):
                last_error = "本地限流等待超时"
                break
            try:
                return self._call_once(action, payload)
            except OneBotError as err:
                # 协议层明确失败，直接返回结果，不做无意义重试。
                return OneBotResponse(action, False, retcode=err.retcode, error=str(err))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    ValueError, json.JSONDecodeError) as err:
                last_error = self._describe_error(err)
                if attempt >= remaining:
                    break
                delay = RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.85, 1.15)
                time.sleep(delay)

        return OneBotResponse(action, False, error=last_error or "未知错误")

    def _call_once(self, action: str, payload: Dict[str, Any]) -> OneBotResponse:
        """执行一次 HTTP 调用；网络错误向上抛出以便重试。"""
        with self._lock:
            url = f"{self._base_url}/{action}"
            token = self._access_token
            timeout = self._timeout

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "MoviePilot-OneBotMsg/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()

        try:
            result = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError as err:
            raise OneBotError(f"响应不是合法 JSON：{raw[:200]!r}") from err

        if not isinstance(result, dict):
            raise OneBotError(f"响应结构非法：{result!r}")

        status = result.get("status")
        retcode = result.get("retcode")
        message = result.get("message") or result.get("msg") or result.get("wording")
        data = result.get("data")
        ok = status == "ok" and (retcode in (0, None))
        if not ok:
            raise OneBotError(
                f"retcode={retcode} status={status} message={message}",
                action=action,
                retcode=retcode if isinstance(retcode, int) else None,
            )
        return OneBotResponse(action, True, retcode=retcode, status=status,
                              message=message, data=data)

    @staticmethod
    def _describe_error(err: BaseException) -> str:
        """把底层网络异常转换为可读的短描述。"""
        if isinstance(err, urllib.error.HTTPError):
            # NapCat 未配置 Token 或 Token 错误时通常返回 401/403。
            hint = "（请检查 Access Token）" if err.code in (401, 403) else ""
            return f"HTTP {err.code} {err.reason}{hint}"
        if isinstance(err, urllib.error.URLError):
            return f"连接失败：{getattr(err, 'reason', err)}"
        return f"{type(err).__name__}: {err}"

    # ------------------------------------------------------------------ 动作

    def get_login_info(self) -> OneBotResponse:
        """获取登录账号信息，用于连通性测试。"""
        return self.call("get_login_info", retries=0)

    def get_status(self) -> OneBotResponse:
        """获取运行状态，用于连通性测试。"""
        return self.call("get_status", retries=0)

    def send_private_msg(self, user_id: Union[str, int],
                         message: Union[str, Sequence[Dict[str, Any]]],
                         auto_escape: bool = False) -> OneBotResponse:
        """发送私聊消息。"""
        return self.call("send_private_msg", {
            "user_id": str(user_id),
            "message": _normalize_message(message),
            "auto_escape": bool(auto_escape),
        })

    def send_group_msg(self, group_id: Union[str, int],
                       message: Union[str, Sequence[Dict[str, Any]]],
                       auto_escape: bool = False) -> OneBotResponse:
        """发送群消息。"""
        return self.call("send_group_msg", {
            "group_id": str(group_id),
            "message": _normalize_message(message),
            "auto_escape": bool(auto_escape),
        })

    def send_msg(self, message_type: str, message: Union[str, Sequence[Dict[str, Any]]],
                 user_id: Optional[Union[str, int]] = None,
                 group_id: Optional[Union[str, int]] = None) -> OneBotResponse:
        """发送消息，目标由 message_type 决定（private/group）。"""
        payload: Dict[str, Any] = {
            "message_type": message_type,
            "message": _normalize_message(message),
        }
        if user_id is not None:
            payload["user_id"] = str(user_id)
        if group_id is not None:
            payload["group_id"] = str(group_id)
        return self.call("send_msg", payload)

    def delete_msg(self, message_id: Union[str, int]) -> OneBotResponse:
        """撤回消息。"""
        return self.call("delete_msg", {"message_id": str(message_id)})

    def send_poke(self, user_id: Union[str, int],
                  group_id: Optional[Union[str, int]] = None) -> OneBotResponse:
        """发送戳一戳；传入 group_id 时为群内戳一戳。"""
        if group_id is not None:
            return self.call("group_poke", {
                "group_id": str(group_id),
                "user_id": str(user_id),
            })
        return self.call("friend_poke", {"user_id": str(user_id)})

    def get_group_list(self) -> OneBotResponse:
        """获取群列表，用于配置页校验群号。"""
        return self.call("get_group_list", retries=0)

    # ------------------------------------------------------- 组合发送能力

    def send_notification(self, message: Union[str, Sequence[Dict[str, Any]]],
                          user_ids: Sequence[Union[str, int]] = (),
                          group_ids: Sequence[Union[str, int]] = (),
                          at_user: Optional[Union[str, int]] = None) -> List[OneBotResponse]:
        """向多个私聊与群聊目标发送同一条消息，返回每次调用的结果。

        群消息可选在文本前插入 @ 段。单个目标失败不会中断其余目标，
        调用方可根据返回结果决定是否提示用户。
        """
        results: List[OneBotResponse] = []
        for user_id in user_ids:
            text = str(user_id)
            if not text:
                continue
            results.append(self.send_private_msg(text, message))
        for group_id in group_ids:
            text = str(group_id)
            if not text:
                continue
            if at_user:
                segments: List[Dict[str, Any]] = [build_at_segment(at_user)]
                if isinstance(message, str):
                    segments.append(build_text_segment(f" {message}"))
                else:
                    # 组合消息时把 @ 段插到最前面，正文原样保留。
                    segments.extend(list(message))
                results.append(self.send_group_msg(text, segments))
            else:
                results.append(self.send_group_msg(text, message))
        return results


def _normalize_message(message: Union[str, Sequence[Dict[str, Any]]]) -> Any:
    """把字符串或消息段列表统一为协议可发送的形式。

    OneBot 同时接受字符串与消息段数组；这里只做结构校验，
    不把字符串包装成 text 段，以保留 NapCat 对内联 CQ 码的处理行为。
    """
    if isinstance(message, str):
        return message
    if isinstance(message, (list, tuple)):
        segments = [seg for seg in message if isinstance(seg, dict)]
        return segments
    return str(message)


def split_segments(segments: Sequence[Dict[str, Any]],
                   limit: int) -> List[List[Dict[str, Any]]]:
    """按文本段累计长度切分消息段列表，避免超出 NapCat 的单条消息上限。

    图片段不参与长度累计，也不会被拆分。``limit`` 小于等于 0 时原样返回。
    """
    if limit <= 0:
        return [list(segments)]
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_len = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = ""
        if segment.get("type") == "text":
            data = segment.get("data") or {}
            text = str(data.get("text") or "")
        seg_len = len(text)
        if current and current_len + seg_len > limit:
            chunks.append(current)
            current = []
            current_len = 0
        if seg_len > limit:
            # 单个文本段超长时按字符硬切，保证不会一次性发出超大消息。
            for index in range(0, seg_len, limit):
                piece = text[index:index + limit]
                if current:
                    chunks.append(current)
                    current = []
                    current_len = 0
                chunks.append([build_text_segment(piece)])
            continue
        current.append(segment)
        current_len += seg_len
    if current:
        chunks.append(current)
    return chunks or [[]]


def join_segment_texts(segments: Sequence[Dict[str, Any]]) -> str:
    """拼接消息段中的所有文本，用于日志与测试断言。"""
    parts: List[str] = []
    for segment in segments:
        if isinstance(segment, dict) and segment.get("type") == "text":
            parts.append(str((segment.get("data") or {}).get("text") or ""))
    return "".join(parts)


def shorten(text: Optional[str], limit: int) -> str:
    """截断过长文本，``limit`` 小于等于 0 时不截断。"""
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + "..."


def format_duration(seconds: float) -> str:
    """把秒数格式化为便于阅读的时长文本。"""
    try:
        total = int(max(float(seconds), 0))
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total // 3600} 时 {(total % 3600) // 60} 分"


def parse_targets(value: Any) -> List[str]:
    """解析用户配置里的目标列表，支持列表、逗号/换行/分号分隔的字符串。"""
    if not value:
        return []
    items: List[str] = []
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value).replace("，", ",").replace(";", ",").replace("；", ",")
        text = text.replace("\r", "\n")
        raw_items = []
        for line in text.split("\n"):
            raw_items.extend(line.split(","))
    for item in raw_items:
        text = str(item).strip()
        if text and text not in items:
            items.append(text)
    return items


def parse_user_target_map(value: Any) -> Dict[str, List[str]]:
    """解析 MoviePilot 用户名到 QQ 目标的映射。

    配置格式为 JSON 对象，值可以是单个 QQ 号、逗号分隔字符串或数组：
    ``{"admin": "123456", "alice": ["123456", "10001"]}``。
    """
    if not value:
        return {}
    data = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 兼容 ``admin=123456`` 这类行式配置，降低手写 JSON 的门槛。
            mapping: Dict[str, List[str]] = {}
            for line in text.replace("\r", "\n").split("\n"):
                if not line.strip():
                    continue
                separator = "=" if "=" in line else (":" if ":" in line else None)
                if not separator:
                    continue
                name, _, target = line.partition(separator)
                name = name.strip().strip('"').strip("'")
                targets = parse_targets(target)
                if name and targets:
                    mapping[name] = targets
            return mapping
    if not isinstance(data, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for key, target in data.items():
        name = str(key).strip()
        targets = parse_targets(target)
        if name and targets:
            result[name] = targets
    return result


# 群目标的显式前缀，其余条目一律按私聊 QQ 号处理。
_GROUP_PREFIXES = ("group:", "g:", "群:", "群聊:", "群组:")
_PRIVATE_PREFIXES = ("private:", "user:", "p:", "私聊:", "好友:", "个人:")


def _fold_colon(text: str) -> str:
    """把全角冒号折成半角，让 ``群：`` 与 ``群:`` 两种写法都能识别。

    用户在不同输入法下写出的冒号并不一致，前缀识别必须两者都收，
    否则 ``群：123456`` 会被当成一个私聊 QQ 号，消息发不到群里。
    """
    return text.replace("：", ":").replace("，", ",")


def parse_qq_targets(value: Any) -> Tuple[List[str], List[str]]:
    """把目标列表拆分为私聊 QQ 号与群号。

    裸 QQ 号与群号在 OneBot 里都是纯数字，无法可靠自动区分，因此群目标必须
    显式加前缀，例如 ``group:123456``、``群:123456`` 或 ``群：123456``；
    没有前缀的条目按私聊 QQ 号处理，避免把群号误当好友号导致发送失败。
    """
    users: List[str] = []
    groups: List[str] = []
    for raw_target in parse_targets(value):
        # 前缀用折叠后的文本判断，取值仍从原文按等长切，避免全角字符错位。
        folded = _fold_colon(raw_target).lower()
        matched = False
        for prefix in _GROUP_PREFIXES:
            if folded.startswith(prefix):
                group_id = raw_target[len(prefix):].strip()
                if group_id:
                    groups.append(group_id)
                matched = True
                break
        if matched:
            continue
        for prefix in _PRIVATE_PREFIXES:
            if folded.startswith(prefix):
                user_id = raw_target[len(prefix):].strip()
                if user_id:
                    users.append(user_id)
                matched = True
                break
        if matched:
            continue
        users.append(raw_target)
    return users, groups
