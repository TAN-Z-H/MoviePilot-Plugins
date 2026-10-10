"""通知排版：把 MoviePilot 的通知渲染成适合 QQ 聊天气泡的卡片文本。

两个关键设计取舍：

1. **不使用 Markdown**。OneBot v11 的文本段在 QQ 里不会渲染 ``**粗体**``、``> 引用``，
   只会原样显示，因此这里改用制表符 / 制表位字符与缩进来做视觉分区，任何客户端都
   能正确显示。
2. **按显示宽度对齐**，而不是按字符数。中日韩文字与 emoji 占两列，直接对中会让
   边框错位；:func:`display_width` 用 ``unicodedata.east_asian_width`` 计算真实列宽。

样式：

* ``card``：带边框的卡片 —— 标题 + 分隔线 + 字段区 + 底部链接区，长通知最好看；
* ``compact``：单行标题 + 缩进正文，气泡更矮，适合刷屏场景；
* ``plain``：完全不加装饰，保留 MoviePilot 原始文本。
"""

import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 排版样式标识
STYLE_CARD = "card"
STYLE_MINIMAL = "minimal"
STYLE_COMPACT = "compact"
STYLE_PLAIN = "plain"

STYLE_LABELS = {
    STYLE_CARD: "卡片（带边框，等宽字体最好看）",
    STYLE_MINIMAL: "极简（✅ 标题 + · 字段，配海报最协调）",
    STYLE_COMPACT: "紧凑（单行标题 + · 字段）",
    STYLE_PLAIN: "原始（不添加任何装饰）",
}

# 卡片默认最大显示宽度（列）。QQ 手机端气泡较窄，超过容易被折行破坏边框，
# 因此默认取一个保守值，并在构建时按内容自适应收窄。
DEFAULT_MAX_WIDTH = 34
# 边框最窄宽度
MIN_WIDTH = 20
# 正文里单个字段值的最大列数
DEFAULT_VALUE_WIDTH = 26
# 正文最多保留的行数（超出丢弃并提示）
DEFAULT_MAX_ROWS = 14


def normalize_style(value: Any) -> str:
    """规范化样式配置。"""
    text = str(value or "").strip().lower()
    if text in (STYLE_MINIMAL, "极简"):
        return STYLE_MINIMAL
    if text in (STYLE_COMPACT, "紧凑"):
        return STYLE_COMPACT
    if text in (STYLE_PLAIN, "原始", "plaintext"):
        return STYLE_PLAIN
    return STYLE_CARD


def display_width(text: str) -> int:
    """计算字符串在等宽字体下的显示列数（CJK 与全角符号按 2 列）。"""
    width = 0
    for char in str(text or ""):
        if char == "\n":
            continue
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate_width(text: str, limit: int, ellipsis: str = "…") -> str:
    """按显示列数截断，超出部分用省略号替代。"""
    if limit <= 0:
        return ""
    value = str(text or "")
    if display_width(value) <= limit:
        return value
    budget = max(limit - display_width(ellipsis), 1)
    out: List[str] = []
    used = 0
    for char in value:
        char_width = display_width(char)
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return "".join(out) + ellipsis


def pad_width(text: str, width: int) -> str:
    """按显示列数右侧补空格。"""
    return str(text or "") + " " * max(width - display_width(str(text or "")), 0)


def shorten_url(url: str, limit: int = 44) -> str:
    """把过长的链接缩短为「域名/…/尾段」，避免撑破卡片。"""
    text = str(url or "").strip()
    if len(text) <= limit:
        return text
    head, _, tail = text.partition("://")
    if not tail:
        return truncate_width(text, limit)
    host, _, rest = tail.partition("/")
    if not rest:
        return truncate_width(text, limit)
    keep = max(limit - len(host) - len(head) - 6, 8)
    return f"{head}://{host}/…{rest[-keep:]}"


def wrap_text(text: str, width: int) -> List[str]:
    """按显示列数软换行，保留原有换行。"""
    rows: List[str] = []
    for raw_line in str(text or "").split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            rows.append("")
            continue
        current: List[str] = []
        used = 0
        for char in line:
            char_width = display_width(char)
            if used + char_width > width and current:
                rows.append("".join(current))
                current = []
                used = 0
            current.append(char)
            used += char_width
        rows.append("".join(current))
    return rows


def split_fields(text: str) -> List[Tuple[str, str]]:
    """把 MoviePilot 的通知正文拆成 ``(字段名, 值)`` 列表。

    与宿主通知的正文约定一致：形如 ``季集：S01E01``。解析策略：

    * 逐行处理，行内既可能是一个字段，也可能是 ``a：1，b：2`` 这样的多字段；
    * 字段名不能包含分隔符，因此 ``时间：2026-10-10 02:34:17`` 的值里保留冒号；
    * 完全不含 ``标签：`` 的行按整行文本保留，不按逗号硬拆——否则
      ``一句话通知，没有字段结构`` 会被错误地切成两行。

    没有字段名结构的行按整体文本保留，避免正文被曲解。
    """
    import re

    value = str(text or "").strip()
    if not value:
        return []
    label_token = re.compile(r"(?:^|[，,;；])\s*([^：:，,;；]{1,16})[：:]")
    fields: List[Tuple[str, str]] = []
    for raw_line in value.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if not label_token.search(line):
            fields.append(("", line))
            continue
        pieces = [piece.strip()
                  for piece in re.split(r"[，,;；]+", line)
                  if piece.strip()]
        for piece in pieces:
            match = re.match(r"^([^：:]{1,16})[：:]\s*(.*)$", piece, re.DOTALL)
            if match and match.group(1).strip():
                fields.append((match.group(1).strip(), match.group(2).strip()))
            else:
                fields.append(("", piece))
    return fields


def build_card(emoji: str, title: str, text: Optional[str], link: Optional[str],
               *, style: str = STYLE_CARD, prefix: str = "",
               max_width: int = DEFAULT_MAX_WIDTH,
               max_rows: int = DEFAULT_MAX_ROWS,
               value_width: int = DEFAULT_VALUE_WIDTH,
               footer: Optional[str] = None) -> str:
    """渲染一条通知文本。"""
    style = normalize_style(style)
    heading = " ".join(part for part in (str(emoji or "").strip(),
                                         str(title or "").strip()) if part)
    if prefix:
        heading = f"{prefix} {heading}".strip()
    body = str(text or "").strip()
    link_text = str(link or "").strip()

    if style == STYLE_PLAIN:
        lines = [heading] if heading else []
        if body:
            lines.append(body)
        if link_text:
            lines.append(link_text)
        return "\n".join(lines)

    if style in (STYLE_COMPACT, STYLE_MINIMAL):
        lines = [heading] if heading else []
        for name, item in split_fields(body):
            lines.append(f"· {name}：{item}" if name else f"· {item}")
        if footer:
            lines.append(f"· {footer}")
        if link_text:
            lines.append(f"· 🔗 {shorten_url(link_text, 48)}")
        return "\n".join(lines)

    # ---- 卡片样式：先算内容，再按内容宽度决定边框宽度 ----
    rows: List[str] = []
    fields = split_fields(body)
    if len(fields) > max_rows:
        hidden = len(fields) - max_rows
        fields = fields[:max_rows]
        fields.append(("", f"（另有 {hidden} 项已省略）"))
    for name, item in fields:
        if name:
            rows.append(f"{name}  {truncate_width(item, value_width)}")
        else:
            rows.extend(wrap_text(item, value_width + len(name) + 2))

    inner = max(
        [display_width(heading)] + [display_width(row) for row in rows] or [MIN_WIDTH]
    )
    inner = max(min(inner, max_width), MIN_WIDTH - 2)

    top = "╭" + "─" * (inner + 2) + "╮"
    bottom = "╰" + "─" * (inner + 2) + "╯"
    lines = [top]
    if heading:
        lines.append("│ " + pad_width(truncate_width(heading, inner), inner) + " │")
        if rows or footer or link_text:
            lines.append("├" + "─" * (inner + 2) + "┤")
    for row in rows:
        lines.append("│ " + pad_width(truncate_width(row, inner), inner) + " │")
    if footer:
        if rows:
            lines.append("├" + "─" * (inner + 2) + "┤")
        lines.append("│ " + pad_width(truncate_width(footer, inner), inner) + " │")
    if link_text:
        if rows or footer:
            lines.append("├" + "─" * (inner + 2) + "┤")
        lines.append("│ " + pad_width(truncate_width(shorten_url(link_text, inner), inner),
                                      inner) + " │")
    lines.append(bottom)
    return "\n".join(lines)


def build_forward_nodes(emoji: str, title: str, text: Optional[str],
                        link: Optional[str], *, bot_name: str = "MoviePilot",
                        bot_uin: str = "10000",
                        footer: Optional[str] = None,
                        max_rows: int = DEFAULT_MAX_ROWS,
                        ) -> List[Dict[str, Any]]:
    """把一条通知拆成合并转发的多个节点。

    合并转发（NapCat ``send_forward_msg``）在 QQ 里折叠成一张卡片，点开是逐条消息，
    适合字段很多的入库通知：既不刷屏，也不用把正文压缩成一行。
    """
    from .napcat import node_from_text

    heading = " ".join(part for part in (str(emoji or "").strip(),
                                         str(title or "").strip()) if part)
    nodes: List[Dict[str, Any]] = []
    if heading:
        nodes.append(node_from_text(bot_name, f"【{heading}】", uin=bot_uin))
    fields = split_fields(text)
    if len(fields) > max_rows:
        fields = fields[:max_rows] + [("", "（其余内容已省略）")]
    for name, item in fields:
        nodes.append(node_from_text(
            bot_name, f"{name}：{item}" if name else item, uin=bot_uin,
        ))
    if footer:
        nodes.append(node_from_text(bot_name, footer, uin=bot_uin))
    if link:
        nodes.append(node_from_text(bot_name, f"🔗 {link}", uin=bot_uin))
    return nodes


def build_test_card(plugin_name: str, version: str, transport_label: str,
                    target_summary: str, now_text: str,
                    style: str = STYLE_CARD) -> str:
    """渲染测试通知，便于直观检查排版效果。"""
    return build_card(
        "✅", "OneBot 通知测试",
        f"通道：{transport_label}\n目标：{target_summary}\n时间：{now_text}",
        None, style=style, footer=f"{plugin_name} v{version}",
    )


def build_status_card(plugin_name: str, version: str, transport_label: str,
                      endpoint: str, stats: Dict[str, int], queue_size: int,
                      targets: str, style: str = STYLE_CARD) -> str:
    """渲染运行状态卡片。"""
    body = "\n".join([
        f"通道：{transport_label}",
        f"地址：{endpoint or '未配置'}",
        f"目标：{targets}",
        f"队列：{queue_size} 待发送",
        f"接收 {stats.get('received', 0)} / 发送 {stats.get('sent', 0)}",
        f"失败 {stats.get('failed', 0)} / 过滤 {stats.get('filtered', 0)}",
        f"去重 {stats.get('deduped', 0)} / 丢弃 {stats.get('dropped', 0)}",
    ])
    return build_card("🐋", "OneBot 通知运行状态", body, None,
                      style=style, footer=f"{plugin_name} v{version}")
