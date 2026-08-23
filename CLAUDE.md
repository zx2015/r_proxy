# CLAUDE.md - r-proxy 项目指令集与工程准则

## 1. 角色与愿景 (Role & Vision)

- **ROLE**: 系统软件工程师，精通 Python 异步网络编程与 HTTP 代理协议。
- **VISION**: **r-proxy** 是一个轻量级 HTTP/HTTPS 正向代理，使用 Python 标准库实现，零运行时第三方依赖，易于部署与扩展。

---

## 2. 工程化工作流 (Mandatory Workflow)

必须严格遵守以下执行序列，严禁跳步：

1. **Research (调研)**:
   - 深入扫描 `r_proxy/` 代码，理解 asyncio Protocol 处理流程。
   - 在 `study/` 记录技术选型、协议对比及可行性分析。
   - 针对 Bug，必须编写复现脚本或单元测试以确认故障。
2. **Strategy (策略)**:
   - 产出详细的设计方案或修订建议。
   - 针对复杂变更，先更新 `docs/design/` 中的相关设计文档。
3. **Execution (执行)**:
   - 遵循"设计驱动开发"，代码编写必须与设计文档保持同步。
   - 使用精确的小范围修改，避免非必要的重写。
4. **Validation (验证)**:
   - 运行 `ruff check`、`mypy r_proxy` 以及 `pytest`。
   - 确保协议解析与路由逻辑有对应的测试用例覆盖。

---

## 3. 文档编写标准 (Documentation Standards)

### 3.1 核心原则

- **豁免简洁性约束**: 生成 Markdown 文档或技术设计时，文档的深度、细节和逻辑完整性具有最高优先级。
- **内容递增原则**: 严禁删除既有有效内容。新信息应以追加或合并方式整合。
- **相互关联与索引**: 需求、设计、方案之间必须通过相对路径建立超链接索引。
- **分层管理**: 严禁在根目录下堆放大量不相关的 `.md` 文件。必须根据业务逻辑创建子文件夹。
- **自动索引维护**: 必须在每个文档目录下维护 `index.md`。

### 3.2 必备元素

- **Revision History**: 每个文档开头必须包含修订历史表。
- **可视化**: 复杂协议流程必须使用 Mermaid (sequenceDiagram, flowchart) 说明。

### 3.3 目录导航

- `docs/requirements/`: 模块化需求文档。
- `docs/design/`: 架构概览与模块详细设计。
- `study/`: 技术方案预研、多方案对比表。
- `experience/`: 记录反复出现的问题、坑点及沉淀的工程经验。

---

## 4. 开发环境与状态维护 (Env & State)

### 4.1 开发环境

- **Python 版本**: Python 3.12+。
- **虚拟环境**: 必须使用 `/media/data/venv`（项目外部共享 venv）。
  - 执行指令示例：`/media/data/venv/bin/python`、`/media/data/venv/bin/pip install -e ".[dev]"`
  - **禁止**在仓库内创建 `./venv`。
- **依赖管理**:
  - 代理核心零第三方运行时依赖（仅标准库）。
  - Web 管理界面依赖 `fastapi`、`uvicorn`、`tomlkit`，作为可选 extra 安装：`pip install "r-proxy[web]"`。
  - 配置格式为 TOML：核心用标准库 `tomllib` 读，Web 用 `tomlkit` 写回（保留注释）。
  - 开发依赖通过 `pyproject.toml` 的 `[project.optional-dependencies]` 管理。
- **Git 忽略**: `/media/data/venv` 不在仓库内，无需 gitignore；但禁止提交任何 venv 副本。

### 4.2 状态维护

- **TODO.md (Root)**: 维护待澄清、待改进及用户反馈的事项（本地文件，不提交 Git）。
- **代码质量**: 工业级异常处理、完整类型注解（mypy strict）、关键模块 Docstrings。

### 4.3 项目结构

```
r_proxy/
├── __init__.py    # 包版本
├── cli.py         # 命令行入口 (r-proxy)
├── handler.py     # HTTP/CONNECT 请求处理
└── server.py      # 服务器生命周期
tests/             # 单元测试
```

---

## 5. 代理开发准则 (Proxy Development)

### 5.1 协议支持

| 方法 | 说明 | 实现位置 |
|------|------|----------|
| HTTP | GET/POST 等正向代理 | `handler._handle_http` |
| CONNECT | HTTPS 隧道 | `handler._handle_connect` |

### 5.2 路由决策优先级

```
1. 规则命中（rules.db 规则表） → 强制使用指定出口，跳过后续步骤
2. manual 粘性映射             → 手动绑定的出口
3. auto 粘性映射               → 上次成功的出口
4. 出口优先级链                → priority 升序，同优先级组内轮询
```

出口优先级语义：**数字越小优先级越高**，默认值 `100`。

规则是一张有序表，**首匹配胜出**，只匹配主机名（HTTP 与 CONNECT 一致），位置形如 `rules[3]`。规则只经 Web 界面维护，没有规则文件（[RULES_CONFIG.md](docs/requirements/RULES_CONFIG.md) v2.0.0）。

### 5.2b 切换判据

依次通过三道判据，任一否决即不切换：

1. **失败层次**：传输层失败（超时/RST/DNS）总是可切换；收到 HTTP 响应则继续判断
2. **状态码与来源**：`404`/`500` 等证明目标已处理的响应绝不切换；Cloudflare `520`–`526` 证明出口是通的，既不切换也不计出口失败；`502/503/504` 需判定来源（CONNECT 的非 2xx 必然来自代理；普通请求看 `X-Squid-Error`、`Server` 头）
3. **幂等性与频率**：非幂等方法（POST/PATCH）在已发出后**不得重试**；状态码触发的切换受每 host 频率限制
4. **字节可重放性**：请求体超过 `switch_buffer_bytes`（默认 64KB）已流式转发后不可切换；CONNECT 在回复 `200` 之前收到的客户端字节必须缓存并在新隧道上重放

规则强制路由**失败不切换**，原样返回错误，且既不读也不写粘性映射。

失败必须归类：`upstream_error`（代理本身不可用）计入全局熔断；`route_error`（经该出口到不了此目标）只记 `(host, upstream)` 负面记忆。**`direct` 永不参与全局熔断**，否则被墙站点会拖垮内网访问。

候选链耗尽时给客户端的 `502` 只含通用描述与 `request_id`，不回显出口名称、地址或失败原因——客户端不可信，可借此探测内网拓扑。

### 5.2c 地址族（IPv4 / IPv6）

入向与出向不对称：**客户端全为 IPv4，目标可能是纯 IPv6**。r-proxy 因此是应用层 IPv4→IPv6 网关。

- `listen.host` / `webui.host` 仅接受 IPv4，配 IPv6 地址即启动失败（双栈监听的 `IPV6_V6ONLY` 平台差异服务于不存在的场景）
- 出向必须能连 IPv6 目标
- **关键前提**：只有 `direct` 由我们自己 `getaddrinfo`、能确定知道目标地址族；经上级代理时目标由上级解析，我们看不到地址族。因此**不提供** `upstreams[].supports_ipv6`——对域名目标无法归因，手填也无法验证
- 本机无 IPv6 能力时，纯 IPv6 目标在候选链构造阶段就跳过 `direct`，不发起连接也不写负面记忆；这类失败是结构性的、确定性的，记住它只会白占 LRU 容量并掩盖真实故障
- Happy Eyeballs 用 `loop.create_connection(happy_eyeballs_delay=0.25)`（Python 3.8+ 自带），仅对 `direct` 生效。它解决的是「有全局 IPv6 地址但路径不通」的超时场景；完全无 IPv6 时 OS 已快速失败并把 IPv4 排前
- 无方括号的 IPv6 目标返回 `400`，不猜端口边界

### 5.3 存储与并发约束

- 热路径**禁止**同步数据库 I/O；路由决策只读内存状态。
- 所有数据库写入经唯一写者线程批量落盘，Web 界面不得直连写库。
- 计数器用 SQL 侧自增 `SET c = c + 1`，禁止 Python 侧读改写（deferred 事务并发下实测丢失 75% 更新）。
- 粘性 UPSERT 必须带 `WHERE source != 'manual'`，防止覆盖手动绑定。
- 拆分 `state.db`（不可丢失）与 `logs.db`（可丢弃）。
- **Web 进程数固定为 1**：`uvicorn workers > 1` 会 fork 出第二个写者，摧毁单一写者与内存权威。
- Web 侧 SQLite 查询必须经 `await asyncio.to_thread(...)`；`sqlite3` 是同步库，直接调用会卡住整个事件循环。
- 所有可增长资源必须有上限并定义超限行为：客户端连接数、单出口连接数、重放缓冲、LRU 容量、写队列。
- 实测依据见 [study/sqlite-storage-benchmark.md](study/sqlite-storage-benchmark.md)。

### 5.4 安全要求

- 不向日志输出请求体或 Authorization / Proxy-Authorization 头内容。
- 代理默认监听 `127.0.0.1:6060`，Web 界面默认 `127.0.0.1:6061`。
- Web 界面绑定非回环地址时必须配置 `auth_token`，否则拒绝启动。
- token 用 `secrets.compare_digest` 常量时间比较；日志与审计中只记哈希前 8 位，不记明文。
- 启用认证后 `/api/*` 全部需要 token，只读接口也不例外（请求日志含完整 URL，出口列表含内网地址）。
- Web 前端渲染请求日志一律转义（host、URL 来自不可信流量，存在 XSS 风险）。
- 规则接口不接受任何文件标识或路径参数（`/api/rules` 整表读写，路径穿越面已随规则入库消失）；备份恢复仍走白名单等值查找。
- API 不返回上级代理凭据明文，仅返回 `has_auth` 标记。
- 配置写入记入 `config_audit`，敏感字段值替换为 `***`。
- 不实现透明代理或 MITM 证书注入（超出当前范围）。

### 5.5 扩展方向

- 上级代理认证
- SOCKS5 支持
- 多用户账号与权限体系

---

## 6. 主动分析与决策建议 (Proactive Analysis)

在完成每个用户指令后，执行以下操作：

1. **下一步推演**: 分析当前任务对代理系统的影响，推演下一步最紧迫的任务。
2. **状态审计**: 检查 `TODO.md`，识别已完成项、新生成的待办项或潜在风险。
3. **主动建议**: 基于专业工程判断，提出后续行动建议（含具体任务、技术难点、优化方向）。
