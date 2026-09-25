"""写入事件与有界队列。

对应设计：docs/design/DD_STORAGE.md §4.2、§4.6。

优先级与目标库**不由调用方传入**，而是由操作种类唯一决定：传错优先级会让
粘性变更被当成日志丢掉，而这种错误不会有任何直接症状——直到重启后发现
路由记忆没了。
"""

from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Protocol

from r_proxy.storage.schema import Database

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    CRITICAL = 0  # state.db 的绑定关系变更，仅在硬上限处丢弃
    NORMAL = 1  # 计数器更新，可合并
    LOSSY = 2  # request_log，队列满即丢弃


class OpKind(StrEnum):
    """一个种类对应一条 SQL。写者按种类分组后 ``executemany``。"""

    STICKY_UPSERT = "sticky_upsert"
    STICKY_MANUAL_UPSERT = "sticky_manual_upsert"
    STICKY_HIT = "sticky_hit"
    STICKY_DELETE = "sticky_delete"
    ROUTE_BLOCK_UPSERT = "route_block_upsert"
    ROUTE_BLOCK_DELETE = "route_block_delete"
    HEALTH_COUNTERS = "health_counters"
    REQUEST_LOG = "request_log"
    TRAFFIC_LOG = "traffic_log"
    CONFIG_AUDIT = "config_audit"


@dataclass(frozen=True, slots=True)
class _Spec:
    """一个写入种类的全部元数据。

    集中成一张表而非四个平行字典：这些字段必须彼此一致（``key_slots`` 指向
    的列要真的是主键、``delta_slots`` 指向的列在 SQL 里要真的是自增），
    并排放着才能一眼核对。
    """

    priority: Priority
    database: Database
    table: str
    # 主键在 payload 中的位置。空元组表示不可合并（INSERT 类，每条都要保留）。
    key_slots: tuple[int, ...] = ()
    # 增量列的位置。合并时相加，其余列取后写的那一份。混淆「增量」与「权威
    # 值」是本模块最容易出的错：把覆盖写成自增，计数会翻倍；把自增写成覆盖，
    # 同批次的多次变更会互相丢失。
    delta_slots: tuple[int, ...] = ()
    deletes_row: bool = False


_SPECS: dict[OpKind, _Spec] = {
    OpKind.STICKY_UPSERT: _Spec(Priority.CRITICAL, Database.STATE, "host_upstream", key_slots=(0,)),
    OpKind.STICKY_MANUAL_UPSERT: _Spec(
        Priority.CRITICAL, Database.STATE, "host_upstream", key_slots=(0,)
    ),
    OpKind.STICKY_HIT: _Spec(
        Priority.NORMAL, Database.STATE, "host_upstream", key_slots=(3,), delta_slots=(0,)
    ),
    OpKind.STICKY_DELETE: _Spec(
        Priority.CRITICAL, Database.STATE, "host_upstream", key_slots=(0,), deletes_row=True
    ),
    OpKind.ROUTE_BLOCK_UPSERT: _Spec(
        Priority.CRITICAL, Database.STATE, "route_block", key_slots=(0, 1)
    ),
    OpKind.ROUTE_BLOCK_DELETE: _Spec(
        Priority.CRITICAL, Database.STATE, "route_block", key_slots=(0, 1), deletes_row=True
    ),
    OpKind.HEALTH_COUNTERS: _Spec(
        Priority.NORMAL,
        Database.STATE,
        "upstream_health",
        key_slots=(0,),
        delta_slots=(1, 2, 9, 10),
    ),
    OpKind.REQUEST_LOG: _Spec(Priority.LOSSY, Database.LOGS, "request_log"),
    OpKind.TRAFFIC_LOG: _Spec(Priority.LOSSY, Database.LOGS, "traffic_log"),
    OpKind.CONFIG_AUDIT: _Spec(Priority.CRITICAL, Database.LOGS, "config_audit"),
}


@dataclass(frozen=True, slots=True)
class WriteOp:
    kind: OpKind
    payload: tuple[object, ...]

    @property
    def spec(self) -> _Spec:
        return _SPECS[self.kind]

    @property
    def priority(self) -> Priority:
        return self.spec.priority

    @property
    def database(self) -> Database:
        return self.spec.database

    @property
    def row_key(self) -> tuple[str, tuple[object, ...]] | None:
        """标识被写的那一行 ``(表, 主键)``；``None`` 表示不可合并。

        按表而非按种类归组：同一行上的 ``delete`` 必须能抹掉之前累积的
        ``upsert`` 与自增，跨种类的先后关系正是这里要保住的东西。
        """
        slots = self.spec.key_slots
        if not slots:
            return None
        return (self.spec.table, tuple(self.payload[i] for i in slots))

    @property
    def merge_key(self) -> tuple[object, ...] | None:
        slots = self.spec.key_slots
        return tuple(self.payload[i] for i in slots) if slots else None


# --------------------------------------------------------------------------
# 构造器：payload 的列顺序必须与 writer 的 SQL 参数顺序一致，因此集中在这里
# 定义，绝不让调用方自己拼元组。
# --------------------------------------------------------------------------


def sticky_upsert(
    *, host: str, upstream: str, url: str | None, now_unix: int, status: int | None
) -> WriteOp:
    """绑定关系变化才发 UPSERT。纯计数变化走 :func:`sticky_hit`。"""
    return WriteOp(OpKind.STICKY_UPSERT, (host, upstream, url, now_unix, status, now_unix))


def sticky_manual_upsert(*, host: str, upstream: str, now_unix: int) -> WriteOp:
    """管理员手动绑定。与 :func:`sticky_upsert` 的差别全在 SQL 上：

    - 写入 ``source = 'manual'``，而自动路径写死 ``'auto'``
    - **没有** ``WHERE source != 'manual'`` 护栏——那道护栏防的是自动逻辑覆盖
      手动意图，而这里就是手动意图本身，带上它会让改绑对已有的 manual 行静默
      失效（内存改了、库没改，重启后变回旧绑定）
    - 不动 ``hit_count`` / ``last_success_at``：改绑不代表发生过一次成功
    """
    return WriteOp(OpKind.STICKY_MANUAL_UPSERT, (host, upstream, now_unix))


def sticky_hit(*, host: str, now_unix: int, delta: int = 1) -> WriteOp:
    """命中计数自增。SQL 侧 ``hit_count = hit_count + ?``，Python 侧从不读当前值。"""
    return WriteOp(OpKind.STICKY_HIT, (delta, now_unix, now_unix, host))


def sticky_delete(*, host: str) -> WriteOp:
    return WriteOp(OpKind.STICKY_DELETE, (host,))


def route_block_upsert(
    *, host: str, upstream: str, reason: str, now_unix: int, blocked_until: int
) -> WriteOp:
    return WriteOp(OpKind.ROUTE_BLOCK_UPSERT, (host, upstream, reason, now_unix, blocked_until))


def route_block_delete(*, host: str, upstream: str) -> WriteOp:
    return WriteOp(OpKind.ROUTE_BLOCK_DELETE, (host, upstream))


def health_counters(
    *,
    upstream: str,
    success_delta: int,
    failure_delta: int,
    consecutive_failures: int,
    avg_latency_ms: int,
    circuit_state: str,
    cooldown_until: int,
    auth_error: int,
    now_unix: int,
    bytes_up_delta: int = 0,
    bytes_down_delta: int = 0,
) -> WriteOp:
    """``*_delta`` 是本批次增量，其余字段是内存中的权威值（覆盖写）。"""
    return WriteOp(
        OpKind.HEALTH_COUNTERS,
        (
            upstream,
            success_delta,
            failure_delta,
            consecutive_failures,
            avg_latency_ms,
            circuit_state,
            cooldown_until,
            auth_error,
            now_unix,
            bytes_up_delta,
            bytes_down_delta,
        ),
    )


def request_log(
    *,
    request_id: str,
    client_addr: str | None,
    host: str,
    url: str | None,
    method: str,
    upstream: str,
    upstream_priority: int | None,
    attempt_index: int,
    decision_source: str | None,
    rule_origin: str | None,
    http_status: int | None,
    error: str | None,
    failure_kind: str | None,
    keep_reason: str | None,
    elapsed_ms: int,
    bytes_up: int,
    bytes_down: int,
    now_unix: int,
) -> WriteOp:
    """一次出口尝试的记录。同一 ``request_id`` 的多行按 ``attempt_index`` 构成切换链。

    优先级是 ``LOSSY``：诊断价值高，但丢了不改变任何行为。粘性与熔断状态丢了
    会让路由走错，日志丢了只是看不到——队列告急时先牺牲这一类是对的。

    ``error`` 直接取自 ``AttemptOutcome.error``，只含异常类型名或 errno 名，
    不含出口地址。

    ``client_addr`` 只存 IP（不含端口，见 DD_STORAGE.md §3.1），采集自
    ``ProxyServer._on_client`` 的 ``peername``；解析不到时为 ``None``。它的
    准确性依赖容器网络模式（`host` 网络下是真实客户端 IP，`bridge` + 端口
    映射下是网桥地址），见 DD_PROXY.md §4.4。
    """
    return WriteOp(
        OpKind.REQUEST_LOG,
        (
            request_id,
            client_addr,
            host,
            url,
            method,
            upstream,
            upstream_priority,
            attempt_index,
            decision_source,
            rule_origin,
            http_status,
            error,
            failure_kind,
            keep_reason,
            elapsed_ms,
            bytes_up,
            bytes_down,
            now_unix,
        ),
    )


def traffic_log(
    *,
    request_id: str,
    host: str,
    upstream: str,
    bytes_up: int,
    bytes_down: int,
    now_unix: int,
) -> WriteOp:
    """一个成功交付的请求关闭/结束时落一行（DD_STORAGE.md §4.3b）。

    与 :func:`request_log` 分表：那张表是「每次尝试一行」的审计粒度，字节数
    恒为 0；这里是「传输结束后才知道数字」的独立事实，只在最终交付的那次
    尝试完整结束时写入一次。优先级同为 ``LOSSY``——丢了不改变任何路由行为，
    只是流量榜少一行。
    """
    return WriteOp(
        OpKind.TRAFFIC_LOG,
        (request_id, host, upstream, bytes_up, bytes_down, now_unix),
    )


def config_audit(
    *,
    actor: str,
    action: str,
    target: str,
    diff: str | None,
    version_before: str | None,
    version_after: str | None,
    now_unix: int,
) -> WriteOp:
    """配置与运维操作的审计记录。

    优先级是 ``CRITICAL``：审计的价值全在「事后能追溯」，队列繁忙时丢掉它，
    等于恰好在系统出问题的时候失去记录。
    """
    return WriteOp(
        OpKind.CONFIG_AUDIT,
        (actor, action, target, diff, version_before, version_after, now_unix),
    )


class WriteSink(Protocol):
    """写入事件的接收端。

    以协议而非具体类型注入：``egress`` 只需要「能收下一个操作」这一点能力，
    不该被绑到线程与数据库上——测试里的假 sink 也因此不必继承任何东西。
    """

    def put(self, op: WriteOp) -> bool: ...


# 唤醒阻塞在 drain 上的写者。是一个真实的 WriteOp 实例只为满足队列的类型，
# 判定一律按身份（``is``）而非内容，它永远不会被执行。
_INTERRUPT = WriteOp(OpKind.STICKY_DELETE, ())

# 严重积压时同一秒内的丢弃只报一次摘要：极端 QPS 下逐条打印会让日志本身
# 变成新的 CPU 负担，且刷屏没有任何增量信息（DD_WEB §5.2.1 同一套判据）。
_SEVERE_LOG_INTERVAL = 1.0


class WriteQueue:
    """无界底层容器 + 自己维护的水位。

    用 ``SimpleQueue`` 而非 ``queue.Queue``：后者的 ``put`` 在满时阻塞，而这里
    绝不能阻塞——``put`` 在事件循环中调用。容量控制表现为丢弃并计数。
    """

    __slots__ = (
        "_drained",
        "_enqueued",
        "_maxsize",
        "_queue",
        "_severe_log_at",
        "_severe_since_log",
        "dropped",
        "high_water",
    )

    def __init__(self, maxsize: int) -> None:
        self._queue: queue.SimpleQueue[WriteOp] = queue.SimpleQueue()
        self._maxsize = maxsize
        # 两个单调计数器而非一个 size：生产者只增 _enqueued，写者只增
        # _drained，每个变量都只有一个写线程，因此无需锁也不会丢更新。
        # size 读到的可能略微陈旧，对水位判断无影响。
        self._enqueued = 0
        self._drained = 0
        self.dropped: dict[Priority, int] = dict.fromkeys(Priority, 0)
        # 历史最高水位。周期采样看不到峰值：积压是突发的，两次采样之间涨上去
        # 又落回来的那一次，恰恰是最需要知道的那一次。
        self.high_water = 0
        self._severe_log_at = 0.0
        self._severe_since_log = 0

    @property
    def size(self) -> int:
        return self._enqueued - self._drained

    @property
    def accepted(self) -> int:
        """累计被接受的操作数。写者据此判断「入队的都已落盘」。"""
        return self._enqueued

    @property
    def dropped_lossy(self) -> int:
        return self.dropped[Priority.LOSSY]

    @property
    def dropped_normal(self) -> int:
        return self.dropped[Priority.NORMAL]

    @property
    def dropped_critical(self) -> int:
        """**非零即为严重问题**：粘性或熔断状态没能落盘，重启后会丢失。"""
        return self.dropped[Priority.CRITICAL]

    def put(self, op: WriteOp) -> bool:
        """在事件循环中调用，绝不阻塞。返回 ``False`` 表示被丢弃。"""
        size = self.size
        if size >= self._maxsize * 2:
            self.dropped[op.priority] += 1
            self._severe_since_log += 1
            now = time.monotonic()
            if now - self._severe_log_at >= _SEVERE_LOG_INTERVAL:
                logger.error(
                    "写入队列严重积压（%d 条），本窗口已丢弃 %d 条（含本次 %s）",
                    size,
                    self._severe_since_log,
                    op.kind,
                )
                self._severe_log_at = now
                self._severe_since_log = 0
            return False
        if size >= self._maxsize and op.priority is Priority.LOSSY:
            self.dropped[Priority.LOSSY] += 1
            return False
        self._queue.put(op)
        self._enqueued += 1
        if (level := self.size) > self.high_water:
            self.high_water = level
        return True

    def drain(self, *, max_items: int, timeout: float) -> list[WriteOp]:
        """在写者线程中调用。攒够 ``max_items`` 或等满 ``timeout`` 即返回。

        攒满时间窗才落盘是有意的：批量后单行成本从 0.11–0.47ms 降到
        0.0025ms。写入本身是异步的，多等 200ms 不影响任何请求的延迟。
        """
        batch: list[WriteOp] = []
        deadline = time.monotonic() + timeout
        while len(batch) < max_items:
            remaining = deadline - time.monotonic()
            try:
                # 时间窗用尽后仍取一次：``timeout=0`` 应当表示「取走现有的」，
                # 而不是「什么都不取」。
                op = (
                    self._queue.get(timeout=remaining)
                    if remaining > 0
                    else self._queue.get_nowait()
                )
            except queue.Empty:
                break
            if op is _INTERRUPT:
                break
            batch.append(op)
        self._drained += len(batch)
        return batch

    def drain_all(self) -> list[WriteOp]:
        """取走当前全部积压，不等待。进程关闭时排空队列用。"""
        batch: list[WriteOp] = []
        while True:
            try:
                op = self._queue.get_nowait()
            except queue.Empty:
                break
            if op is not _INTERRUPT:
                batch.append(op)
        self._drained += len(batch)
        return batch

    def interrupt(self) -> None:
        """立刻唤醒阻塞在 :meth:`drain` 上的写者。

        关停时必须有这一步：``flush_interval_ms`` 可以配到几分钟，靠等时间窗
        自然到期会让进程退出被拖住同样久（写者线程不是 daemon）。
        """
        self._queue.put(_INTERRUPT)
