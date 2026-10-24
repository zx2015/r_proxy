"""只读盘点部署规则：不建库、不迁移、不启动应用、不修改运行状态。

默认 CLI 只输出数量摘要；``--output`` 显式保存含 host/规则内容的详细 JSON。
容器内 ``/data`` 路径须以 ``--data-dir`` 显式映射到宿主挂载目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from r_proxy.config.loader import DEFAULT_RULES_PATH
from r_proxy.rules import loader as rules_loader

MAX_AUDIT_RULES = 5_000


class AuditError(Exception):
    """读取或结构错误；调用方可以报告，但不能尝试自动修复。"""


@dataclass(frozen=True, slots=True)
class StoredRule:
    id: int
    position: int
    condition: str
    upstream: str


@dataclass(frozen=True, slots=True)
class AuditIssue:
    level: str
    code: str
    location: str | None
    message: str
    conflict: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class AuditReport:
    database_path: str
    enabled: bool
    revision: str | None
    schema_version: str | None
    snapshot_sha256: str
    rows: tuple[StoredRule, ...]
    issues: tuple[AuditIssue, ...]
    upstream_count: int
    analysis_version: str
    snapshot_consistent: bool = True
    source_changes: int = 0

    @property
    def rule_count(self) -> int:
        return len(self.rows)

    @property
    def regex_count(self) -> int:
        return sum(_is_regex(row.condition) for row in self.rows)

    @property
    def ready(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    @property
    def analysis_complete(self) -> bool:
        return not any(
            issue.code in {"E_RULE_CHECK_LIMIT", "E_RULE_CHECK_INCOMPLETE"} for issue in self.issues
        )

    def summary(self) -> dict[str, object]:
        """摘要不包含规则文本、host、上级代理地址或认证字段。"""
        return {
            "database_path": self.database_path,
            "enabled": self.enabled,
            "revision": self.revision,
            "schema_version": self.schema_version,
            "rule_count": self.rule_count,
            "regex_count": self.regex_count,
            "upstream_count": self.upstream_count,
            "ready": self.ready,
            "analysis_complete": self.analysis_complete,
            "analysis_version": self.analysis_version,
            "issue_counts": dict(Counter(issue.code for issue in self.issues)),
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_consistent": self.snapshot_consistent,
            "source_changes": self.source_changes,
        }

    def details(self) -> dict[str, object]:
        return {
            **self.summary(),
            "rows": [asdict(row) for row in self.rows],
            "issues": [asdict(issue) for issue in self.issues],
        }


def _is_regex(condition: str) -> bool:
    text = condition.strip()
    return text.startswith("^") or (len(text) >= 2 and text.startswith("/") and text.endswith("/"))


def _table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditError(f"配置表 {name} 的类型不正确")
    return value


def _resolve_database(config: dict[str, Any], config_path: Path, data_dir: Path | None) -> Path:
    raw = _table(config.get("database", {}), "database").get("rules_path", DEFAULT_RULES_PATH)
    if not isinstance(raw, str) or not raw:
        raise AuditError("database.rules_path 必须为非空路径字符串")
    path = Path(raw).expanduser()
    if data_dir is not None and path.is_relative_to("/data"):
        path = data_dir / path.relative_to("/data")
        if not path.resolve().is_relative_to(data_dir.resolve()):
            raise AuditError("容器数据路径越出了显式映射目录")
    elif not path.is_absolute():
        # 配置加载器原先按工作目录展开相对路径；离线工具不能默猜容器工作目录。
        raise AuditError("rules_path 为相对路径，请先提供明确的绝对路径配置副本")
    resolved = path.resolve()
    if not resolved.is_file():
        raise AuditError(f"规则数据库不存在或不是普通文件：{resolved}")
    if resolved == config_path.resolve():
        raise AuditError("规则数据库路径不能指向配置文件")
    return resolved


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # table 仅来自下面的固定内部白名单，不接受用户输入作为 SQL 标识符。
    if table not in {"rule", "rule_meta", "schema_meta"}:
        raise AuditError("不支持读取此表")
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _metadata(conn: sqlite3.Connection, table: str, key: str) -> str | None:
    if not {"key", "value"} <= _columns(conn, table):
        return None
    row = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _snapshot(database: Path) -> tuple[tuple[StoredRule, ...], str | None, str | None, int]:
    try:
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise AuditError("无法以只读模式打开规则数据库") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        if not {"id", "position", "condition", "upstream"} <= _columns(conn, "rule"):
            raise AuditError("规则数据库的 rule 表不存在或字段不符合预期；未执行迁移")
        revision = _metadata(conn, "rule_meta", "revision")
        schema_version = _metadata(conn, "schema_meta", "schema_version")
        stored: list[StoredRule] = []
        for row in conn.execute(
            "SELECT id, position, condition, upstream FROM rule ORDER BY position,id LIMIT ?",
            (MAX_AUDIT_RULES + 1,),
        ):
            if len(stored) >= MAX_AUDIT_RULES:
                raise AuditError("规则数超过只读盘点上限 5000，未完成全量分析")
            if not (
                isinstance(row[0], int)
                and isinstance(row[1], int)
                and isinstance(row[2], str)
                and isinstance(row[3], str)
            ):
                raise AuditError("rule 表含类型异常数据；未改写该行")
            stored.append(StoredRule(row[0], row[1], row[2], row[3]))
        changes = conn.total_changes
        conn.rollback()
        return tuple(stored), revision, schema_version, changes
    except sqlite3.Error as exc:
        raise AuditError(f"无法只读查询规则数据库（{type(exc).__name__}）；未执行修复") from exc
    finally:
        conn.close()


def audit_config(config_path: Path, *, data_dir: Path | None = None) -> AuditReport:
    """在只读事务内盘点原始规则，再离线进行严格分析；不输出配置认证内容。"""
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"无法读取 TOML 配置（{type(exc).__name__}）") from exc
    database = _resolve_database(config, config_path, data_dir)
    rules_table = _table(config.get("rules", {}), "rules")
    enabled = rules_table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AuditError("rules.enabled 必须为布尔值")
    upstreams: dict[str, bool] = {"direct": True}
    items = config.get("upstreams", [])
    if not isinstance(items, list):
        raise AuditError("upstreams 必须为配置表数组")
    for item in items:
        upstream = _table(item, "upstreams")
        name = upstream.get("name")
        active = upstream.get("enabled", True)
        if not isinstance(name, str) or not isinstance(active, bool):
            raise AuditError("出口名称或启用状态类型不正确")
        upstreams[name] = active
    rows, revision, schema_version, changes = _snapshot(database)
    issues: list[AuditIssue] = []
    if revision is None or schema_version is None:
        issues.append(
            AuditIssue("error", "E_AUDIT_METADATA", None, "规则库缺少 revision 或 schema_version")
        )
    positions: set[int] = set()
    for row in rows:
        location = f"rules[{row.position}]"
        if row.position < 0 or row.position in positions:
            issues.append(
                AuditIssue("warning", "W_RULE_POSITION", location, "原始位置为负数或重复")
            )
        positions.add(row.position)
        if row.upstream not in upstreams:
            issues.append(
                AuditIssue("error", "E_RULE_TARGET_UNKNOWN", location, "引用的出口不存在")
            )
        elif not upstreams[row.upstream]:
            issues.append(
                AuditIssue("warning", "W_RULE_TARGET_DISABLED", location, "引用的出口已禁用")
            )
    analysis_version = getattr(rules_loader, "ANALYSIS_VERSION", "legacy")
    if analysis_version != "exclusive-v2":
        issues.append(
            AuditIssue(
                "error",
                "E_RULE_CHECK_INCOMPLETE",
                None,
                "当前规则分析器尚未实现互斥检查，不能据旧遮蔽检查判定迁移就绪",
            )
        )
    result = rules_loader.compile_rules(
        [(row.position, row.condition, row.upstream) for row in rows]
    )
    for issue in result.issues:
        issues.append(
            AuditIssue(
                issue.level,
                issue.code,
                issue.location,
                issue.message,
                getattr(issue, "conflict", None),
            )
        )
    payload = {
        "revision": revision,
        "schema_version": schema_version,
        "rows": [asdict(row) for row in rows],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return AuditReport(
        str(database),
        enabled,
        revision,
        schema_version,
        digest,
        rows,
        tuple(issues),
        len(upstreams),
        analysis_version,
        source_changes=changes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读盘点历史规则，默认仅输出摘要")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, help="将配置中的 /data 显式映射到此目录")
    parser.add_argument("--output", type=Path, help="显式保存含 host/规则的详细 JSON 报告")
    args = parser.parse_args(argv)
    try:
        report = audit_config(args.config, data_dir=args.data_dir)
        if args.output is not None:
            # 报告含 host，创建时即设 0600，且拒绝覆盖任何既有文件/符号链接。
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(report.details(), ensure_ascii=False, indent=2) + "\n")
    except (AuditError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report.summary(), ensure_ascii=False))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
