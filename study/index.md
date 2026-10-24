# 技术预研索引

| 版本号 | 日期 | 变更说明 | 作者 |
| :--- | :--- | :--- | :--- |
| v1.2.0 | 2026-10-24 | 新增自动粘性有效期小时输入及提示实施计划 | Agent |
| v1.1.0 | 2026-10-24 | 索引互斥规则实施计划及历史只读盘点 | Agent |

记录技术选型、对比实验与可行性分析。

| 文档 | 说明 |
|------|------|
| [2026-10-24-exclusive-rules-plan.md](./2026-10-24-exclusive-rules-plan.md) | 已确认的互斥规则实现计划、验证记录与历史配置只读盘点边界 |
| [sqlite-storage-benchmark.md](./sqlite-storage-benchmark.md) | SQLite 并发丢更新与写入吞吐实测，确定存储层架构 |

## 脚本

| 脚本 | 说明 |
|------|------|
| [scripts/sqlite_bench.py](./scripts/sqlite_bench.py) | SQLite 写入吞吐与并发竞态基准 |

## 相关文档

- [需求文档](../docs/requirements/index.md)
- [设计文档](../docs/design/index.md)

- [自动粘性有效期小时输入实施计划](2026-10-24-sticky-ttl-hours-plan.md) — 已确认方案、转换兼容与提示交互验证。
