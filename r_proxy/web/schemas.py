"""请求与响应模型。

对应设计：docs/design/DD_WEB.md §3、需求：WEBUI_SPEC.md §3。

模型只描述**对外契约**，不复用内部数据类：内部结构变化不该自动变成 API 变化，
而内部结构里有些字段（上级代理密码、`auth_token`）永远不该出现在响应里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

StatusCode = Annotated[int, Field(ge=100, le=599)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProxyInfo(Model):
    host: str
    port: int


class ConnectionsInfo(Model):
    active: int
    rejected: int
    limit: int


class RequestsInfo(Model):
    """出口尝试的累计口径。

    一次客户端请求可能尝试多个出口，因此 ``attempts`` 不等于请求数。分开命名
    以免看板上出现「成功率 > 100%」这类由口径混用造成的怪数字。
    """

    attempts: int
    success: int
    failure: int
    success_rate: float


class StorageInfo(Model):
    queue_size: int
    queue_capacity: int
    queue_high_water: int
    dropped_lossy: int
    dropped_normal: int
    dropped_critical: int
    write_errors: int
    last_flush_at: float
    flush_duration_p99_ms: float
    merge_ratio: float


class StatusResponse(Model):
    version: str
    uptime_seconds: float
    config_version: str
    proxy: ProxyInfo
    connections: ConnectionsInfo
    requests: RequestsInfo
    storage: StorageInfo


class Pagination(Model):
    """两个上限都是必需的，理由见 DD_WEB §4.3。

    ``page_size`` 无上限时一次请求百万行会占满线程池数秒，而 DNS 解析与 Web
    查询共用默认线程池——表现为「打开管理界面后新连接变慢」。``page`` 无上限时
    ``OFFSET`` 会让 SQLite 扫描并丢弃千万行。
    """

    page: int = Field(1, ge=1, le=10_000)
    page_size: int = Field(50, ge=1, le=1_000)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class LogQuery(Pagination):
    """请求日志的筛选条件。字段全部可选，缺省即不筛。

    长度与取值范围在这里就卡住：越界的值到了 SQL 里只会命中零行，但在此拒绝
    能让用户立刻看到「参数写错了」，而不是「怎么什么都查不到」。
    """

    host: str | None = Field(None, max_length=255)
    upstream: str | None = Field(None, max_length=64)
    client_addr: str | None = Field(None, max_length=255)
    status: int | None = Field(None, ge=100, le=599)
    since: int | None = Field(None, ge=0)
    until: int | None = Field(None, ge=0)


class LogItem(Model):
    id: int
    request_id: str
    client_addr: str | None
    host: str
    url: str | None
    method: str
    upstream_name: str
    upstream_priority: int | None
    attempt_index: int
    decision_source: str | None
    rule_origin: str | None
    http_status: int | None
    error: str | None
    failure_kind: str | None
    keep_reason: str | None
    elapsed_ms: int
    bytes_up: int
    bytes_down: int
    created_at: int
    # 真实传输量，来自与 ``traffic_log`` 的联表（见 web/queries.py）。``None``
    # 表示这次尝试从未传输过数据（被切换掉）或响应体仍在流式转发中——
    # 与「确实传输了 0 字节」是两回事，前端据此展示「—」而不是「0 B」。
    # ``bytes_up``/``bytes_down`` 保留但恒为 0（DD_STORAGE.md §4.9），是
    # request_log 表本身的历史字段，仍然对外暴露供审计核对。
    traffic_bytes_up: int | None
    traffic_bytes_down: int | None


class LogPage(Model):
    """``has_more`` 而非 ``total``：见 queries.query_logs 的说明。"""

    items: list[LogItem]
    page: int
    page_size: int
    has_more: bool


class HostTrafficQuery(Model):
    """主机流量榜的筛选条件。``since``/``until`` 缺省时取「今日」。

    「今日」按**服务器进程的本地时区**计算零点（与 DD_DEPLOY.md 的容器时区
    前提一致：`/etc/localtime` 只读挂载，与宿主一致）——`created_at` 存的是
    UTC unix 秒，但「今天」是给人看的概念，必须按人所在的时区换算。
    """

    since: int | None = Field(None, ge=0)
    until: int | None = Field(None, ge=0)
    # 不复用 Pagination.page_size：这个接口不分页，只取「前 N 名」，没有
    # 「下一页」的概念。
    limit: int = Field(20, ge=1, le=200)

    def resolved_range(self, *, now: datetime | None = None) -> tuple[int, int]:
        moment = now if now is not None else datetime.now().astimezone()
        since = self.since
        if since is None:
            midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            since = int(midnight.timestamp())
        until = self.until if self.until is not None else int(moment.timestamp())
        return since, until


class HostTrafficItem(Model):
    host: str
    bytes_up: int
    bytes_down: int
    requests: int


class HostTrafficResponse(Model):
    items: list[HostTrafficItem]
    since: int
    until: int


class SwitchAttempt(Model):
    attempt_index: int
    upstream_name: str
    upstream_priority: int | None
    http_status: int | None
    error: str | None
    failure_kind: str | None
    elapsed_ms: int
    created_at: int


class SwitchItem(Model):
    """一次请求的完整尝试链，按发生顺序。前端据此渲染切换路径。"""

    request_id: str
    host: str
    url: str | None
    method: str
    attempts: list[SwitchAttempt]


class SwitchPage(Model):
    items: list[SwitchItem]
    page: int
    page_size: int
    has_more: bool


class UpstreamHealthInfo(Model):
    """出口的健康面。

    不含 ``avg_latency_ms``：内存里尚未统计延迟，回一个 0 会被看板显示成
    「平均延迟 0ms」，比缺这一项更容易误导。
    """

    circuit_state: str
    available: bool
    consecutive_failures: int
    total_success: int
    total_failure: int
    success_rate: float
    auth_error: bool
    last_error: str | None
    # 相对时长而非绝对时间：内存里的时间戳是 monotonic，绝对值对客户端无意义。
    last_success_age_seconds: float | None
    # 累计流量，纯展示字段，不参与任何路由判据。单位是字节，人类可读格式化
    # （KB/MB/GB）交给前端。
    bytes_up_total: int
    bytes_down_total: int


class UpstreamHealthItem(UpstreamHealthInfo):
    """看板健康表的一行：健康面 + 定位所需的配置面。

    继承而非内嵌，是为了保持 `/api/health` 的扁平结构——它是 3 秒轮询一次的
    端点，扁平结构让前端少一层解包。
    """

    name: str
    type: str
    address: str | None
    priority: int
    enabled: bool


class HealthResponse(Model):
    upstreams: list[UpstreamHealthItem]


class UpstreamItem(Model):
    """出口管理页的一行。健康面内嵌，对应 WEBUI_SPEC §3.2 的响应示例。

    ``has_auth`` 只表示是否配了认证：用户名与密码明文**永不**出现在响应里。
    """

    name: str
    type: str
    address: str | None
    priority: int
    enabled: bool
    has_auth: bool
    health: UpstreamHealthInfo


class UpstreamsResponse(Model):
    upstreams: list[UpstreamItem]


class ProbeResult(Model):
    """连通性测试结果。

    **不含响应体**：返回了就等于给出一个通用的内网探测器。``error`` 只有异常
    类型名或 errno 名，不含地址（与代理侧的失败归类同一口径）。
    """

    name: str
    ok: bool
    elapsed_ms: float
    target: str
    http_status: int | None
    error: str | None


class StickyQuery(Pagination):
    q: str | None = Field(None, max_length=255)
    upstream: str | None = Field(None, max_length=64)
    # 「按失败次数排序」用于找出问题绑定，是需求里明确要求的排序方式之一。
    sort: Literal["recent", "fails"] = "recent"


class StickyItem(Model):
    host: str
    upstream: str
    source: str
    fail_count: int
    hit_count: int
    last_used_age_seconds: float | None


class StickyPage(Model):
    """这里回 ``total`` 而日志页回 ``has_more``：粘性映射整份就在内存里，筛完的
    列表长度是白拿的；日志的总数要让 SQLite 扫全表，代价完全不同。"""

    items: list[StickyItem]
    page: int
    page_size: int
    total: int


class StickyBindRequest(Model):
    upstream: str = Field(min_length=1, max_length=64)


class StickyClearRequest(Model):
    """批量清除的目标。两者都不给即拒绝——空条件会清空全部映射。"""

    upstream: str | None = Field(None, max_length=64)
    hosts: list[str] | None = Field(None, max_length=1_000)


class ClearedResponse(Model):
    cleared: int


class RouteBlockItem(Model):
    host: str
    upstream: str
    fail_count: int
    reason: str
    expires_in_seconds: float


class RouteBlockPage(Model):
    items: list[RouteBlockItem]
    page: int
    page_size: int
    total: int


# --------------------------------------------------------------------------
# 配置写回（切片 d）
# --------------------------------------------------------------------------

# 出口名会成为 TOML 里的值、审计的 target、规则的 forward 目标。限成保守的字符
# 集，任何一处的转义疏漏都不至于变成注入。
UPSTREAM_NAME = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 版本号是 sha256 的前 16 位十六进制。
VERSION_FIELD = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")


class VersionedRequest(Model):
    """带 ``config_version`` 的写请求。

    并发写保护靠它：客户端必须回传读到的版本号，服务端在锁内重读磁盘比对
    （WEBUI_SPEC §6.4.1）。
    """

    config_version: str = VERSION_FIELD


class UpstreamCreateRequest(VersionedRequest):
    name: str = UPSTREAM_NAME
    type: Literal["http", "direct"]
    address: str | None = Field(None, max_length=255)
    priority: int = Field(100, ge=1, le=999)
    enabled: bool = True


class UpstreamUpdateRequest(VersionedRequest):
    """只允许改这三项（WEBUI_SPEC §2.2）。

    ``type`` 与 ``name`` 不可改：改名等于删一个建一个，而规则、粘性映射、健康
    状态全都以名字为键，静默改名会让它们集体失效。
    """

    address: str | None = Field(None, max_length=255)
    priority: int | None = Field(None, ge=1, le=999)
    enabled: bool | None = None


class PriorityGroupsRequest(VersionedRequest):
    """拖拽排序的结果：按新顺序排列的**优先级组**。

    传组而不是传每个出口的数值，是因为界面上拖动的单位就是组（同优先级的出口
    整体移动，避免拖散轮询组），而具体数值由服务端按组数选步长算出。
    """

    groups: list[list[str]] = Field(min_length=1, max_length=999)


class ConfigVersionResponse(Model):
    config_version: str


class RuleRow(Model):
    """一条规则。没有行 id——整表替换不需要标识单行，`position` 由数组下标决定。

    条件两端的空白在这里就去掉：尾随空格在界面上不可见，却会让
    `example.com ` 编译成一条永远不会命中的精确主机规则。
    """

    condition: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=1_000
    )
    upstream: str = UPSTREAM_NAME


# 规则条数上限。可增长资源必须有界，而线性桶（通配符 + 正则）的匹配成本随
# 条数线性增长——热路径上的开销必须有上界。
RULES_FIELD = Field(max_length=5_000)


class RulesResponse(Model):
    """`enabled` 反映 `config.toml` 的 `[rules] enabled`，供界面决定是否显示
    「规则当前未生效」横幅。数组顺序即匹配顺序（首匹配胜出）。"""

    revision: int
    enabled: bool
    rules: list[RuleRow]


class RulesSaveRequest(Model):
    """整表替换。`revision` 是乐观锁，不等于库内当前值即 `409`。"""

    revision: int = Field(ge=0)
    rules: list[RuleRow] = RULES_FIELD


class RulesValidateRequest(Model):
    rules: list[RuleRow] = RULES_FIELD


class IssueItem(Model):
    code: str
    location: str
    message: str
    level: str


class RulesSaveResponse(Model):
    """告警随 `200` 一并返回，不阻断保存。"""

    revision: int
    issues: list[IssueItem]


class RulesValidateResponse(Model):
    ok: bool
    issues: list[IssueItem]
    rule_count: int


class RouteTestRequest(Model):
    url: str = Field(min_length=1, max_length=2_048)


class MatchedRule(Model):
    position: int
    condition: str


class RouteTestResponse(Model):
    """路由决策测试。走与真实请求同一个 `Router.build_chain`。"""

    url: str
    host: str
    port: int
    decision: str
    upstream: str | None
    matched_rule: MatchedRule | None
    candidate_chain: list[str]


class StickyPromoteRequest(Model):
    """把一条粘性映射固化成规则。

    定义在这里而不是粘性那一段：它引用 `RuleRow` 的条件约束与 `MatchedRule`，
    两者都在规则段。条件与出口都由客户端给全，服务端不从粘性条目里取——用户在
    确认框里看到并可能改过的是这两个值，回头再去读内存状态可能已经不是那一个。
    """

    condition: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=1_000
    )
    upstream: str = UPSTREAM_NAME


class StickyPromoteResponse(Model):
    """`position` 恒为 0（插表首），显式回出来是为了让界面能说清「插到了哪」。

    `previous_match` 是固化**之前**该 host 命中的规则：非空说明表里已经有一条更
    宽的规则管着它，新规则从此优先于它。界面据此提示，避免用户在规则页看到两条
    都能匹配的规则时以为出了错。
    """

    position: int
    revision: int
    rules_enabled: bool
    sticky_cleared: bool
    previous_match: MatchedRule | None
    issues: list[IssueItem]


class TimeoutSettings(Model):
    connect_timeout: float
    read_timeout: float


class RateLimitSettings(Model):
    max_switches_per_host: int
    window_seconds: int


class CircuitBreakerSettings(Model):
    enabled: bool
    fail_threshold: int
    cooldown_seconds: int


class RoutingSettings(Model):
    connect_timeout: float
    read_timeout: float
    switch_on_status: list[int]
    sticky_fail_threshold: int
    route_block_ttl: int
    tunnel_probe_window: float
    switch_buffer_bytes: int
    happy_eyeballs_delay: float
    status_switch_rate_limit: RateLimitSettings
    circuit_breaker: CircuitBreakerSettings


class StorageSettings(Model):
    retention_days: int
    max_log_rows: int
    backup_keep: int


class ListenSettings(Model):
    """只读展示：改这些要重启（WEBUI_SPEC §2.5、§6.3）。"""

    listen_host: str
    listen_port: int
    webui_host: str
    webui_port: int


class SettingsResponse(Model):
    """**不含 `auth_token`**：任何接口都不返回它（DD_WEB §7.4）。"""

    config_version: str
    routing: RoutingSettings
    storage: StorageSettings
    listen: ListenSettings
    restart_required_fields: list[str]


class SettingsUpdateRequest(VersionedRequest):
    """可改设置的**白名单**。

    字段逐个列出而不是接受点分键的字典：让客户端指定键名等于允许它写
    `webui.auth_token`，或者写进任何一个加载器不认识的键——后者会让下一次启动
    直接失败。
    """

    connect_timeout: float | None = Field(None, gt=0, le=300)
    read_timeout: float | None = Field(None, gt=0, le=3_600)
    # 逐项限 100–599。核心校验只拒 2xx/3xx，写进去的 700 不会命中任何响应，
    # 但会让用户以为设置生效了。
    switch_on_status: list[StatusCode] | None = Field(None, max_length=64)
    sticky_fail_threshold: int | None = Field(None, ge=1, le=100)
    route_block_ttl: int | None = Field(None, ge=1, le=86_400)
    tunnel_probe_window: float | None = Field(None, gt=0, le=300)
    max_switches_per_host: int | None = Field(None, ge=1, le=1_000)
    window_seconds: int | None = Field(None, ge=1, le=3_600)
    circuit_breaker_enabled: bool | None = None
    circuit_breaker_fail_threshold: int | None = Field(None, ge=1, le=1_000)
    circuit_breaker_cooldown_seconds: int | None = Field(None, ge=1, le=86_400)
    retention_days: int | None = Field(None, ge=1, le=3_650)
    max_log_rows: int | None = Field(None, ge=1_000, le=100_000_000)
    backup_keep: int | None = Field(None, ge=1, le=100)

    def changes(self) -> dict[str, object]:
        """字段名 → `config.toml` 里的点分键。只包含本次显式提交的字段。

        ``exclude_unset`` 是关键：``None`` 既可能是「没提交」也可能是合法值，
        用它区分才不会把没动过的项写成默认值。
        """
        submitted = self.model_dump(exclude_unset=True)
        return {
            _SETTING_KEYS[name]: getattr(self, name) for name in submitted if name in _SETTING_KEYS
        }


_SETTING_KEYS = {
    "connect_timeout": "routing.connect_timeout",
    "read_timeout": "routing.read_timeout",
    "switch_on_status": "routing.switch_on_status",
    "sticky_fail_threshold": "routing.sticky_fail_threshold",
    "route_block_ttl": "routing.route_block_ttl",
    "tunnel_probe_window": "routing.tunnel_probe_window",
    "max_switches_per_host": "routing.status_switch_rate_limit.max_switches_per_host",
    "window_seconds": "routing.status_switch_rate_limit.window_seconds",
    "circuit_breaker_enabled": "routing.circuit_breaker.enabled",
    "circuit_breaker_fail_threshold": "routing.circuit_breaker.fail_threshold",
    "circuit_breaker_cooldown_seconds": "routing.circuit_breaker.cooldown_seconds",
    "retention_days": "database.retention_days",
    "max_log_rows": "database.max_log_rows",
    "backup_keep": "database.backup_keep",
}


class BackupItem(Model):
    filename: str
    size: int
    created_at: float


class BackupsResponse(Model):
    backups: list[BackupItem]


class RestoreRequest(VersionedRequest):
    filename: str = Field(min_length=1, max_length=255)


class AuditQuery(Pagination):
    action: str | None = Field(None, max_length=64)
    since: int | None = Field(None, ge=0)
    until: int | None = Field(None, ge=0)


class AuditItem(Model):
    id: int
    actor: str
    action: str
    target: str
    diff: str | None
    version_before: str | None
    version_after: str | None
    created_at: int


class AuditPage(Model):
    items: list[AuditItem]
    page: int
    page_size: int
    has_more: bool
