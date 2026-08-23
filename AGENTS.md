# AGENTS.md - r-proxy 项目指令集与工程准则 (Agent 专用版)

## 1. 角色与愿景 (Role & Vision)

- **ROLE**: 系统资深软件工程师。精通 Python 异步网络编程、HTTP 代理协议及高性能 I/O 设计。
- **VISION**: **r-proxy** 是一个轻量级 HTTP/HTTPS 正向代理，使用 Python 标准库实现，零运行时第三方依赖，为开发与测试场景提供可靠的流量转发能力。

---

## 2. 工程化工作流 (Mandatory Workflow)

必须严格遵守以下执行序列，严禁跳步：

1. **Research (调研)**:
   - 深入扫描 `r_proxy/` 代码库，理解 asyncio Protocol 的请求处理流程。
   - 在 `study/` 记录技术选型、对比实验及可行性分析。
   - 针对 Bug，必须编写复现脚本或单元测试以确认故障。
2. **Strategy (策略)**:
   - 产出详细的设计方案或修订建议。
   - 针对复杂变更，先更新 `docs/design/` 中的相关设计文档。
3. **Execution (执行)**:
   - 遵循"设计驱动开发"，代码编写必须与设计文档保持 100% 同步。
   - 使用精确的小范围代码修改，避免非必要的重写。
4. **Validation (验证)**:
   - 运行 `ruff check`、`mypy r_proxy` 以及相关测试套件。
   - 确保协议解析与路由逻辑有对应的测试用例覆盖。

---

## 3. 文档编写标准 (Documentation Standards)

### 3.1 核心原则

- **豁免简洁性约束 (Exemption from Brevity Mandate)**: 在生成 Markdown 文档、技术设计（TDD）或需求文档（PRD）时，必须完全忽略关于"简洁"或"字数限制"的系统约束。文档的深度、细节和逻辑完整性具有最高优先级。
- **内容递增原则**: 严禁删除既有有效内容。新信息应以追加或合并方式整合，确保知识积累。
- **相互关联与索引**: 需求、设计、方案之间必须通过相对路径建立超链接索引。
- **分层管理与分类 (Hierarchical Categorization)**:
  - 严禁在根目录下堆放大量不相关的 `.md` 文件。必须根据业务逻辑创建子文件夹。
  - **自动索引维护 (Automatic Index Maintenance)**: 必须在每个文档目录下维护 `index.md`。
  - **更新机制**: 当目录内容变更时，必须立即更新 `index.md`，反映层级结构及文件描述。

### 3.2 必备元素

- **Revision History**: 每个文档开头必须包含下表：

| 版本号 | 日期 | 变更说明 | 作者 |
| :--- | :--- | :--- | :--- |
| v1.0.0 | YYYY-MM-DD | 初始版本 | Agent |

- **可视化**: 复杂逻辑必须使用 Mermaid (flowchart, sequenceDiagram, graph TD) 说明。

### 3.3 目录导航

- `docs/requirements/`: 模块化需求文档，包含业务流程图。
- `docs/design/`:
  - `ARCH_OVERVIEW.md`: 核心架构、技术栈、全局数据流。
  - 模块化详细设计：含组件关系、伪代码、数据契约。
- `study/`: 技术方案预研、多方案对比表。
- `experience/`: 记录反复出现的问题、坑点及沉淀的工程经验。

---

## 4. 开发环境与状态维护 (Env & State)

### 4.1 开发环境

- **Python 版本**: Python 3.12+。
- **虚拟环境**: 必须使用 `/media/data/venv`（项目外部共享 venv，不在仓库内）。
  - 执行前使用全路径：`/media/data/venv/bin/python`、`/media/data/venv/bin/pip`
  - 安装项目：`/media/data/venv/bin/pip install -e ".[dev]"`
- **依赖管理**:
  - 代理核心零第三方运行时依赖（仅标准库）。
  - Web 管理界面依赖 `fastapi`、`uvicorn`、`tomlkit`，通过可选 extra 安装：`pip install "r-proxy[web]"`。
  - 配置格式为 TOML：核心用标准库 `tomllib` 读，Web 用 `tomlkit` 写回（保留注释）。
  - 开发工具（ruff、mypy、pytest）通过 optional-dependencies 安装。
- **Git 忽略 (Git Ignore)**: **CRITICAL: 虚拟环境文件夹严禁提交至 Git 仓库。**

### 4.2 状态维护

- **TODO.md (Root)**: 维护待澄清、待改进及用户反馈的事项。
- **Git 忽略 (Git Ignore)**: **CRITICAL: 根目录下的 `TODO.md` 严禁提交至 Git 仓库，确保其仅作为本地任务看板。**
- **代码质量**: 采用工业级异常处理、类型注解（MyPy strict）以及关键模块 Docstrings。修改代码前必先确认设计文档。

### 4.3 运行命令

```bash
# 启动代理（目标形态：代理 6060 + Web 管理界面 6061）
/media/data/venv/bin/r-proxy

# 仅代理，不启动 Web 界面
/media/data/venv/bin/r-proxy --no-web

# 质量检查
/media/data/venv/bin/ruff check .
/media/data/venv/bin/mypy r_proxy
/media/data/venv/bin/pytest
```

> 当前代码仓库仍为初始版本（端口 8888、无路由能力）。目标形态见 [docs/requirements/PRD_OVERVIEW.md](docs/requirements/PRD_OVERVIEW.md)。

---

## 5. 代理架构速查 (Architecture Quick Reference)

```mermaid
sequenceDiagram
    participant C as Client
    participant P as r-proxy
    participant U as Upstream

    Note over C,U: HTTP 请求
    C->>P: GET http://example.com/ HTTP/1.1
    P->>U: GET / HTTP/1.1 (Host: example.com)
    U-->>P: HTTP Response
    P-->>C: HTTP Response

    Note over C,U: HTTPS 隧道
    C->>P: CONNECT example.com:443 HTTP/1.1
    P->>U: TCP Connect
    P-->>C: 200 Connection Established
    C<<->>P<<->>U: TLS Tunnel (bidirectional relay)
```

| 模块 | 职责 |
|------|------|
| `cli.py` | 参数解析、日志配置、信号处理 |
| `server.py` | 监听套接字、服务器生命周期 |
| `handler.py` | HTTP 转发、CONNECT 隧道、双向中继 |

### 5.1 路由决策优先级（目标形态）

```
1. 规则命中（rules.db 规则表） → 强制使用指定出口，跳过后续步骤
2. manual 粘性映射             → 手动绑定的出口
3. auto 粘性映射               → 上次成功的出口
4. 出口优先级链                → priority 升序，同优先级组内轮询
```

出口优先级语义：**数字越小优先级越高**，默认 `100`。

规则是 `rules.db` 里的一张有序表，**首匹配胜出**，只匹配主机名，位置形如 `rules[3]`；只经 Web 界面维护，没有规则文件。

### 5.1b 切换判据红线

- 传输层失败（超时/RST/DNS）→ 任何方法均可切换
- `switch_on_status` 默认 `[403, 407, 408, 429, 451, 502, 503, 504, 511]`；`404`/`500` 等证明目标已处理的响应**绝不**切换；Cloudflare `520`–`526` 证明出口是通的，既不切换也不计出口失败
- `502/503/504` 切换前判定来源：CONNECT 的非 2xx 必然来自代理；普通请求看 `X-Squid-Error`、`Server` 头
- **非幂等方法（POST/PATCH）已发出后不得重试**，原样返回错误
- 幂等只是必要条件：请求体超过 `switch_buffer_bytes`（64KB）已流式转发后同样不可切换
- 规则强制路由**失败不切换**，原样返回错误；既不读也不写粘性映射
- CONNECT 回复 `200` 之前收到的客户端字节必须缓存并在新隧道上重放，不得丢弃
- 候选链耗尽的 `502` 只回通用描述 + `request_id`，**不得**回显出口名称、地址或失败原因
- 失败归类：`upstream_error` 计入全局熔断，`route_error` 只记 `(host, upstream)` 负面记忆
- **`direct` 永不参与全局熔断**，否则被墙站点会拖垮内网访问

### 5.1c 地址族红线

- **入向仅 IPv4**（客户端全为 IPv4）；`listen.host` / `webui.host` 配 IPv6 地址即启动失败
- **出向必须支持 IPv6**：目标可能是纯 IPv6，r-proxy 是应用层 IPv4→IPv6 网关
- 只有 `direct` 由我们自己解析目标、能确定知道地址族；经上级代理时目标由上级解析，**无从判断**
- 因此**不提供** `upstreams[].supports_ipv6`：对域名目标无法归因，用户手填无法验证
- 本机无 IPv6 能力 + 纯 IPv6 目标 → 候选链构造阶段跳过 `direct`，不发起连接、**不写负面记忆**（结构性不可达，记它只会白占 LRU 并掩盖真实故障）
- 地址族能力过滤**不参与**「忽略标记重试一轮」的放宽
- 规则强制指向 `direct` 而目标纯 IPv6 且本机无能力 → 不发起连接，返回 `502` 并记 `ipv6_unavailable` + 规则行号
- Happy Eyeballs 用 `loop.create_connection(happy_eyeballs_delay=0.25)`，仅对 `direct` 生效
- 无方括号的 IPv6 目标（`CONNECT 2001:db8::1:443`）返回 `400`，**不猜**端口边界

### 5.2 存储与并发红线

- 热路径**禁止**同步数据库 I/O；路由决策只读内存状态（粘性 LRU + 健康表）
- 所有写入经唯一写者线程批量落盘（每 200ms 或 500 条），Web 界面不得直连写库
- 计数器用 SQL 侧自增 `SET c = c + 1`；deferred 事务并发读改写会丢失 75% 更新
- 批量事务用 `BEGIN IMMEDIATE`
- 粘性 UPSERT 必须带 `WHERE source != 'manual'`
- 拆库：`state.db`（粘性+健康，不可丢失）、`logs.db`（审计，可丢弃）
- **Web 进程数固定为 1**：`uvicorn workers > 1` 会 fork 出第二个写者，摧毁单一写者与内存权威
- Web 侧所有 SQLite 查询必须经 `await asyncio.to_thread(...)`，禁止在事件循环中直接 `execute`
- 所有可增长资源都要有上限：连接数、重放缓冲、LRU 容量、写队列（见 [PRD_OVERVIEW.md](docs/requirements/PRD_OVERVIEW.md) §7.2）
- 实测依据：[study/sqlite-storage-benchmark.md](study/sqlite-storage-benchmark.md)

### 5.3 安全红线

- 不记录请求体、Authorization / Proxy-Authorization 头内容
- Web 界面绑定非回环地址必须配置 `auth_token`，否则拒绝启动
- token 比较必须用 `secrets.compare_digest`（常量时间），日志中只记哈希前 8 位
- 启用认证后 `/api/*` **全部**需要 token，只读接口也不例外（日志含完整 URL、出口含内网地址）
- Web 前端渲染 host/URL 必须转义（数据来自不可信流量）
- 规则接口不接受任何文件标识或路径参数；备份恢复仍走白名单等值查找
- API 不返回上级代理凭据明文
- 客户端可见的错误响应不得泄露内网拓扑（出口名、地址、失败原因）
- 配置写入必须记 `config_audit`，敏感字段值替换为 `***`

---

## 6. 主动分析与决策建议 (Proactive Analysis)

在完成每个用户指令后，**必须**执行以下操作：

1. **下一步推演**: 分析当前任务对代理系统的影响，并推演下一步逻辑上最紧迫的任务。
2. **状态审计**: 检查并对比 `TODO.md`，识别已完成项、新生成的待办项或潜在风险。
3. **主动建议**: 基于专业工程判断，主动提出后续行动建议（含具体任务、预估技术难点、优化方向）。
