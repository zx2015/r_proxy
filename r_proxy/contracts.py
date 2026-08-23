"""跨层共享的数据契约。

对应设计：docs/design/ARCH_OVERVIEW.md「跨层数据契约」。

这些类型被 protocol、decision、egress、state 多层共用。它们不能放在任何一层
内部——决策层禁止导入 protocol（见 tests/test_architecture.py），而两层都需要
``RequestTarget``。本模块只含数据定义，不做任何 I/O，因此可被所有层安全导入。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, auto


class Method(Enum):
    """HTTP 方法及其幂等性。"""

    GET = auto()
    HEAD = auto()
    PUT = auto()
    DELETE = auto()
    OPTIONS = auto()
    TRACE = auto()
    POST = auto()
    PATCH = auto()
    CONNECT = auto()
    OTHER = auto()

    @property
    def idempotent(self) -> bool:
        # OTHER 归入非幂等：未知方法的语义无从判断，重放可能造成副作用。
        return self not in (Method.POST, Method.PATCH, Method.OTHER)

    @classmethod
    def parse(cls, raw: str) -> Method:
        return cls.__members__.get(raw.upper(), cls.OTHER)


class FailureKind(Enum):
    """失败归类，决定计入哪种计数。见 PRD §4.3.6。"""

    UPSTREAM_ERROR = auto()  # 出口本身不可用，计入全局熔断
    ROUTE_ERROR = auto()  # 经该出口到不了此目标，只记 (host, upstream) 负面记忆
    CAPABILITY_MISMATCH = auto()  # 结构性不可达，两者都不计
    NOT_A_FAILURE = auto()  # 如空闲连接回收的 408


class AddressFamily(Enum):
    """目标 host 的地址族。UNKNOWN 表示域名且尚未解析。"""

    IPV4_ONLY = auto()
    IPV6_ONLY = auto()
    DUAL = auto()
    UNKNOWN = auto()

    @classmethod
    def of_literal(cls, host: str) -> AddressFamily:
        """判断 host 是否为 IP 字面量。域名返回 UNKNOWN（需 DNS 才能确定）。"""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return cls.UNKNOWN
        return cls.IPV6_ONLY if addr.version == 6 else cls.IPV4_ONLY


class Headers:
    """保序、允许重复键、查找不区分大小写的头部集合。

    不能用 ``dict``：``Set-Cookie`` 等头部允许出现多次，用 dict 会静默丢弃。
    转发时必须原样保留顺序与重复。
    """

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        self._items: tuple[tuple[str, str], ...] = tuple(items)

    def get(self, name: str, default: str | None = None) -> str | None:
        """返回首个匹配的值。"""
        lowered = name.lower()
        for key, value in self._items:
            if key.lower() == lowered:
                return value
        return default

    def get_all(self, name: str) -> tuple[str, ...]:
        lowered = name.lower()
        return tuple(v for k, v in self._items if k.lower() == lowered)

    def items(self) -> tuple[tuple[str, str], ...]:
        return self._items

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Headers) and self._items == other._items

    def __repr__(self) -> str:
        # 只暴露键名。头部值可能含 Authorization 凭据、Cookie 等敏感数据，
        # 而 repr 常出现在异常回溯与日志里。
        return f"Headers({[k for k, _ in self._items]!r})"


@dataclass(frozen=True, slots=True)
class RequestTarget:
    """路由决策所需的请求特征。不含请求体、不含敏感头。"""

    host: str
    port: int
    method: Method
    url: str | None
    is_connect: bool
    family: AddressFamily

    @property
    def is_private_literal(self) -> bool:
        """目标是否为私网、回环或链路本地的 IP **字面量**。

        域名一律返回 ``False``：判断它落在哪个网段要先解析，而热路径上不做
        解析（与 :attr:`family` 对域名给 ``UNKNOWN`` 是同一条理由）。用户要让
        某个内网域名直连，写一条规则即可。
        """
        try:
            addr = ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return addr.is_private or addr.is_loopback or addr.is_link_local

    @property
    def authority(self) -> str:
        """用于日志与错误消息的 ``host:port``，IPv6 补方括号。"""
        host = f"[{self.host}]" if self.family is AddressFamily.IPV6_ONLY else self.host
        return f"{host}:{self.port}"
