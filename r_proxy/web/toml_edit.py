"""`config.toml` 的风格保留编辑。

对应设计：docs/design/DD_WEB.md §6.2，需求：WEBUI_SPEC.md §6.2.2。

这里全是**纯文本函数**：输入原文与改动意图，输出新文本，不碰文件系统。写入的
编排在 :mod:`r_proxy.web.config_writer`。分开是为了让「注释有没有被保住」这类
判断可以用一行字符串断言验证，不必先造出一个应用实例。

`tomlkit` 只在本模块与 `config_writer` 中出现：`config` 包读配置一律用标准库
`tomllib`，否则 `--no-web` 形态会失去零依赖性质（DD_WEB §6.2.2）。

**改值走 `tomlkit`，增删表走行区间。** 理由见 §6.2.3：`tomlkit` 把「视觉上位于
某个表之前」的注释块存在**前一项的尾部**，因此 `aot.append()` 与 `del aot[i]`
都会让注释错位一格——删掉 `home` 之后描述 `home` 的注释会留下来盖在 `direct`
头上。改值不移动任何项，没有这个问题。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit.items import AoT, Table

# 出口表里允许 Web 改写的键。**白名单**而非黑名单：客户端提交未知键时应当
# 拒绝，而不是原样写进配置文件——写进去之后加载器会以 E_UNKNOWN_KEY 拒绝启动。
UPSTREAM_FIELDS = ("type", "address", "priority", "enabled", "connect_timeout", "read_timeout")

_AOT_HEADER = re.compile(r"^\s*\[\[\s*upstreams\s*\]\]")
_ANY_HEADER = re.compile(r"^\s*\[")
# `[upstreams.auth]` 是被编辑出口的子表，不构成「下一个表」的边界。
_CHILD_HEADER = re.compile(r"^\s*\[\s*upstreams\s*\.")
_COMMENT = re.compile(r"^\s*#")

# 原文 → 新文。写入编排只认这一种形状，因此「改什么」与「怎么落盘」互不知情。
Transform = Callable[[str], str]


class TomlEditError(Exception):
    """原文无法按预期结构编辑。"""


@dataclass(frozen=True, slots=True)
class _Span:
    """一个 `[[upstreams]]` 表在文本中的位置。

    ``end`` 不含表后面的空行与注释：那些注释属于**下一个**表，删除时必须留下。
    """

    name: str
    header: int
    end: int


def apply_settings(text: str, changes: dict[str, object]) -> str:
    """按点分键就地改值，只动被指名的那些键。

    不做「反向序列化整个快照」：那会把所有被默认值填充过的项都显式写进文件，
    配置从几十行涨到几百行，而用户并没有改动它们。
    """
    doc = tomlkit.parse(text)
    for dotted, value in changes.items():
        _set_nested(doc, dotted.split("."), value)
    return tomlkit.dumps(doc)


def upsert_upstream(text: str, *, name: str, fields: dict[str, object]) -> str:
    """改写已有出口，或在最后一个出口之后插入一个新的。

    改写时只动 ``fields`` 里出现的键：``[upstreams.auth]`` 子表与表内注释必须
    原样保留，否则「改一下优先级」会顺手把上级代理的凭据抹掉。
    """
    doc = tomlkit.parse(text)
    table = _find_upstream(doc, name)
    if table is not None:
        _assign(table, fields)
        return tomlkit.dumps(doc)

    block = _render_upstream(name, fields)
    lines = text.splitlines(keepends=True)
    spans = _upstream_spans(lines)
    if not spans:
        return _ensure_trailing_newline(text) + "\n" + block
    # 插在最后一个出口的表体之后、它后面的空行与注释之前：那些注释属于再下面
    # 的表，跨过去会让新出口顶着别人的注释（DD_WEB §6.2.3）。
    at = spans[-1].end
    return "".join(lines[:at]) + "\n" + block + "".join(lines[at:])


def remove_upstream(text: str, name: str) -> str:
    """删掉某个出口：表体、它的子表，以及**紧邻的前置注释块**。

    前置注释描述的正是这个出口，留着会指向一个已经不存在的条目；而表后面的
    注释属于下一个出口，必须原样留下。
    """
    lines = text.splitlines(keepends=True)
    span = next((s for s in _upstream_spans(lines) if s.name == name), None)
    if span is None:
        raise TomlEditError(f"出口不存在: {name}")
    start = _leading_comment_start(lines, span.header)
    result = "".join(lines[:start] + lines[span.end :])
    _verify_removed(result, name)
    return result


def set_priorities(text: str, priorities: dict[str, int]) -> str:
    """批量改优先级。全部改完才产出文本，**不存在只改了一半的中间态**。"""
    doc = tomlkit.parse(text)
    for name, value in priorities.items():
        table = _find_upstream(doc, name)
        if table is None:
            raise TomlEditError(f"出口不存在: {name}")
        table["priority"] = value
    return tomlkit.dumps(doc)


# --------------------------------------------------------------------------
# 行区间定位
# --------------------------------------------------------------------------


def _upstream_spans(lines: list[str]) -> list[_Span]:
    spans: list[_Span] = []
    for index, line in enumerate(lines):
        if not _AOT_HEADER.match(line):
            continue
        end = _table_end(lines, index)
        spans.append(_Span(name=_name_of(lines[index:end]), header=index, end=end))
    return spans


def _table_end(lines: list[str], header: int) -> int:
    """表体结束位置（不含尾部空行与注释）。"""
    end = len(lines)
    for index in range(header + 1, len(lines)):
        line = lines[index]
        if _ANY_HEADER.match(line) and not _CHILD_HEADER.match(line):
            end = index
            break
    while end > header + 1 and (_COMMENT.match(lines[end - 1]) or not lines[end - 1].strip()):
        end -= 1
    return end


def _leading_comment_start(lines: list[str], header: int) -> int:
    """紧邻表头的连续注释行的起点。

    中间隔了空行就不算「紧邻」——那是章节分隔，不是这个出口的说明。
    """
    start = header
    while start > 0 and _COMMENT.match(lines[start - 1]):
        start -= 1
    return start


def _name_of(block: list[str]) -> str:
    """解析单个 `[[upstreams]]` 块，取它的 ``name``。

    不手写 `name = "..."` 的正则：值可能带引号转义、行尾注释或单引号形式，
    自己解析必然与 TOML 规范有出入。
    """
    try:
        data = tomllib.loads("".join(block))
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"无法解析 [[upstreams]] 块: {exc}") from exc
    items = data.get("upstreams") or []
    if len(items) != 1 or not isinstance(items[0].get("name"), str):
        raise TomlEditError("每个 [[upstreams]] 表都必须有字符串 name")
    name: str = items[0]["name"]
    return name


def _verify_removed(text: str, name: str) -> None:
    """删完之后必须仍是合法 TOML，且这个出口真的没了。

    行区间定位依赖「顶层表头在行首」这一约定，多行数组等写法理论上能骗过它。
    与其去穷举那些写法，不如在这里确认结果——写坏配置的代价远大于多解析一次。
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"删除后的配置不是合法 TOML: {exc}") from exc
    if any(u.get("name") == name for u in data.get("upstreams") or []):
        raise TomlEditError(f"出口 {name} 未被删除")


# --------------------------------------------------------------------------
# tomlkit 侧
# --------------------------------------------------------------------------


def _render_upstream(name: str, fields: dict[str, object]) -> str:
    """把新出口渲染成 `[[upstreams]]` 文本。

    必须经 `tomlkit` 序列化，不能用字符串拼接：``name`` 与 ``address`` 来自
    请求体，含引号或换行时拼接出来的是攻击者可控的 TOML 片段。
    """
    doc = tomlkit.document()
    table = tomlkit.table()
    table["name"] = name
    _assign(table, fields)
    array = tomlkit.aot()
    array.append(table)
    doc["upstreams"] = array
    return tomlkit.dumps(doc)


def _find_upstream(doc: tomlkit.TOMLDocument, name: str) -> Table | None:
    array = doc.get("upstreams")
    if array is None:
        return None
    if not isinstance(array, AoT):
        raise TomlEditError("upstreams 必须是表数组（[[upstreams]]）")
    for table in array:
        if table.get("name") == name:
            # AoT.__iter__ 的元素类型在 tomlkit 里是 Any，收窄回 Table。
            assert isinstance(table, Table)
            return table
    return None


def _assign(table: Table, fields: dict[str, object]) -> None:
    for key, value in fields.items():
        if key not in UPSTREAM_FIELDS:
            raise TomlEditError(f"不允许写入的字段: {key}")
        if value is None:
            # `direct` 没有地址。留一个 `address = ""` 会让加载器把它当成配错了
            # 地址的上级代理。
            table.pop(key, None)
        else:
            table[key] = value


def _set_nested(container: Any, keys: list[str], value: object) -> None:
    """沿点分路径下钻，缺失的中间表就地创建。

    创建中间表用 `tomlkit.table()` 而不是普通 dict：后者写出来是行内表
    （`routing = {connect_timeout = 5}`），与文件里其余部分的风格不一致。
    """
    head, rest = keys[0], keys[1:]
    if not rest:
        container[head] = value
        return
    child = container.get(head)
    if child is None:
        child = tomlkit.table()
        container[head] = child
    _set_nested(child, rest, value)


def _ensure_trailing_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"
