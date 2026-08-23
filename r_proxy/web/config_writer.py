"""配置与规则的写回。

对应设计：docs/design/DD_WEB.md §6，[DD_CONFIG §5.2](../../docs/design/DD_CONFIG.md)、§5.3。
需求：WEBUI_SPEC.md §6.2、§6.4、§6.5。

两条写入路径共用同一把锁：``config.toml`` 走文件的原子替换，``rules.db`` 走单个
``BEGIN IMMEDIATE`` 事务的整表替换。共用锁是必要的——规则校验要读当前配置里的
出口清单，配置校验要读当前规则里的出口引用，两边同时改会让任一侧基于过期数据
做判断。

顺序不可调换：**先校验版本，再校验内容，最后才碰文件（或库）**。任一步失败时
磁盘文件与库保持原样，也不产生备份。

```
acquire lock
  → 重读磁盘算哈希（不比内存缓存的版本号）
  → 在这份刚读到的文本上做变换
  → 解析 + 校验候选（失败即返回，文件未动）
  → 备份 → 轮转 → 临时文件 → fsync → rename → fsync 父目录
  → 热重载（走标准 tomllib 读路径，与用户手写配置同一条流程）
  → 审计入队（diff 已脱敏）
release lock
```

**基线文本必须在锁内读**，而不是由调用方先读好再传进来：调用方读到的可能是上
一个版本，变换会以它为基准，把别人刚写进去的改动一起抹掉。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from r_proxy.config.loader import ConfigError, load_text
from r_proxy.config.model import ConfigSnapshot
from r_proxy.config.validate import ValidationIssue, validate
from r_proxy.egress.capability import probe_ipv6_egress
from r_proxy.rules.loader import compile_rules
from r_proxy.storage.queue import config_audit
from r_proxy.storage.rules_store import RuleRow, RulesConflict, RulesSnapshot
from r_proxy.storage.schema import StorageError
from r_proxy.web.toml_edit import Transform

if TYPE_CHECKING:
    from r_proxy.app import Application

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
BACKUP_STAMP = "%Y%m%d-%H%M%S"
CONFIG_STEM = "config"
RULES_STEM = "rules"

# 单条插入落在哪个位置。首匹配胜出下只有表首能保证生效（§6.3.3）。
PROMOTED_POSITION = 0
# 备份文件名的形状由服务端决定；恢复接口只接受能在列表里找到的名字（§6.5）。
BACKUP_PATTERN = re.compile(r"^[A-Za-z0-9._-]+-\d{8}-\d{6}(-\d+)?(\.toml)?$")

# 审计 diff 一次最多留多少行。审计表与请求日志同库、共用保留策略，一次粘贴
# 上千行的规则文件会把它撑大，而追溯「改了哪一块」并不需要全文。
MAX_DIFF_LINES = 200

# 审计 diff 里的敏感值。入库前脱敏，不是查询时——查询时脱敏意味着明文已经落盘。
_SECRET_LINE = re.compile(
    r"^(\s*[-+]?\s*(?:password|auth_token|token|secret|username)\s*=\s*).+$",
    re.IGNORECASE | re.MULTILINE,
)


class ConfigConflict(Exception):
    """磁盘上的内容已经不是客户端读到的那一份。"""

    def __init__(self, *, expected: str, actual: str) -> None:
        super().__init__("配置已被其他会话修改")
        self.expected = expected
        self.actual = actual


class ConfigInvalid(Exception):
    """候选内容校验未通过。磁盘文件未改动。"""

    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues = list(issues)
        super().__init__("；".join(f"{i.code}: {i.message}" for i in self.issues))


class RuleExists(Exception):
    """要插入的条件在表里已经有了。库未改动。

    整表替换路径允许重复（只回一条 `W_RULE_DUPLICATE` 告警），插入路径**拒绝**：
    编辑表格时用户看得见那两行并能自己取舍，而从粘性页一键固化看不见规则表，
    重复点两次只会在表首堆出一条让原规则永不生效的死行。
    """

    def __init__(self, *, position: int, condition: str, upstream: str) -> None:
        super().__init__(f"规则已存在：rules[{position}]")
        self.position = position
        self.condition = condition
        self.upstream = upstream


@dataclass(frozen=True, slots=True)
class BackupInfo:
    filename: str
    size: int
    created_at: float


def mask_secrets(text: str | None) -> str | None:
    """把 diff 中的凭据换成 ``***``。

    审计记录本身会被 `GET /api/audit` 返回，diff 里留明文等于把凭据存进了一张
    可查询的表。``username`` 一并脱敏：它单独也是有用的情报。
    """
    return _SECRET_LINE.sub(r"\1***", text) if text else text


def version_of(data: bytes) -> str:
    """与加载器一致的版本号算法（`sha256` 前 16 位）。

    必须共用同一个口径，否则 `If-Match` 永远匹配不上。
    """
    return hashlib.sha256(data).hexdigest()[:16]


def issue_details(issues: Iterable[ValidationIssue]) -> list[dict[str, str]]:
    """校验问题 → 响应体里的 ``details``。

    只回代码、位置与说明。位置是配置键或「规则文件名:行号」，不含文件系统路径
    （DD_WEB §7.4）。
    """
    return [
        {"code": i.code, "location": i.location or "", "message": i.message, "level": i.level}
        for i in issues
    ]


def render_rules(rows: Iterable[RuleRow]) -> str:
    """规则表 → 每行 `条件<TAB>出口` 的文本快照。

    审计 diff 与备份快照共用这一种渲染：审计页与脱敏逻辑因此不需要为规则单独
    加一条分支。
    """
    return "".join(f"{condition}\t{upstream}\n" for _position, condition, upstream in rows)


def unified_diff(before: str, after: str, *, label: str) -> str | None:
    """给审计用的统一 diff。无差异时返回 ``None``。"""
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=label, tofile=label, lineterm="", n=1
        )
    )
    if not lines:
        return None
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... 其余 {len(lines) - MAX_DIFF_LINES} 行省略"]
    return "\n".join(lines)


class ConfigWriter:
    """唯一的配置写入点。

    进程内一把 `asyncio.Lock`：Web 固定单进程单 worker（DD_WEB §2.3），因此进程
    内互斥就够。多进程下这把锁会失效，那也正是禁止 `workers > 1` 的原因之一。
    """

    __slots__ = ("_app", "_lock")

    def __init__(self, app: Application) -> None:
        self._app = app
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._app.snapshot.source_path

    @property
    def backup_dir(self) -> Path:
        """备份目录取数据目录下的 ``backups/``。

        默认配置下即 `~/.r-proxy/backups/`（WEBUI_SPEC §6.2.1）；跟着
        `database.state_path` 走，容器或多实例部署改了数据目录时备份一起搬。
        """
        return self._app.snapshot.database.state_path.parent / BACKUP_DIR_NAME

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def edit_config(
        self,
        transform: Transform,
        *,
        expected_version: str,
        actor: str,
        action: str,
        target: str,
    ) -> str:
        """在锁内读基线、做变换、校验、写回并热重载，返回新的 ``config_version``。"""
        async with self._lock:
            before, version = await self._read_verified(self.path, expected_version)
            text = transform(before)
            await asyncio.to_thread(self._validate_config, text)
            return await self._commit(
                self.path,
                before=before,
                text=text,
                version_before=version,
                actor=actor,
                action=action,
                target=target,
            )

    async def read_rules(self) -> RulesSnapshot:
        """库里的当前规则与 ``revision``。客户端接下来会在这份列表上编辑。"""
        return await asyncio.to_thread(self._app.rules_store.read)

    async def write_rules(
        self,
        rules: Sequence[tuple[str, str]],
        *,
        expected_revision: int,
        actor: str,
    ) -> tuple[int, list[ValidationIssue]]:
        """整表替换，返回 ``(新 revision, 告警清单)``。

        `rules` 是 `(condition, upstream)` 的有序列表，`position` 由下标决定。
        三步保证：**校验版本 → 校验内容 → 单事务写入**。前两步任一失败时库一个
        字节都没动，`revision` 不变，客户端可以直接重试。

        乐观锁用 `rule_meta` 里的整数 `revision` 而不是内容哈希：要回答的问题是
        「我读到之后有没有人写过」，计数器正好表达这个；哈希表达的是「内容是否
        相同」，会在「重排后又排回原状」这类等价内容上产生假冲突。
        """
        async with self._lock:
            current = await asyncio.to_thread(self._app.rules_store.read)
            if current.revision != expected_revision:
                raise RulesConflict(expected=expected_revision, actual=current.revision)
            return await self._commit_rules(
                rules, current=current, actor=actor, action="rules.update", target=RULES_STEM
            )

    async def insert_rule(
        self,
        condition: str,
        upstream: str,
        *,
        actor: str,
        target: str,
    ) -> tuple[int, int, list[ValidationIssue]]:
        """把一条规则插到表首，返回 ``(位置, 新 revision, 告警清单)``。

        供粘性页的「固化为规则」使用（[DD_WEB §8.9](../../docs/design/DD_WEB.md)）。
        读当前表、插入、整表写回**都在这把锁里**完成，客户端因此不需要参与
        `revision` 协商：一键操作没有「请刷新后重试」的合理位置，而把读改写留在
        客户端就一定会有这一步。

        插表首而不是追加表尾：首匹配胜出下，表尾的新规则只要前面有 `*` 或更宽的
        `*.apex` 就永远不会命中——保存成功、只回一条告警、实际无效，是最难查的
        失败形态。表首则一定生效，代价是它会排在用户已有的顺序之前，这一点由界面
        在确认前说明。
        """
        async with self._lock:
            current = await asyncio.to_thread(self._app.rules_store.read)
            self._reject_duplicate(condition, upstream, current.rows)
            rules = [(condition, upstream), *((c, u) for _position, c, u in current.rows)]
            revision, issues = await self._commit_rules(
                rules, current=current, actor=actor, action="rules.promote", target=target
            )
            return PROMOTED_POSITION, revision, issues

    async def _commit_rules(
        self,
        rules: Sequence[tuple[str, str]],
        *,
        current: RulesSnapshot,
        actor: str,
        action: str,
        target: str,
    ) -> tuple[int, list[ValidationIssue]]:
        """校验 → 备份 → 单事务替换 → 热重载 → 审计。**调用方必须已持锁**。

        整表替换与插入共用这一段：两条写入路径各写一份，漂移的方向总是其中一条
        漏掉备份或审计。
        """
        issues = self.validate_rules(rules)
        before = render_rules(current.rows)
        after = render_rules(
            [(i, condition, upstream) for i, (condition, upstream) in enumerate(rules)]
        )

        await asyncio.to_thread(self._backup_rules, before)
        revision = await asyncio.to_thread(
            self._app.rules_store.replace,
            rules,
            expected_revision=current.revision,
            now_unix=int(time.time()),
        )
        await self._app.reload()
        self._app.storage.queue.put(
            config_audit(
                actor=actor,
                action=action,
                target=target,
                diff=unified_diff(before, after, label=RULES_STEM),
                version_before=str(current.revision),
                version_after=str(revision),
                now_unix=int(time.time()),
            )
        )
        return revision, issues

    def _reject_duplicate(self, condition: str, upstream: str, rows: Sequence[RuleRow]) -> None:
        """条件已在表里出现过就拒绝，不写库。

        比的是编译后的 `dedup_key` 而不是原始文本：`[2001:0db8::1]` 与
        `[2001:db8::1]` 是同一条规则，写法差异不该逃过检测。
        """
        candidate = compile_rules([(PROMOTED_POSITION, condition, upstream)])
        if not candidate.ok:
            raise ConfigInvalid(candidate.issues)
        key = candidate.rule_set.rules[0].dedup_key
        for rule in compile_rules(rows).rule_set.rules:
            if rule.dedup_key == key:
                raise RuleExists(position=rule.position, condition=rule.raw, upstream=rule.target)

    async def _commit(
        self,
        path: Path,
        *,
        before: str,
        text: str,
        version_before: str,
        actor: str,
        action: str,
        target: str,
    ) -> str:
        await asyncio.to_thread(self._replace, path, text)
        after = version_of(text.encode("utf-8"))
        # 写回成功后立即热重载。重载走标准的 tomllib 读路径，因此「Web 写入的
        # 配置」与「用户手写的配置」经过完全相同的校验与构建流程。
        await self._app.reload()
        self._app.storage.queue.put(
            config_audit(
                actor=actor,
                action=action,
                target=target,
                diff=mask_secrets(unified_diff(before, text, label=path.name)),
                version_before=version_before,
                version_after=after,
                now_unix=int(time.time()),
            )
        )
        return after

    # ------------------------------------------------------------------
    # 读取与校验
    # ------------------------------------------------------------------

    async def _read_verified(self, path: Path, expected: str) -> tuple[str, str]:
        """重读磁盘算哈希并比对，返回 ``(文本, 版本)``。

        **不能比对内存里缓存的版本号**：内存存的是上次加载时的值，与客户端提交
        的值一致，校验会通过并静默覆盖掉管理员用编辑器做的修改（DD_CONFIG §5.2）。
        """
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ConfigInvalid(
                [ValidationIssue("error", "E_READ_FAILED", f"无法读取 {path.name}: {exc.strerror}")]
            ) from exc
        actual = version_of(data)
        if actual != expected:
            raise ConfigConflict(expected=expected, actual=actual)
        return data.decode("utf-8"), actual

    def _validate_config(self, text: str) -> ConfigSnapshot:
        """解析 + 校验候选配置。**不写任何文件**。

        走的是与启动完全相同的加载与校验流程：Web 写入的配置与用户手写的配置
        必须过同一道关，否则会出现「界面存得下、重启起不来」。

        规则来自库而非候选配置：改 `config.toml` 不会改规则，但**会**改出口
        清单，因此必须拿库里的规则去比对新的出口清单——删掉一个仍被规则引用的
        出口应该在这里就被拦住。
        """
        try:
            candidate = load_text(text, self.path)
        except ConfigError as exc:
            raise ConfigInvalid(
                [ValidationIssue("error", "E_PARSE", _strip_path(str(exc), self.path))]
            ) from exc
        try:
            rows = self._app.rules_store.read_rules()
        except StorageError as exc:
            raise ConfigInvalid([ValidationIssue("error", "E_RULES_READ", str(exc))]) from exc
        loaded = compile_rules(rows)
        issues = loaded.issues + validate(
            candidate,
            has_ipv6_egress=probe_ipv6_egress(),
            rule_targets=loaded.rule_set.targets(),
        )
        if any(i.level == "error" for i in issues):
            raise ConfigInvalid(issues)
        return candidate

    def validate_rules(self, rules: Sequence[tuple[str, str]]) -> list[ValidationIssue]:
        """条件语法 + 出口存在性。有 error 时抛 :class:`ConfigInvalid`。

        出口是否存在只能对着**当前**配置判断。用户可能想先加规则再加出口，但那
        段时间里规则指向一个不存在的出口，启动校验同样会拒绝——两处口径必须
        一致，否则「存得下、起不来」。校验码也共用 `E_RULE_TARGET`。

        位置按提交列表的下标编号，与启动时按库内 `position` 编号得到的
        `rules[i]` 形状一致，界面上两处报错指向同一行。
        """
        loaded = compile_rules(
            [(i, condition, upstream) for i, (condition, upstream) in enumerate(rules)]
        )
        issues = list(loaded.issues)
        known = {u.name for u in self._app.snapshot.upstreams}
        for rule in loaded.rule_set.rules:
            if rule.target not in known:
                issues.append(
                    ValidationIssue(
                        "error", "E_RULE_TARGET", f"出口不存在: {rule.target}", rule.location
                    )
                )
        if any(i.level == "error" for i in issues):
            raise ConfigInvalid(issues)
        return issues

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------

    async def list_backups(self, *, stem: str = CONFIG_STEM) -> list[BackupInfo]:
        """某个来源文件的备份，按时间倒序。默认只列 `config.toml` 的。"""
        return await asyncio.to_thread(self._list_backups, stem)

    async def read_backup(self, filename: str) -> str:
        """按**白名单等值查找**读取备份。

        不做路径拼接：`../../etc/passwd` 不在列表里，直接 404。拼接后再
        `resolve()` 检查目录归属需要正确处理符号链接、大小写不敏感文件系统与
        `..` 规范化，任何一处疏漏都是漏洞（DD_WEB §6.3 同一理由）。
        """
        for info in await self.list_backups():
            if info.filename == filename:
                return await asyncio.to_thread(
                    (self.backup_dir / info.filename).read_text, encoding="utf-8"
                )
        raise FileNotFoundError(filename)

    def _list_backups(self, stem: str) -> list[BackupInfo]:
        directory = self.backup_dir
        if not directory.is_dir():
            return []
        prefix = f"{stem}-"
        found: list[BackupInfo] = []
        for entry in directory.iterdir():
            if not entry.is_file() or not entry.name.startswith(prefix):
                continue
            if not BACKUP_PATTERN.match(entry.name):
                continue
            stat = entry.stat()
            found.append(
                BackupInfo(filename=entry.name, size=stat.st_size, created_at=stat.st_mtime)
            )
        # 按 mtime 排，不按文件名：同一秒内的第二份会带 `-1` 后缀，而 `-` 的
        # 码位小于 `.`，字典序会把它排在无后缀的那份**之前**——轮转于是先删掉
        # 最新的备份。备份文件写完就不再改动，mtime 即创建时间。
        found.sort(key=lambda b: (b.created_at, b.filename), reverse=True)
        return found

    # ------------------------------------------------------------------
    # 原子写
    # ------------------------------------------------------------------

    def _replace(self, path: Path, text: str) -> None:
        """备份 → 轮转 → 原子替换。同步执行，调用方负责放进 `to_thread`。"""
        self._backup(path)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            # config.toml 含明文 auth_token，`open()` 按 umask 默认建出 644；
            # `os.replace` 是纯 rename，目标文件继承 tmp 的权限位，因此必须
            # 在这里限到 600，而不是指望调用方后续再补一次 chmod。
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # 对父目录 fsync 容易被漏掉：`os.replace` 保证了替换的原子性，但目录项
        # 本身的落盘要单独同步，否则断电后可能「新内容已写入而目录仍指向旧
        # inode」。
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _backup(self, path: Path) -> None:
        """配置文件的备份。

        前缀固定用 `config`：实际文件名可能被 `--config` 指成别的，而轮转按前缀
        分组，名字跟着变会让历史备份被当成另一个文件而永不淘汰。
        """
        self._write_backup(CONFIG_STEM, path.read_bytes(), suffix=".toml")

    def _backup_rules(self, text: str) -> None:
        """保存前把当前规则导出为可读文本快照（WEBUI_SPEC §6.2.1）。

        仅供人工查阅与灾难恢复，**不是**受支持的输入格式——系统不从快照导入。
        """
        self._write_backup(RULES_STEM, text.encode("utf-8"), suffix="")

    def _write_backup(self, stem: str, data: bytes, *, suffix: str) -> None:
        directory = self.backup_dir
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime(BACKUP_STAMP)
        target = directory / f"{stem}-{stamp}{suffix}"
        # 同秒内的两次写入会撞名。加序号而不是覆盖：备份的意义就是每一版都留着。
        serial = 1
        while target.exists():
            target = directory / f"{stem}-{stamp}-{serial}{suffix}"
            serial += 1
        target.write_bytes(data)
        # config 备份是明文全文快照，同样含 auth_token；与 _replace 保持一致
        # 收到 600，不依赖 umask。
        os.chmod(target, 0o600)
        self._rotate(stem, keep=self._app.snapshot.database.backup_keep)

    def _rotate(self, stem: str, *, keep: int) -> None:
        """只轮转同一来源的备份。

        按 `stem` 分别计数：`config.toml` 与规则快照共用一个目录，混在一起数
        会让改一次规则冲掉九份配置备份。
        """
        for stale in self._list_backups(stem)[max(keep, 1) :]:
            (self.backup_dir / stale.filename).unlink(missing_ok=True)


def _strip_path(message: str, path: Path) -> str:
    """把消息里的绝对路径换成文件名。

    解析错误必须告诉用户哪里写错了，但响应体不该泄露部署路径。
    """
    return message.replace(str(path), path.name)


def describe_changes(changes: dict[str, object]) -> str:
    """结构化改动的可读摘要。仅用于日志，落库的 diff 走 :func:`unified_diff`。"""
    return ", ".join(f"{key}={value!r}" for key, value in sorted(changes.items()))
