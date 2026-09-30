"""决策层的数据契约。

对应设计：docs/design/ARCH_OVERVIEW.md §9、docs/design/DD_SWITCHING.md §2。

跨越协议层与决策层的基础类型（``Method``、``Headers``、``RequestTarget``、
``FailureKind``）在 :mod:`r_proxy.contracts`；这里只放决策层自己产出与消费的类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal

from r_proxy.contracts import FailureKind, Headers, Method

DecisionSource = Literal["rule", "manual", "auto", "priority"]


@dataclass(frozen=True, slots=True)
class Decision:
    """Router 的输出：按什么顺序尝试哪些出口。"""

    chain: tuple[str, ...]
    source: DecisionSource
    # 命中规则在表中的 0 基序号。写进日志与 request_log.rule_origin，让用户
    # 能把一次失败对回界面上的那一行。
    rule_position: int | None = None
    # 规则强制路由时为 False：候选链长度恒为 1，失败原样返回不顺延。
    switchable: bool = True
    # 候选链为空时说明为什么，供日志与错误响应区分「没有出口」和「都不可用」。
    empty_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """单次出口尝试的结果，由 egress 层产出。

    ``error`` 只存异常类型名或 errno 名，**不含出口地址**——即便有人把它
    拼进响应体也不会泄露内网拓扑。
    """

    upstream: str
    ok: bool
    status: int | None = None
    error: str | None = None
    kind: FailureKind = FailureKind.ROUTE_ERROR
    response_headers: Headers = field(default_factory=Headers)
    elapsed_ms: int = 0
    bytes_up: int = 0
    bytes_down: int = 0


@dataclass(frozen=True, slots=True)
class SwitchContext:
    """判据所需的请求上下文。

    只描述事实，不持有可变状态：限流器由调用方单独传给
    :meth:`~r_proxy.decision.switching.SwitchPolicy.should_switch`。
    """

    method: Method
    is_connect: bool
    # 必须在字节真正写入 socket 之后才置 True，而不是构造请求对象时。
    request_sent: bool
    replayable: bool
    response_started: bool
    host: str
    attempt_index: int = 0
    # 读客户端请求体超时（客户端声明了长度却不发/发得太慢）。与 `replayable`
    # 语义不同：`replayable` 只回答「已转发的字节能不能重放给下一个出口」，
    # 而这里回答「客户端本身还会不会再发数据」——换哪个出口都等不到客户端
    # 补发剩余的 body，因此即便 `replayable` 仍为 True（尚未超过缓冲上限）
    # 也不该切换。`switch_buffer_bytes: 0` 时 `replayable` 从一开始就是
    # False，但传输层失败（字节还没发出）仍可切换（DD_SWITCHING §7.6）——
    # 这条与 `replayable` 分开建模，才不会把那条既有规则连带破坏。
    client_body_timeout: bool = False


class SwitchReason(Enum):
    TRANSPORT_FAILURE = auto()
    PROXY_LAYER_STATUS = auto()
    AMBIGUOUS_STATUS = auto()
    EGRESS_RELATED_STATUS = auto()
    INCOMPLETE_REQUEST = auto()
    # 分类表不认识这个状态码，切换纯粹因为用户把它列进了 switch_on_status。
    CONFIGURED_STATUS = auto()


class KeepReason(Enum):
    TARGET_HANDLED = auto()
    CDN_ORIGIN_ERROR = auto()
    STATUS_FROM_TARGET = auto()
    NON_IDEMPOTENT = auto()
    NOT_REPLAYABLE = auto()
    RESPONSE_STARTED = auto()
    RATE_LIMITED = auto()
    IDLE_CONNECTION_RECYCLED = auto()
    CLIENT_BODY_TIMEOUT = auto()


@dataclass(frozen=True, slots=True)
class SwitchVerdict:
    """切不切，以及为什么。

    ``keep_reason`` 不是调试信息——它要写进 ``request_log``，用户在 Web 界面
    看到「为什么没有切换」全靠它。
    """

    switch: bool
    retry_same_upstream: bool = False
    switch_reason: SwitchReason | None = None
    keep_reason: KeepReason | None = None
    failure_kind: FailureKind = FailureKind.ROUTE_ERROR
