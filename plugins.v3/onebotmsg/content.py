"""通知内容组织：把宿主事件整理成"可渲染的字段 + 模板变量"。

和排版（:mod:`.render`）的分工：

* 本模块负责**内容**——标题怎么起、图标用哪个、哪些字段进通知、模板能取到什么变量；
* ``render`` 负责**形式**——边框、对齐、缩进。

ContentStage 是两者之间与插件之间的传递结构：插件从事件里组装它，渲染时统一消费。
"""

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .template import build_helpers, flatten_variables, render as render_template

# 消息类型 -> (展示名, 图标)。键为 MessageType 成员名，跨版本稳定。
MESSAGE_TYPE_STYLE: Dict[str, Tuple[str, str]] = {
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

# 默认的合并转发节点上限，避免一条通知被拆成几十个节点
MAX_FORWARD_NODES = 12


def emoji_of(type_name: Optional[str]) -> str:
    """取消息类型对应的图标。"""
    if not type_name:
        return "📢"
    return MESSAGE_TYPE_STYLE.get(type_name, ("", "📢"))[1]


def type_title_of(type_name: Optional[str]) -> str:
    """取消息类型的展示名。"""
    if not type_name:
        return "通知"
    return MESSAGE_TYPE_STYLE.get(type_name, (str(type_name), ""))[0]


@dataclass
class ContentStage:
    """一条待渲染的通知内容。

    ``fields`` 是按顺序排好的 ``(字段名, 值)``；``raw_text`` 是宿主原文，
    在模板没有完全接管内容时作为兜底；``event`` 保留完整事件数据，供模板取用。
    """

    title: str = ""
    type_name: Optional[str] = None
    fields: List[Tuple[str, str]] = field(default_factory=list)
    raw_text: str = ""
    image: Optional[str] = None
    link: Optional[str] = None
    userid: Optional[str] = None
    username: Optional[str] = None
    event: Dict[str, Any] = field(default_factory=dict)

    @property
    def emoji(self) -> str:
        """本条通知应使用的图标。"""
        return emoji_of(self.type_name)

    def body(self) -> str:
        """把字段拼成 ``名：值`` 的正文（模板未接管时使用）。"""
        if self.fields:
            return "\n".join(
                f"{name}：{value}" if name else str(value) for name, value in self.fields
            )
        return self.raw_text

    def template_variables(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造模板变量表。

        除了事件自带的扁平键（``media.tmdb_id``、``note.size`` 等），再补一批
        通知场景常用量，让模板能直接写 ``${title}``、``${season}``、``${files}``。
        """
        variables: Dict[str, Any] = {}
        # 1) 事件数据摊平，模板可写 note.xxx / media.xxx
        variables.update(flatten_variables(self.event))
        # 2) 常用量
        variables.update({
            "title": self.title,
            "text": self.raw_text,
            "body": self.body(),
            "type": self.type_name or "",
            "type_name": type_title_of(self.type_name),
            "emoji": self.emoji,
            "image": self.image or "",
            "link": self.link or "",
            "userid": self.userid or "",
            "username": self.username or "",
            "now": time.strftime("%Y-%m-%d %H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "fields": dict(self.fields),
            "field_names": [name for name, _ in self.fields],
            "lines": [value for _, value in self.fields],
            "fields_text": self.body(),
        })
        # 3) 字段名同时提供顶层键：${季集} 这类中文键也能取到
        for name, value in self.fields:
            if name:
                variables.setdefault(name, value)
        variables.update(extra or {})
        return variables

    def apply_template(self, source: str) -> Tuple[str, Optional[str]]:
        """按用户模板渲染正文，返回 ``(文本, 错误说明)``。"""
        return render_template(source, self.template_variables())


def fields_from_lines(text: Optional[str]) -> List[Tuple[str, str]]:
    """把多行正文解析成 ``(字段名, 值)``。

    与宿主通知正文的约定一致：形如 ``季集：S01E01``。解析策略：

    * 逐行处理，行内既可能是一个字段，也可能是 ``a：1，b：2`` 这样的多字段；
    * 字段名不能包含分隔符，因此 ``时间：2026-10-10 02:34:17`` 的值里保留冒号；
    * 完全不含 ``标签：`` 的行按整行保留，不按逗号硬拆。
    """
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


def describe_variables(stage: ContentStage, limit: int = 40) -> str:
    """列出当前通知可用的模板变量，供配置页提示与排查。

    多行值（如 ``body``）会把后续行折叠掉，否则一个变量就把列表撑爆。
    """
    variables = stage.template_variables()
    lines: List[str] = []
    for key in sorted(variables):
        if key.startswith("_"):
            continue
        value = variables[key]
        if callable(value):
            continue
        text = " ".join(str(value).split())
        if len(text) > 40:
            text = text[:40] + "…"
        lines.append(f"{key} = {text}")
        if len(lines) >= limit:
            lines.append(f"…… 共 {len(variables)} 个变量，此处只列出前 {limit} 个")
            break
    return "\n".join(lines)


def sample_stage() -> ContentStage:
    """构造一条示例通知，用于配置页的模板预览与自检。"""
    return ContentStage(
        title="整理入库 药屋少女的呢喃 (2023)",
        type_name="Organize",
        fields=[
            ("季集", "S03E50"),
            ("分类", "日番"),
            ("来源", "themoviedb:220542"),
            ("文件数", "1"),
            ("体积", "879.02 MB"),
        ],
        raw_text="季集：S03E50\n分类：日番\n来源：themoviedb:220542",
        image="https://image.tmdb.org/t/p/original/example.jpg",
        link="http://moviepilot.local:3000/#/media",
        username="admin",
        event={
            "type": "Organize",
            "media": {"title": "药屋少女的呢喃", "year": "2023",
                      "media_source": "themoviedb", "media_id": "220542"},
            "note": {"size": 921000000, "files": ["药屋少女的呢喃 - S03E50.mkv"]},
        },
    )


__all__ = [
    "ContentStage",
    "MAX_FORWARD_NODES",
    "MESSAGE_TYPE_STYLE",
    "build_helpers",
    "describe_variables",
    "emoji_of",
    "fields_from_lines",
    "sample_stage",
    "type_title_of",
]
