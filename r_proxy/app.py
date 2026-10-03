"""Application：生命周期编排（启动 / 关闭 / 重载）。

对应设计：docs/design/ARCH_OVERVIEW.md §7、§8。

装配「配置 + 运行时状态 + 代理服务」；存储与 Web 界面在后续里程碑接入。
"""

from __future__ import annotations

import asyncio
import logging
import resource
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from r_proxy.config.loader import ConfigError, load
from r_proxy.config.model import ConfigSnapshot
from r_proxy.config.validate import ValidationIssue, validate
from r_proxy.egress.capability import probe_ipv6_egress
from r_proxy.persistence import HealthPersister, apply_initial_state
from r_proxy.protocol.server import DEFAULT_DRAIN_TIMEOUT, ProxyServer
from r_proxy.rules.loader import LoadResult, load_rules
from r_proxy.rules.model import EMPTY_RULE_SET, RuleSet
from r_proxy.state.runtime import RuntimeState
from r_proxy.storage.expiry import StickyExpiryPolicy
from r_proxy.storage.metrics import MetricsReporter
from r_proxy.storage.rules_store import RulesStore
from r_proxy.storage.schema import StorageError
from r_proxy.storage.service import StorageService

if TYPE_CHECKING:
    from r_proxy.web import WebRunner

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """配置无法使用，拒绝启动。"""


class Application:
    def __init__(
        self,
        *,
        config_path: Path,
        overrides: Mapping[str, object] | None = None,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        self._config_path = config_path
        self._overrides = dict(overrides or {})
        self._drain_timeout = drain_timeout
        self._snapshot: ConfigSnapshot | None = None
        self._rules: RuleSet = EMPTY_RULE_SET
        self._rules_store: RulesStore | None = None
        self._server: ProxyServer | None = None
        self._storage: StorageService | None = None
        self._background: list[asyncio.Task[None]] = []
        self._web: WebRunner | None = None
        self._started_at = 0.0
        self._started = asyncio.Event()
        self._stopping = asyncio.Event()

    @property
    def snapshot(self) -> ConfigSnapshot:
        if self._snapshot is None:
            raise RuntimeError("配置尚未加载")
        return self._snapshot

    @property
    def proxy_address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("代理服务尚未启动")
        return self._server.bound_address

    @property
    def state(self) -> RuntimeState:
        """运行时状态（熔断、负面记忆、游标）。Web 界面与测试的只读入口。"""
        if self._server is None:
            raise RuntimeError("代理服务尚未启动")
        return self._server.state

    @property
    def rules(self) -> RuleSet:
        return self._rules

    @property
    def rules_store(self) -> RulesStore:
        """``rules.db`` 的读写门面。Web 的规则页经它读取与整表替换。"""
        if self._rules_store is None:
            raise RuntimeError("规则库尚未装配")
        return self._rules_store

    @property
    def storage(self) -> StorageService:
        """存储子系统。Web 界面的只读查询与指标从这里取。"""
        if self._storage is None:
            raise RuntimeError("存储子系统尚未启动")
        return self._storage

    @property
    def web(self) -> WebRunner | None:
        """已启动的 Web 界面；未启用或依赖缺失时为 ``None``。"""
        return self._web

    @property
    def uptime_seconds(self) -> float:
        return 0.0 if self._started_at == 0.0 else time.monotonic() - self._started_at

    @property
    def active_connections(self) -> int:
        return 0 if self._server is None else self._server.active_connections

    @property
    def rejected_connections(self) -> int:
        return 0 if self._server is None else self._server.rejected_connections

    def check(self) -> list[ValidationIssue]:
        """只加载并校验配置，不绑定任何端口。供 ``--check`` 使用。"""
        snapshot = self._load_snapshot()
        loaded = self._load_rules(snapshot)
        return loaded.issues + validate(
            snapshot,
            has_ipv6_egress=probe_ipv6_egress(),
            nofile_limit=_nofile_limit(),
            rule_targets=loaded.rule_set.targets(),
        )

    async def start(self) -> None:
        snapshot = self._load_snapshot()
        has_ipv6 = probe_ipv6_egress()
        loaded = self._load_rules(snapshot)
        _raise_on_errors(
            _report(
                loaded.issues
                + validate(
                    snapshot,
                    has_ipv6_egress=has_ipv6,
                    nofile_limit=_nofile_limit(),
                    rule_targets=loaded.rule_set.targets(),
                )
            )
        )
        self._snapshot = snapshot
        self._rules = loaded.rule_set
        self._ensure_rules_db(snapshot)

        # 库打不开就拒绝启动：粘性与负面记忆丢了还能重学，但一个连不上磁盘的
        # 代理会静默地把每次学到的东西都扔掉，用户无从察觉。
        storage = StorageService(
            snapshot.database,
            snapshot.limits,
            sticky_policy=StickyExpiryPolicy(ttl_seconds=float(snapshot.routing.sticky_ttl)),
        )
        self._storage = storage
        initial = storage.load_initial_state()

        try:
            try:
                storage.start()
            except (StorageError, TimeoutError) as exc:
                raise StartupError(str(exc)) from exc

            self._server = ProxyServer(snapshot, rule_set=loaded.rule_set, sink=storage.queue)
            apply_initial_state(self._server.state, initial)
            # 决策层禁止 I/O，探测结果只能从这里注入。
            self._server.state.set_ipv6_egress(has_ipv6)
            await self._server.start()
            self._background = [
                asyncio.create_task(
                    HealthPersister(storage.queue, initial=initial).run(self._server.state)
                ),
                asyncio.create_task(MetricsReporter(storage.metrics).run()),
            ]
            self._started_at = time.monotonic()
            await self._maybe_start_web()
        except BaseException:
            # 启动到一半失败必须把已经拉起来的东西收回去。写者线程不是 daemon，
            # 漏掉关库这一步进程就永远退不出去——「端口被占用之后连 Ctrl-C
            # 都没反应」正是这么来的。
            await self.stop()
            raise
        self._started.set()

    async def _maybe_start_web(self) -> None:
        """启用时才导入 Web 包。

        ``from r_proxy import web`` 是全代码库中唯一导入 Web 包的位置：只有
        这样 ``--no-web`` 形态才真的不碰 ``fastapi``。依赖缺失时给可操作的提示，
        而不是让 ``ModuleNotFoundError`` 把代理一起带走。
        """
        if not self.snapshot.webui.enabled:
            return
        try:
            # 守卫必须一直罩到 start()：`r_proxy.web` 的模块体只用标准库，
            # fastapi 与 uvicorn 是在 start() 里才导入的，只包住这一行 import
            # 等于没包——缺依赖时异常会从 start() 里冒出来。
            from r_proxy import web

            runner = await web.start(self)
        except ImportError as exc:
            logger.warning(
                'Web 界面已启用但依赖缺失（%s）。执行 pip install "r-proxy[web]" 安装，'
                "或用 --no-web 关闭此提示。",
                exc.name,
            )
            return
        self._web = runner
        cfg = self.snapshot.webui
        logger.info("Web 管理界面监听于 %s:%s", cfg.host, cfg.port)

    async def stop(self) -> None:
        self._stopping.set()
        # 每一环独立捕获异常。任何一环抛出都不能跳过后面的清理：写者线程不是
        # daemon，关库这一步被跳过就意味着进程永远退不出去。
        for shutdown in (self._stop_web, self._stop_background, self._stop_proxy):
            try:
                await shutdown()
            except Exception:
                logger.exception("关停某一环节失败，继续执行后续清理")
        if self._storage is not None:
            # 必须在服务停完之后：排空队列的前提是不再有新的写入进来。
            # 线程 join 会阻塞事件循环，但此时已经没有连接在等它。
            self._storage.stop()
            self._storage = None
        self._started_at = 0.0
        self._started.clear()

    async def _stop_web(self) -> None:
        """Web 先停：它会读内存状态并往写入队列投递。

        停在代理之后就可能在队列已经排空之后又塞进新的写入。
        """
        if self._web is not None:
            web, self._web = self._web, None
            await web.stop()

    async def _stop_background(self) -> None:
        if not self._background:
            return
        tasks, self._background = self._background, []
        for task in tasks:
            task.cancel()
        # 后台任务只在 sleep 或入队之间被取消，没有需要等待的清理动作。
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_proxy(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop(drain_timeout=self._drain_timeout)

    async def run(self) -> None:
        """启动并阻塞直到收到停止请求。"""
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.stop()

    async def wait_started(self) -> None:
        await self._started.wait()

    def request_stop(self) -> None:
        self._stopping.set()

    async def reload(self) -> None:
        """重新加载配置。

        失败时保留正在生效的快照：一次手滑的编辑不应该让代理停摆。新快照
        通过单次引用赋值替换，处理中的请求继续用它启动时取到的那一份。
        """
        snapshot = self._load_snapshot()
        # 重探：网络环境可能在两次加载之间变化（拨号、切 Wi-Fi、容器换网）。
        has_ipv6 = probe_ipv6_egress()
        loaded = self._load_rules(snapshot)
        # 规则有条件错误时整份新规则集都不采纳：部分采纳会让路由处于
        # 「一半新一半旧」的状态，比完全不生效更难排查。抛出后旧的
        # ``self._rules`` 原样保留，代理继续按已知可用的规则工作。
        _raise_on_errors(
            _report(
                loaded.issues
                + validate(
                    snapshot,
                    has_ipv6_egress=has_ipv6,
                    nofile_limit=_nofile_limit(),
                    rule_targets=loaded.rule_set.targets(),
                )
            )
        )
        # 版本要在替换之前取：绝大多数重载来自 Web 界面改规则，`config.toml`
        # 原封不动。一律说「配置已重载」会让运维照着一个没变过的文件排查。
        current = self._snapshot
        changed = current is None or current.config_version != snapshot.config_version
        self._snapshot = snapshot
        self._rules = loaded.rule_set
        self._ensure_rules_db(snapshot)
        if self._server is not None:
            self._server.update_snapshot(snapshot, loaded.rule_set)
            self._server.state.set_ipv6_egress(has_ipv6)
        if changed:
            logger.info(
                "配置已重载，版本 %s，规则 %d 条",
                snapshot.config_version,
                len(loaded.rule_set.rules),
            )
        else:
            logger.info(
                "规则已重载，%d 条；配置未变（版本 %s）",
                len(loaded.rule_set.rules),
                snapshot.config_version,
            )

    def _load_snapshot(self) -> ConfigSnapshot:
        try:
            return load(self._config_path, self._overrides)
        except ConfigError as exc:
            raise StartupError(str(exc)) from exc

    def _load_rules(self, snapshot: ConfigSnapshot) -> LoadResult:
        """按新快照的路径读规则库并编译。

        每次都新建 ``RulesStore``：``database.rules_path`` 可能在重载时变了，
        沿用旧实例会读到旧库。库读不出来时拒绝采纳（转成 ``StartupError``），
        与「条件非法」同一处理——两者都意味着规则集不可信。
        """
        store = RulesStore(snapshot.database.rules_path)
        try:
            loaded = load_rules(store, enabled=snapshot.rules_enabled)
        except StorageError as exc:
            raise StartupError(str(exc)) from exc
        self._rules_store = store
        return loaded

    def _ensure_rules_db(self, snapshot: ConfigSnapshot) -> None:
        """建库、建表、写入 ``revision = 0``。

        ``rules.enabled = false`` 时**不建库**：救场开关的语义是「完全不碰规则
        库」，在库可能已损坏的场景下这一点是关键。
        """
        if not snapshot.rules_enabled or self._rules_store is None:
            return
        try:
            self._rules_store.ensure_schema()
        except StorageError as exc:
            raise StartupError(str(exc)) from exc


def _report(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    for issue in issues:
        where = f"[{issue.location}] " if issue.location else ""
        line = f"{issue.code}: {where}{issue.message}"
        if issue.level == "error":
            logger.error("%s", line)
        else:
            logger.warning("%s", line)
    return issues


def _raise_on_errors(issues: list[ValidationIssue]) -> None:
    errors = [i for i in issues if i.level == "error"]
    if errors:
        detail = "\n".join(f"  {i.code}: {i.message}" for i in errors)
        raise StartupError(f"配置校验未通过：\n{detail}")


def _nofile_limit() -> int | None:
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    return None if soft < 0 else soft
