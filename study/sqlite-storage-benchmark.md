# SQLite 存储层选型基准

| 版本 | 日期 | 变更说明 | 作者 |
|------|------|----------|------|
| v1.0.0 | 2026-08-12 | 初始版本，验证并发丢更新与写入吞吐，确定存储架构 | Agent |

## 1. 背景

需求评审提出三个并发竞态场景，并质疑 SQLite 在高并发场景下的适用性：

- 场景 A：同一 host 的并发请求同时使用已失效的粘性出口，造成雪崩
- 场景 B：并发失败时竞相写 SQLite，`fail_count` 计数不准
- 场景 C：一个请求已更新粘性，另一个请求仍读到旧值

本文用实测数据判定这些场景是否成立，并确定 r-proxy 的存储架构。

复现命令：

```bash
/media/data/venv/bin/python study/scripts/sqlite_bench.py

# 指定其他文件系统对照
BENCH_DIR=/tmp/bench /media/data/venv/bin/python study/scripts/sqlite_bench.py
```

## 2. 测试环境

| 项目 | 值 |
|------|-----|
| SQLite | 3.51.2 |
| Python | 3.12.13 |
| PRAGMA | `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000` |
| 文件系统 | XFS on SATA（`/media/data`）、XFS on NVMe（`/`）、tmpfs（`/tmp`） |

## 3. 结论摘要

| 结论 | 依据 |
|------|------|
| 场景 B 成立且后果严重 | deferred 事务丢失 75% 的更新 |
| `BEGIN IMMEDIATE` 可解决，但 SQL 侧自增更优 | 两者均零丢失，后者无需显式事务 |
| SQLite 适合本项目 | WAL 下读写不互斥，批量写吞吐远超代理量级 |
| 真正的风险是阻塞事件循环，不是锁竞争 | 单行提交需 0.11–0.47ms，批量后降至 0.0025ms |
| 必须批量写入 | 批量化带来 44–190 倍吞吐提升 |

## 4. 实测数据

### 4.1 写入吞吐（受 fsync 主导，随磁盘差异显著）

| 写法 | SATA (XFS) | NVMe (XFS) | tmpfs |
|------|-----------:|-----------:|------:|
| 单行自动提交 | 2,114 rows/s | 8,948 rows/s | 69,696 rows/s |
| 批量 500 行/事务 | 400,544 rows/s | 393,411 rows/s | 440,174 rows/s |
| 粘性 UPSERT（单条） | 6,843 ops/s | 22,925 ops/s | 96,163 ops/s |

**解读**：单行提交的耗时几乎完全由 fsync 决定，因此在不同磁盘上相差 33 倍。批量写入把 500 行摊到一次 fsync 上，结果与磁盘类型基本无关，稳定在 40 万行/秒左右。

换算成单行成本：

| 写法 | SATA | NVMe |
|------|-----:|-----:|
| 单行自动提交 | 0.47ms | 0.11ms |
| 批量 500 行/事务 | 0.0025ms | 0.0025ms |

> 部署目录 `~/.r-proxy/`（含 `state.db` 与 `logs.db`）位于 NVMe 分区，故以 NVMe 数据为准；SATA 数据代表低配环境下的下界。

### 4.2 并发丢更新（4 线程 × 500 次自增，期望 2000）

| 写法 | 最终计数 | SQLITE_BUSY | 结果 |
|------|--------:|------------:|------|
| `BEGIN`（deferred）+ Python 读改写 | **509** | 1491 | 丢失 75% |
| `BEGIN IMMEDIATE` + Python 读改写 | 2000 | 0 | 正确 |
| `UPDATE ... SET c = c + 1`（自动提交） | 2000 | 0 | 正确 |
| `UPSERT ... DO UPDATE SET c = c + 1` | 2000 | 0 | 正确 |

**deferred 为何失败**：两个连接各自以 deferred 事务开始，都先取得共享读锁，随后都试图升级为写锁。持有读锁的一方无法退避，`busy_timeout` 起不了作用，SQLite 立即返回 `SQLITE_BUSY`。

**为何 SQL 侧自增免疫**：读和写发生在同一条语句内部，SQLite 在语句级别保证原子性，Python 侧不存在读改写窗口。这个方案比 `BEGIN IMMEDIATE` 更简单，也不需要显式事务管理。

### 4.3 manual 绑定保护

验证 UPSERT 的 `WHERE host_upstream.source != 'manual'` 条件能否阻止自动写入覆盖管理员的手动绑定：

```
初始：('proxy-c', 'manual')
执行：auto 写入 proxy-a
结果：('proxy-c', 'manual')   OK
```

### 4.4 WAL 下写入进行中的读取延迟

在持续批量写入的同时，用只读连接查询 10 万行的 `request_log`：

```
avg = 0.26ms    max = 1.10ms
```

WAL 模式下读者不被写者阻塞，Web 管理界面的查询不会干扰代理转发。

## 5. 架构决策

### 5.1 保留 SQLite

理由：

- 单机单进程本地代理，不存在多节点共享数据库的需求
- WAL 下读写互不阻塞（实测 0.26ms 查询延迟）
- Python 标准库自带，符合"代理核心零第三方依赖"的定位
- 批量写吞吐 40 万行/秒，比代理自身的请求量级高出几个数量级

SQLite 的单写者限制在这里没有成本，因为单进程应用本来就应该只有一个写者。

### 5.2 必须配套的四项约束

1. **热路径完全不碰数据库**。路由决策只读内存中的粘性 LRU 缓存与健康状态表。理由见 §4.1：即使在 NVMe 上，同步写一行也要 0.11ms，每请求 2 行就是 0.22ms 的事件循环阻塞，期间所有代理连接的数据转发全部停摆。

2. **单一批量写者**。写入投递到有界 `asyncio.Queue`，由后台专用线程每 200ms 或积满 500 条批量落盘。Web 界面的写入走同一队列，确保真的只有一个写者。

3. **拆分两个数据库文件**：

   | 文件 | 内容 | 特征 |
   |------|------|------|
   | `state.db` | `host_upstream`、`upstream_health` | 体量小、写入频率低、不可丢失 |
   | `logs.db` | `request_log` | 高频写入、可丢弃、需定期轮转 |

   拆分后日志写入的争用不会影响路由状态的持久化，日志清理与轮转也互不牵连。

4. **队列满时分级降级**：丢弃 `request_log`（可观测性降级可接受），但 `host_upstream` 的变更必须保留。

### 5.3 SQL 写法约定

| 场景 | 写法 |
|------|------|
| 计数器累加 | `UPDATE ... SET c = c + 1`，禁止 Python 侧读改写 |
| 粘性映射更新 | `INSERT ... ON CONFLICT(host) DO UPDATE ... WHERE source != 'manual'` |
| 批量日志写入 | `BEGIN IMMEDIATE` + `executemany` + `COMMIT` |
| Web 界面查询 | 独立的 `mode=ro` 只读连接 |

### 5.4 SQLite 不再适用的边界

- 多实例共享同一数据库，尤其位于网络文件系统上 → 改用 PostgreSQL
- 需要跨机器聚合分析 → 外部时序数据库
- 持续超过约 5000 attempts/s 且要求全量审计 → 改为采样记录或追加式日志文件

这三种情况均超出"本地单机代理"的产品定位，本期不予考虑。

## 6. 被否决的替代方案

| 方案 | 否决理由 |
|------|----------|
| Redis | 对粘性与健康状态很合适，但引入常驻进程依赖，审计日志仍需另找存储，与本地零依赖定位冲突 |
| PostgreSQL | 单机本地代理的运维负担过重 |
| 纯内存 + 定期快照 | 崩溃时丢失粘性映射与审计日志；Web 界面的分页筛选需自行实现 |
| JSONL 日志文件 + SQLite 仅存状态 | 日志写入更快，但 Web 界面的筛选与分页要自己实现，收益不抵成本 |

## 7. 相关文档

- [../docs/requirements/PRD_OVERVIEW.md](../docs/requirements/PRD_OVERVIEW.md) §4.4 存储模型、§4.9 并发与一致性
- [scripts/sqlite_bench.py](./scripts/sqlite_bench.py) — 基准脚本
