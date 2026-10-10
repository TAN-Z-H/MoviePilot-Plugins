"""通知内容模板：让用户自己决定"通知里显示哪些字段、怎么排版"。

参考聚合消息通知插件的做法（其 OneBot-11 渠道用 Mako 模板渲染通知正文），
但本插件不允许引入额外依赖，因此这里实现一个**受限但够用**的模板引擎，
覆盖面就是通知场景真正会用到的四件事：

1. 变量替换：``${title}``、``${media.tmdb_id}``、``${note['size']}``
2. 兜底取值：``${year or '未知'}``（``or`` / ``and`` / 三目都支持）
3. 条件块：``% if season:`` … ``% else:`` … ``% endif``
4. 循环块：``% for item in files:`` … ``% endfor``

渲染在受限命名空间里求值：**可用内建函数被白名单限制**，模板既拿不到
``__import__`` 之类的逃逸入口，也不会因为表达式写错而让整条通知消失——
出错时按原文返回，并在日志里留下原因。

模板可用的辅助函数：

* ``fmt_time(value, pattern)``：格式化时间（支持时间戳与常见字符串）
* ``size(value)``：字节数转 ``1.23 GB``
* ``shorten(text, n)``：按显示宽度截断
* ``emoji_of(type_name)``：取消息类型对应的图标
````````````````````````````````````````````````````````````````````````````````````````
"""

import ast
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 模板占位符：${ 表达式 }
_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]*)\}")
# 控制行：% if / % elif / % else / % endif / % for / % endfor
_CONTROL_RE = re.compile(r"^\s*%(.*)$")
# 允许在表达式中使用的内建函数（白名单，避免模板逃逸到运行环境）
_ALLOWED_BUILTINS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "round": round,
    "abs": abs,
    "min": min,
    "max": max,
    "sorted": sorted,
    "list": list,
    "dict": dict,
    "set": set,
    "any": any,
    "all": all,
    "sum": sum,
    "enumerate": enumerate,
    "range": range,
    "zip": zip,
    "bool": bool,
    "None": None,
    "True": True,
    "False": False,
}


class TemplateError(ValueError):
    """模板语法或求值错误。"""


def format_size(value: Any) -> str:
    """把字节数格式化为便于阅读的体积。"""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.2f} {units[index]}" if index else f"{int(size)} {units[index]}"


def display_width(text: str) -> int:
    """计算显示列数（中日韩与 emoji 按 2 列）。"""
    width = 0
    for char in str(text or ""):
        if char == "\n" or unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate(text: Any, limit: int, ellipsis: str = "…") -> str:
    """按显示列数截断。"""
    value = str(text or "")
    if limit <= 0 or display_width(value) <= limit:
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


def format_time(value: Any, pattern: str = "%Y-%m-%d %H:%M") -> str:
    """格式化时间，支持时间戳、datetime 与常见字符串。"""
    import datetime

    if value is None or value == "":
        return ""
    moment = None
    if isinstance(value, (int, float)):
        try:
            moment = datetime.datetime.fromtimestamp(float(value))
        except (OSError, OverflowError, ValueError):
            moment = None
    elif isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                moment = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if moment is None:
        return str(value)
    try:
        return moment.strftime(pattern)
    except (ValueError, TypeError):
        return str(value)


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """按 ``a.b`` / ``a[0]`` 形式从嵌套结构里取值。"""
    current = data
    for token in re.findall(r"[^.\[\]]+", str(path or "")):
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(token, default)
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return default
        else:
            current = getattr(current, token, default)
    return current


def flatten_variables(data: Any, prefix: str = "", depth: int = 3) -> Dict[str, Any]:
    """把嵌套字典摊平成 ``{"media.tmdb_id": ...}`` 形式的变量表。

    模板里既能写 ``${media.tmdb_id}``，也能直接用扁平键；扁平键在展示
    "当前可用变量" 时更直观。
    """
    result: Dict[str, Any] = {}
    if depth < 0 or not isinstance(data, dict):
        return result
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            result.update(flatten_variables(value, prefix=f"{name}.", depth=depth - 1))
        else:
            result[name] = value
    return result


class SafeEvaluator:
    """受限表达式求值器。

    只接受单个表达式（``ast.Expression``），并拒绝属性访问下划线开头的名字，
    因此模板无法通过 ``().__class__.__bases__`` 这类路径逃逸。
    """

    def __init__(self, variables: Dict[str, Any], helpers: Optional[Dict[str, Any]] = None):
        """绑定变量与辅助函数；嵌套结构会转成可点号访问的盒子。"""
        self._variables: Dict[str, Any] = {
            key: _box(value) for key, value in (variables or {}).items()
        }
        self._variables.update(helpers or {})

    def set(self, name: str, value: Any) -> None:
        """为循环变量赋值（值同样装箱，便于继续用点号）。"""
        self._variables[name] = _box(value)

    def evaluate(self, expression: str) -> Any:
        """求值一个表达式，失败抛 :class:`TemplateError`。"""
        text = str(expression or "").strip().rstrip(":").strip()
        if not text:
            return ""
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as err:
            raise TemplateError(f"表达式语法错误：{text!r}（{err.msg}）") from err
        self._validate(tree)
        try:
            return eval(  # noqa: S307 - 已用 AST 白名单校验，且命名空间受限
                compile(tree, "<template>", "eval"),
                {"__builtins__": _ALLOWED_BUILTINS},
                dict(self._variables),
            )
        except NameError:
            # 变量不存在时按"空值"处理：宿主并非每条通知都带全部字段
            # （例如订阅通知没有季集、插件通知没有图片），
            # 让缺字段渲染成空串，比整条通知退回默认内容更有用。
            if _referenced_names(tree) - set(self._variables) - set(_ALLOWED_BUILTINS):
                return ""
            raise TemplateError(f"表达式求值失败：{text!r}（未定义的变量）")
        except TemplateError:
            raise
        except Exception as err:  # noqa: BLE001 - 求值失败统一转成模板错误
            raise TemplateError(f"表达式求值失败：{text!r}（{type(err).__name__}: {err}）") from err

    @staticmethod
    def _validate(tree: ast.AST) -> None:
        """拒绝不安全的节点：下划线属性、双下划线名字、导入与 lambda。"""
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Lambda,
                                ast.Await, ast.Yield, ast.NamedExpr)):
                raise TemplateError(f"表达式含不允许的语法：{type(node).__name__}")
            if isinstance(node, ast.Attribute):
                if str(node.attr).startswith("_"):
                    raise TemplateError("表达式不允许访问下划线开头的属性")
            if isinstance(node, ast.Name):
                if str(node.id).startswith("__"):
                    raise TemplateError("表达式不允许使用双下划线名字")
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("eval", "exec", "open",
                                                              "compile", "getattr",
                                                              "setattr", "globals",
                                                              "locals", "vars", "input"):
                    raise TemplateError(f"表达式不允许调用 {func.id}")


class _Box:
    """把字典包装成支持点号访问的盒子：``${media.tmdb_id}`` 才能像 Mako 那样写。

    只暴露原始键，且下划线开头的键在求值阶段已被拒绝，因此不会成为逃逸入口。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Dict[str, Any]):
        """保存原始字典。"""
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        """按属性取键；缺失返回空串而不是抛错，让模板对缺字段更宽容。"""
        if name.startswith("_"):
            raise AttributeError(name)
        data = object.__getattribute__(self, "_data")
        if name in data:
            return _box(data[name])
        return ""

    def __getitem__(self, key: Any) -> Any:
        """支持下标访问。"""
        data = object.__getattribute__(self, "_data")
        try:
            return _box(data[key])
        except (KeyError, IndexError, TypeError):
            return ""

    def __contains__(self, key: Any) -> bool:
        """支持 ``in`` 判断。"""
        return key in object.__getattribute__(self, "_data")

    def __iter__(self):
        """遍历键，便于 ``% for k in xxx``。"""
        return iter(object.__getattribute__(self, "_data"))

    def __len__(self) -> int:
        """键数量。"""
        return len(object.__getattribute__(self, "_data"))

    def __bool__(self) -> bool:
        """空字典为假，便于 ``% if media:``。"""
        return bool(object.__getattribute__(self, "_data"))

    def keys(self):
        """返回键集合。"""
        return object.__getattribute__(self, "_data").keys()

    def values(self):
        """返回值集合（已装箱）。"""
        return [_box(item) for item in
                object.__getattribute__(self, "_data").values()]

    def items(self):
        """返回 ``(键, 值)`` 集合（值已装箱）。"""
        return [(key, _box(value)) for key, value in
                object.__getattribute__(self, "_data").items()]

    def get(self, key: Any, default: Any = "") -> Any:
        """取键，缺失返回默认值。"""
        data = object.__getattribute__(self, "_data")
        return _box(data[key]) if key in data else default

    def __str__(self) -> str:
        """转字符串时给出可读形式。"""
        return str(object.__getattribute__(self, "_data"))

    def __repr__(self) -> str:
        """调试展示。"""
        return f"<Box {object.__getattribute__(self, '_data')!r}>"


def _referenced_names(tree: ast.AST) -> set:
    """收集表达式里引用的裸变量名（用于区分"缺变量"与"写错表达式"）。"""
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def _box(value: Any) -> Any:
    """按需把字典/列表装箱，其它类型原样返回。"""
    if isinstance(value, _Box):
        return value
    if isinstance(value, dict):
        return _Box(value)
    if isinstance(value, (list, tuple)):
        return [_box(item) for item in value]
    return value


def build_helpers() -> Dict[str, Any]:
    """构造模板可用的辅助函数集合。"""
    return {
        "fmt_time": format_time,
        "size": format_size,
        "shorten": truncate,
        "width": display_width,
        "int": int,
        "str": str,
    }


class Template:
    """一个已编译的通知内容模板。

    编译结果是一棵节点树（文本 / 输出 / 条件 / 循环），渲染时按节点走，
    既不使用 ``exec``，也不把用户输入拼进可执行代码。
    """

    # 节点类型
    NODE_TEXT = "text"
    NODE_OUTPUT = "output"
    NODE_IF = "if"
    NODE_FOR = "for"

    def __init__(self, nodes: Sequence[Dict[str, Any]]):
        """保存节点树。"""
        self._nodes = list(nodes)

    @classmethod
    def compile(cls, source: str) -> "Template":
        """把模板文本编译成节点树。

        行尾换行在编译期补回：模板是按行解析的，如果不在每个文本行后补 ``\\n``，
        多行模板会被渲染成一整行。
        """
        lines = str(source or "").split("\n")
        nodes, index = cls._parse_block(lines, 0, terminators=())
        if index < len(lines):
            leftover = lines[index].strip()
            raise TemplateError(f"模板存在未闭合的控制块：{leftover!r}")
        return cls(nodes)

    @staticmethod
    def _directive(line: str) -> Tuple[str, str]:
        """把控制行拆成 ``(关键字, 参数)``，并把行尾冒号去掉。

        ``% if season:`` 与 ``% if season`` 两种写法都要支持——大多数模板语言
        习惯带冒号，Python 语法则不带，去掉后交给表达式求值器更省事。
        """
        body = str(line or "").strip().lstrip("%").strip().rstrip(":").strip()
        if not body:
            return "", ""
        keyword, _, rest = body.partition(" ")
        return keyword.strip(), rest.strip()

    @classmethod
    def _parse_block(cls, lines: List[str], start: int,
                     terminators: Tuple[str, ...]) -> Tuple[List[Dict[str, Any]], int]:
        """解析一个块，直到遇到终结指令（endif / endfor / else / elif）。"""
        nodes: List[Dict[str, Any]] = []
        index = start
        while index < len(lines):
            line = lines[index]
            match = _CONTROL_RE.match(line)
            if match:
                keyword, argument = cls._directive(line)
                if keyword in terminators:
                    return nodes, index
                if keyword == "if":
                    node, index = cls._parse_if(lines, index, argument)
                    nodes.append(node)
                    continue
                if keyword == "for":
                    node, index = cls._parse_for(lines, index, argument)
                    nodes.append(node)
                    continue
                # 清空行（只有一个 %）直接忽略；未知指令按普通文本处理，
                # 避免用户写错一行就丢掉整条通知。
                if keyword:
                    nodes.extend(cls._parse_text_line(line))
                    nodes.append({"type": cls.NODE_TEXT, "value": "\n"})
                index += 1
                continue
            nodes.extend(cls._parse_text_line(line))
            nodes.append({"type": cls.NODE_TEXT, "value": "\n"})
            index += 1
        return nodes, index

    @classmethod
    def _parse_if(cls, lines: List[str], start: int,
                  condition: str) -> Tuple[Dict[str, Any], int]:
        """解析 ``% if`` … ``% elif`` … ``% else`` … ``% endif``。"""
        node: Dict[str, Any] = {"type": cls.NODE_IF, "branches": [], "else": []}
        current_condition = condition
        index = start + 1
        while True:
            block, index = cls._parse_block(
                lines, index, terminators=("elif", "else", "endif"),
            )
            node["branches"].append({"condition": current_condition, "nodes": block})
            if index >= len(lines):
                # 缺 endif：不报错，按已解析内容收尾，保证通知还能发出去。
                return node, index
            keyword, argument = cls._directive(lines[index])
            if keyword == "elif":
                current_condition = argument
                index += 1
                continue
            if keyword == "else":
                else_block, index = cls._parse_block(lines, index + 1,
                                                     terminators=("endif",))
                node["else"] = else_block
                if index < len(lines) and cls._directive(lines[index])[0] == "endif":
                    index += 1
                return node, index
            # endif
            index += 1
            return node, index

    @classmethod
    def _parse_for(cls, lines: List[str], start: int,
                   spec: str) -> Tuple[Dict[str, Any], int]:
        """解析 ``% for x in items:`` … ``% endfor``。"""
        names, _, source = str(spec or "").partition(" in ")
        node = {
            "type": cls.NODE_FOR,
            "names": [item.strip() for item in names.split(",") if item.strip()],
            "source": source.strip(),
            "nodes": [],
        }
        block, index = cls._parse_block(lines, start + 1, terminators=("endfor",))
        node["nodes"] = block
        if index < len(lines) and cls._directive(lines[index])[0] == "endfor":
            index += 1
        return node, index

    @staticmethod
    def _parse_text_line(line: str) -> List[Dict[str, Any]]:
        """把一行文本拆成 文本/输出 节点序列。"""
        nodes: List[Dict[str, Any]] = []
        position = 0
        for match in _PLACEHOLDER_RE.finditer(line):
            if match.start() > position:
                nodes.append({"type": Template.NODE_TEXT, "value": line[position:match.start()]})
            nodes.append({"type": Template.NODE_OUTPUT, "expression": match.group(1)})
            position = match.end()
        if position < len(line):
            nodes.append({"type": Template.NODE_TEXT, "value": line[position:]})
        if not nodes:
            nodes.append({"type": Template.NODE_TEXT, "value": line})
        return nodes

    @property
    def dynamic(self) -> bool:
        """模板是否包含任何动态节点（用于判断用户是否真的配了模板）。"""
        return any(node.get("type") != self.NODE_TEXT for node in self._nodes)

    def render(self, variables: Dict[str, Any],
               helpers: Optional[Dict[str, Any]] = None) -> str:
        """渲染模板。任一步失败都会抛出 :class:`TemplateError`，由调用方决定降级。"""
        evaluator = SafeEvaluator(variables, helpers or build_helpers())
        return "".join(self._render_nodes(self._nodes, evaluator))

    def _render_nodes(self, nodes: Sequence[Dict[str, Any]],
                      evaluator: SafeEvaluator) -> List[str]:
        """递归渲染节点列表。"""
        parts: List[str] = []
        for node in nodes:
            kind = node.get("type")
            if kind == self.NODE_TEXT:
                parts.append(str(node.get("value") or ""))
            elif kind == self.NODE_OUTPUT:
                value = evaluator.evaluate(str(node.get("expression") or ""))
                parts.append("" if value is None else str(value))
            elif kind == self.NODE_IF:
                rendered = False
                for branch in node.get("branches") or []:
                    if evaluator.evaluate(branch.get("condition") or ""):
                        parts.extend(self._render_nodes(branch.get("nodes") or [], evaluator))
                        rendered = True
                        break
                if not rendered:
                    parts.extend(self._render_nodes(node.get("else") or [], evaluator))
            elif kind == self.NODE_FOR:
                items = evaluator.evaluate(str(node.get("source") or "")) or []
                if isinstance(items, (str, bytes)) or not hasattr(items, "__iter__"):
                    continue
                names = node.get("names") or []
                for item in items:
                    if len(names) == 1:
                        evaluator.set(names[0], item)
                    elif names:
                        values = list(item) if hasattr(item, "__iter__") else [item]
                        for position, name in enumerate(names):
                            evaluator.set(name, values[position]
                                          if position < len(values) else None)
                    parts.extend(self._render_nodes(node.get("nodes") or [], evaluator))
        return parts


def render(source: str, variables: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """渲染模板并返回 ``(结果, 错误说明)``。

    出错时返回原文与错误信息：通知宁可"排版退化成默认"，也不能因为用户把
    模板写错就整条丢失。
    """
    text = str(source or "")
    if not text.strip():
        return "", None
    try:
        template = Template.compile(text)
        return template.render(variables), None
    except TemplateError as err:
        return text, str(err)
    except Exception as err:  # noqa: BLE001 - 兜底，绝不让模板异常冒泡到发送流程
        return text, f"{type(err).__name__}: {err}"
