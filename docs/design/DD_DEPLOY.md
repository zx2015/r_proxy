# DD_DEPLOY.md - 容器部署详细设计

| 版本号 | 日期 | 变更说明 | 作者 |
| :--- | :--- | :--- | :--- |
| v1.0.0 | 2026-08-16 | 初始版本：Dockerfile、docker-compose、host 网络选型、卷布局与迁移流程、验证记录 | Agent |
| v1.1.0 | 2026-08-16 | §9.1 计数翻倍缺陷已修复：`HealthPersister` 增加 `initial` 基线播种，补充修复理由、测试与端到端验证结果 | Agent |
| v1.3.0 | 2026-08-17 | 新增 §10：第二个实例部署于 `host-a.example.internal`（Fedora 44 + Podman 5.8.2 + Quadlet，无 Docker）。记录既有代理链拓扑、与 Compose 部署的六项差异（`--format docker` 为硬要求，否则 HEALTHCHECK 被静默丢弃）、无防火墙与 SIEM 监控两项风险、验证记录 | Agent |
| v1.4.0 | 2026-08-17 | `host-a.example.internal` 同日由 Podman + Quadlet 改为 Docker CE + Compose（用户要求），数据目录改到独立数据盘 `/var/lib/r-proxy`。§10 重写：Docker CE 安装、data-root 迁移、企业代理需为 dockerd 单独配置（不继承 shell 环境）、Docker CE 29.7.2 未将 `FORWARD` 改为 `DROP`（与旧版本常见认知不同，已实测验证现役服务不受影响）。新增 §10.5：`docker kill <signal>` 会使 dockerd 放弃该容器的 restart-manager，之后即便真实崩溃也不会自动重启，必须改用宿主侧对真实 PID 发信号；原 Podman + Quadlet 方案存档至 §10.7 | Agent |
| v1.5.0 | 2026-08-17 | `host-a.example.internal` 当天再改：由 `network_mode: host` 切换为 `bridge` + 显式 `ports:` 映射（新增 §10.2b）。起因是该机器没装 firewalld，§2 选 host 模式的前提（保护 firewalld INPUT 规则）在此不成立，而 host 模式下 `docker compose ps` 端口列永远为空、易被误判为「未开放」。已实测确认端口发布下代理与 Web 功能连续多轮无回归，代价是请求日志的客户端来源变为 Docker 网桥地址 | Agent |
| v1.6.0 | 2026-08-17 | 新增 §11：定位到 host-a.example.internal 外部不可达的根因是 OpenStack 安全组未放行 `6060`/`6061`/`10000–12000`（追加端口范围后仍不可达，证明是安全组源地址范围问题而非端口号），改为放弃该主机、迁移到已验证外网直通的 `host-b.example.internal`，端口复用其已放行的 `8080`/`8081`（`8081` 原被 `cp_checklist_frontend` 占用，经用户确认后停用该服务腾出端口）。完整迁移 `config.toml` 与三个数据库文件（规则、粘性映射不在配置文件里，只搬配置会丢规则）。记录两处新踩坑：构建缺 `README.md`、迁移后的配置/数据文件属主为 root 导致容器内非 root 用户读写被拒。经 `verify-host` 公网真机验证代理转发与 Web 鉴权均正常，`host-a.example.internal` 上容器与镜像已删除，原有其他代理服务不受影响 | Agent |
| v1.7.0 | 2026-08-18 | 新增 §11.8：定位新增上级代理报 500 的根因是 §11.5 的属主修复只 chown 了 `config.toml` 文件、没 chown `config/` 目录本身（偏离了 §10.3 早已写好的「目录与文件一起 chown」步骤），导致 Web 界面原子写配置时无法在目录里建临时文件。诊断中顺带发现并修复代码级安全缺陷：`config_writer.py` 的原子写会把 `config.toml`（含明文 `auth_token`）权限从 `600` 重置为 `644`，配置备份文件同样受影响；已修复为写入即 `fchmod`/`chmod` 到 `600`，补两条回归测试，`pytest`/`ruff`/`mypy` 全绿。记录重建镜像时 BuildKit 内部 DNS 解析稳定失败、`DOCKER_BUILDKIT=0` 退回传统 builder 可绕过的现象 | Agent |
| v1.8.0 | 2026-09-30 | 代码评审提醒：§2.1 补「反向代理会让认证失败限流失效」的警告——`AuthThrottle` 按 `request.client.host` 计键且刻意不看 `X-Forwarded-For`，若在 Web 界面前套反向代理做 TLS 终止，所有请求会共享同一个（反向代理的）源 IP，导致限流对攻击者失效、对合法用户误伤；给出两个部署侧规避方向，并明确指出不能靠信任 `X-Forwarded-For` 来"修复" | Agent |
| v1.2.0 | 2026-08-16 | 新增 §2.1：Web 界面改绑 `0.0.0.0` 并对 `192.0.2.0/24` 开放，token 与文件权限约定、实际暴露面为两个网段；新增 §9.2：`request_log` 生产端缺失 | Agent |

本文描述 r-proxy 的容器化部署方案。相关设计见 [DD_CONFIG.md](./DD_CONFIG.md)（配置加载与路径解析）、[DD_STORAGE.md](./DD_STORAGE.md)（三库布局与单一写者）、[DD_WEB.md](./DD_WEB.md)（Web 认证与绑定约束）。

---

## 1. 目标与约束

| 目标 | 判定标准 |
|------|----------|
| 一条命令完成部署 | `docker compose up -d --build` |
| 配置与数据落在宿主 | 容器与镜像整体删除后重建，配置、规则、粘性映射、日志全部保留 |
| 不削弱现有安全边界 | 6060 的源网段访问控制继续由 firewalld 承担 |
| 不改动代理核心代码 | 容器化只通过配置文件与环境变量接入 |

约束来自项目既有红线：

- Web 界面绑定非回环地址时**必须**配置 `auth_token`，否则启动即失败（[DD_WEB](./DD_WEB.md) 安全要求）。
- 数据库写入必须只有一个写者进程，因此**一个数据目录只能挂给一个容器**。
- `webui.workers` 恒为 1，容器编排层也不得横向扩副本（`deploy.replicas` 必须保持 1）。

---

## 2. 网络模式选型

这是整个方案里唯一有实质分歧的决策，先给结论：**用 `network_mode: host`**。

| 方案 | 访问控制 | 客户端 IP | Web 绑定 | 结论 |
|------|----------|-----------|----------|------|
| **host 网络** | firewalld 的源网段 rich rule 继续生效 | 保留真实客户端 IP | 绑定地址与裸跑时同义，`127.0.0.1` 就是「仅宿主可访问」 | **采用** |
| bridge + 端口发布 | 发布端口走 nat/PREROUTING 的 DNAT，不经过 filter/INPUT，现有 rich rule 全部失效 | 全部变成网关地址 | 必须绑 `0.0.0.0` 并配 token | 否决 |

决定性的一条是访问控制。宿主上 6060 的放行规则是：

```
rule family="ipv4" source address="192.0.2.0/24"   port port="6060" protocol="tcp" accept
rule family="ipv4" source address="198.51.100.0/24" port port="6060" protocol="tcp" accept
```

这些规则挂在 INPUT 链上，而 Docker 的端口发布在 PREROUTING 就把包 DNAT 到容器，INPUT 上的判断根本不会执行。换成 bridge 模式后，一个本意只对两个内网段开放的正向代理会变成对全网开放——这不是「再补一条 DOCKER-USER 规则」能等价挽回的，它把访问控制从一处集中配置拆成了两套需要同步维护的规则。

host 网络的代价是失去网络命名空间隔离，且只在 Linux 上可用。对一台自用的 Linux 网关来说，两者都不构成实际损失。

副作用：host 模式下 `ports:` 声明无效（写了也不生效），`EXPOSE` 仅剩文档意义；容器内的 `127.0.0.1` 就是宿主的回环，所以 Web 界面绑回环即「只有宿主本机可访问」，与裸跑时的语义完全一致。

### 2.1 Web 界面对内网开放（2026-08-16 变更）

管理界面改为绑 `0.0.0.0:6061`，并新增一条 firewalld 规则让 `192.0.2.0/24` 可访问：

```
rule family="ipv4" source address="192.0.2.0/24" port port="6061" protocol="tcp" accept
```

三点必须记住：

1. **绑定地址不是访问控制**。`0.0.0.0` 只表示「监听所有接口」，谁能连进来完全由 firewalld 决定。两者职责分离，改一个不会自动收紧另一个。
2. **`auth_token` 从可选变成必填**。非回环绑定时缺 token 直接拒绝启动（`E_WEB_TOKEN_REQUIRED`），这是设计好的兜底，不要试图绕过。token 存在 `/var/lib/r-proxy-docker/config/config.toml`（**在 git 仓库之外**），文件权限 `600`。**不要**把 token 写进 `docker-compose.yml`——那个文件在仓库里，会被提交。
3. **`198.51.100.0/24` 也一并获得了访问权**。该网段有一条全端口放行规则（`source address="198.51.100.0/24" accept`），先于端口级规则生效。也就是说这次变更的实际暴露面是**两个网段**而不是一个。

安全边界：界面走明文 HTTP，token 以 `Authorization: Bearer` 头传输，同网段内可被嗅探。这在受控内网可以接受；若日后网段内出现不可信设备，应改为经反向代理套 TLS，而不是仅靠换更长的 token。

**警告：反向代理会让认证失败限流失效。** `AuthThrottle`（[DD_WEB §7.1](./DD_WEB.md)）按 `request.client.host` 计限流键，且刻意**不看** `X-Forwarded-For`——采信它等于让攻击者用一个伪造头绕开失败限流。如果按上一段的建议在 Web 界面前面套一层反向代理做 TLS 终止，`request.client.host` 看到的会是反向代理自身的地址（通常是同一个内网 IP），后果双向：攻击者可以对同一个 token 无限次爆破而不触发限流；同时任何经这台代理转发的正常请求都会共享同一个限流阈值，一次爆破会连带把所有走该代理的合法用户一起挡在外面。这不是代码缺陷，而是「按客户端 IP 限流」这个方案本身对部署拓扑的假设——它假设 Web 界面直接暴露给客户端，看到的是真实源地址。

若确实需要在 Web 界面前面加反向代理（做 TLS 终止或聚合入口），二选一：

1. **直接暴露 + 自行做 TLS**：不经反向代理，接受「同网段可嗅探明文」在受控内网的既有取舍（本节上一段），保留 `AuthThrottle` 的有效性。
2. **反向代理做 TLS，但把访问控制收窄到反向代理本身**：只信任反向代理的源 IP 访问 6061（防火墙层面），且反向代理与 r-proxy 之间的这一跳网络本身必须可信（同宿主或专用内网段），此时限流失效的风险由「谁能连到反向代理」这一道更早的关卡兜底，而不是 `AuthThrottle`。

**不要**试图让 r-proxy 信任 `X-Forwarded-For` 来"修复"这个问题——转发头由请求方决定内容，客户端能随意伪造成任意 IP，采信它比不限流更糟：攻击者只需要在每次爆破请求里换一个伪造的 `X-Forwarded-For` 值，就能让限流形同虚设，且反而会把无辜的伪造 IP 拖进限流名单。

---

## 3. 镜像

`Dockerfile` 单阶段，基于 `python:3.12-slim`。

不做多阶段构建：运行时依赖只有 `fastapi`、`uvicorn`、`tomlkit` 及其传递依赖，全是纯 Python wheel，没有编译链需要剥离；`--no-cache-dir` 已经避免了 pip 缓存进层。多阶段在这里只增加 Dockerfile 的阅读成本。

关键决策：

| 决策 | 理由 |
|------|------|
| 固定 UID/GID `10001` | 宿主数据目录按同一数字授权。若让系统分配，换基础镜像或重建后 UID 可能变化，表现为容器起来后 `sqlite3.OperationalError: unable to open database file` |
| 非 root 运行 | host 网络下 6060/6061 均 > 1024，不需要 `CAP_NET_BIND_SERVICE` |
| 只 COPY `pyproject.toml`、`README.md`、`r_proxy/` | 镜像不含 `tests/`、`docs/`、`study/`；`.dockerignore` 进一步把 `.git/`、各类缓存、`*.db` 挡在构建上下文之外，避免把宿主的数据库误打进镜像 |
| `HEALTHCHECK` 用标准库 socket 探 6060 | 不依赖 Web 是否启用，也不必为探活往镜像里装 curl |
| `CMD ["r-proxy"]`（exec 形式） | 进程本体即 PID 1，`docker stop` 的 SIGTERM 直接命中 `cli._serve` 已注册的优雅退出；若用 shell 形式，PID 1 会是 `/bin/sh`，信号收不到，最终被 SIGKILL 强杀，写队列里未落盘的数据丢失 |

镜像内置 `R_PROXY_CONFIG=/config/config.toml`。该变量由 `cli.resolve_config_path` 读取（[DD_CONFIG](./DD_CONFIG.md)），因此容器里不需要给 `r-proxy` 传任何参数。

---

## 4. 卷布局

```
/var/lib/r-proxy-docker/          属主 10001:10001
├── config/                          → 容器 /config
│   └── config.toml
└── data/                            → 容器 /data
    ├── state.db                     粘性映射 + 负面记忆 + 出口计数（不可丢失）
    ├── logs.db                      请求日志与审计（可丢弃）
    ├── rules.db                     路由规则（不可丢失）
    └── backups/                     配置文本快照
```

拆成两个挂载点而不是一个：配置是人写的、需要备份与版本管理，数据是程序写的、体积会增长且随时可重建（`logs.db` 部分）。分开之后也便于日后把 `/data` 单独放到别的盘。

`backups/` 目录不需要单独配置：`ConfigWriter.backup_dir` 取 `database.state_path` 的父目录（见 `r_proxy/web/config_writer.py`），`state_path = /data/state.db` 即意味着备份落在 `/data/backups`。

容器内的 `config.toml` 与裸跑版本只差数据库路径：

```toml
[database]
state_path = "/data/state.db"
logs_path  = "/data/logs.db"
rules_path = "/data/rules.db"
```

`listen.host = "0.0.0.0"` 原样保留——host 网络下它的含义与裸跑时相同。`webui.host` 初期沿用 `127.0.0.1`，2026-08-16 改为 `0.0.0.0` 并配 `auth_token`（见 §2.1）。

配置文件权限为 `600`、属主 `10001`：它现在含有 Web 认证凭据，不能再是默认的 `644`。

---

## 5. 编排

`docker-compose.yml` 中除卷与网络外，还有三项不是默认值的设置：

| 设置 | 值 | 理由 |
|------|-----|------|
| `ulimits.nofile` | 65535 | 容器默认软限 1024，低于 `limits.max_client_connections` 所需余量，启动时报 `W_NOFILE_LOW`，高并发下表现为 accept 失败 |
| `stop_grace_period` | 20s | 默认 10s 在写队列积压时可能不够，进程被 SIGKILL 会丢掉尚未落盘的批次 |
| `logging` | json-file，10MB × 3 | 容器日志默认无上限，长期运行会吃满磁盘。这是继 `retention_days` / `max_log_rows` 之后第三处需要设上限的可增长资源 |

`restart: unless-stopped` 而非 `always`：手工 `docker compose stop` 之后不希望宿主重启把它又拉起来。

---

## 6. 常用操作

```bash
cd /media/data/git/r_proxy

docker compose up -d --build     # 首次部署 / 代码更新后重新部署
docker compose logs -f           # 跟踪日志
docker compose ps                # 含健康检查状态
docker compose restart           # 重启
docker compose down              # 停止并删除容器（数据在宿主，不受影响）

docker kill -s HUP r-proxy       # 热重载配置，不断开现有连接
```

宿主上直接编辑 `/var/lib/r-proxy-docker/config/config.toml` 后发 SIGHUP 即可生效；经 Web 界面改配置则由程序自己完成重载。

---

## 7. 从裸跑迁移

顺序不能颠倒——复制数据库必须在进程退出之后：SQLite 处于 WAL 模式，运行中的 `-wal` 与主库是两份需要一起解释的状态，热复制得到的快照不一定自洽。

```bash
kill -TERM <pid>                                   # 1. 优雅停止，等待端口释放
tar czf /var/backups/r-proxy-$(date +%F).tar.gz \
    -C /root .r-proxy .config/r-proxy              # 2. 退出后再备份，快照才一致
mkdir -p /var/lib/r-proxy-docker/{config,data}
cp -a /var/lib/r-proxy/. /var/lib/r-proxy-docker/data/   # 3. 含 backups/ 与残留 WAL
# 4. 写 /var/lib/r-proxy-docker/config/config.toml，数据库路径改为 /data/*.db
chown -R 10001:10001 /var/lib/r-proxy-docker    # 5. 交给容器内的 rproxy 用户
docker compose run --rm r-proxy r-proxy --check    # 6. 先校验配置再正式起
docker compose up -d --build
```

第 6 步值得单独做一次：`--check` 不绑定端口，配置有错时得到的是明确的 `E_*` 错误码，而不是容器起不来后去翻日志。

---

## 8. 验证记录（2026-08-16）

部署后执行的验证及结果：

| 项目 | 方法 | 结果 |
|------|------|------|
| HTTP 正向代理 | `curl -x http://127.0.0.1:6060 http://www.baidu.com/` | 200 |
| CONNECT 隧道 | `curl -x http://127.0.0.1:6060 https://www.baidu.com/` | 200 |
| Web 界面 | `curl http://127.0.0.1:6061/` | 200 |
| 健康检查 | `docker inspect --format '{{.State.Health.Status}}' r-proxy` | healthy |
| 数据回填 | 启动日志 | 回填粘性 349 条、出口计数 3 项 |
| 时区 | 日志时间戳 | 与宿主一致（挂载 `/etc/localtime`） |
| **持久化** | 写入标记规则 → `docker compose down` → 删除镜像 → `up -d --build` | 规则修订号 29 与标记规则均保留，粘性映射与历史计数完好 |

持久化验证特意删掉了镜像本身而不只是容器：只重建容器无法区分「数据在卷里」与「数据在容器可写层里」，删镜像重建才能证明状态确实全部落在宿主目录。

---

## 9. 缺陷记录

### 9.1 重启导致出口累计计数翻倍（既有缺陷，非容器化引入；2026-08-16 已修复）

持久化验证时发现：`/api/status` 的 `requests.attempts` 在一次重启后从 3085 变成 6167，几乎正好翻倍。

成因在 `r_proxy/persistence.py`：`HealthPersister._last` 是「上次已落盘的累计值」基线，进程启动时为空字典；而 `apply_initial_state` 已经把数据库里的历史累计值回填进了内存健康表。于是首次 flush 算出的增量是 `内存总量 - 0`，即整个历史总量，而 SQL 侧是 `total_success = total_success + excluded.total_success` 自增——历史值被再加了一遍。

```python
# r_proxy/persistence.py，HealthPersister.flush
success, failure = self._last.get(health.name, (0, 0))   # 启动后基线为 0
delta = (health.total_success - success, ...)            # delta == 历史总量
```

`HealthPersister` 的类文档恰好警告了同一类错误（「把覆盖写成自增，计数会翻倍」），但漏掉了启动基线这条路径。

影响范围仅限 Web 展示的累计计数与成功率，不影响路由决策（决策只看 `consecutive_failures` 与熔断状态，两者都是覆盖写）。裸跑时同样存在，只是重启频率低不易察觉；容器化让重启变得廉价，问题随之显性化。

**修复**：`HealthPersister.__init__` 新增 `initial: InitialState | None` 参数，用启动回填的同一批数值播种 `_last`，首次 flush 的增量因此为 0。

播种取**库里**的值而非内存快照，这一点不能省：从 `server.start()` 到首次 flush 之间完成的请求是真实的新增量，若用内存快照当基线，这些请求会被静默吞掉——把一个多记的 bug 换成一个少记的 bug。

基线设成构造参数而不是事后调用的 `seed()` 方法，是为了让唯一的生产构造点（`app.start()`）必须正面回答「基线是什么」；`seed()` 可以忘记调用，而忘记正是这个缺陷的成因。

配套测试见 `tests/test_persistence.py::TestHealthPersister`：回填后首次 flush 不入队、只写重启后的增量、已删出口的基线会被裁掉。

容器上的端到端验证：记录重启前内存值 12341 → `docker compose restart` → 回填值仍为 12341（未修复时应为约 24682），且重启前发出的 5 次请求确实落盘。

遗留：库里现存的累计值是历次重启翻倍后的结果（真实量级约 3000，现为 12341），已失去参考意义。修复只保证今后不再翻倍，不追溯纠正历史值；需要干净基线可在 Web 仪表盘上逐出口「重置」。

---

### 9.2 `request_log` 从未被写入（2026-08-16 已修复）

清空统计数据时发现 `request_log` 表是空的，且在其后的成功请求之后依然为空。

这不是清空动作的副作用，也不是队列丢弃：整个代码库里**没有任何地方构造 `OpKind.REQUEST_LOG` 的写操作**。周边设施全都在——

| 环节 | 状态 |
|------|------|
| 表与索引（`storage/schema.py`） | 已建 |
| INSERT 语句（`storage/writer.py`） | 已写 |
| 队列规格与优先级（`storage/queue.py`，`Priority.LOSSY`） | 已定义 |
| 保留期清理（`storage/retention.py`） | 已实现 |
| Web 查询层（`web/queries.py`） | 已实现 |
| Web 请求日志页面 | 已实现 |
| **生产端：代理路径上的入队调用** | **缺失** |

`storage/queue.py` 里有 `sticky_upsert`、`health_counters`、`config_audit` 等工厂函数，唯独没有 `request_log` 的对应物。

后果：Web 界面的「请求日志」页永久为空，`decision_source`、`rule_origin`、`keep_reason` 这些为可观测性专门设计的字段全部无处可看——而它们正是排查「为什么这个请求走了这个出口」的唯一手段。

之所以一直没被发现，是因为存储层与 Web 层的测试各自构造 `WriteOp` 直接插库，两侧都测得过；缺的是一条端到端断言「一次代理请求会产生 N 条 `request_log`」。**分层测试各自为政时，层与层之间没接上的线不会被任何一层的测试照到。**

**修复**：`storage/queue.py` 补上 `request_log()` 工厂，`AttemptExecutor` 在每次尝试结束处入队一行，`protocol/connection.py` 把 `request_id` 传进 `execute()`。设计细节见 [DD_STORAGE §4.9](./DD_STORAGE.md)。

端到端验证（容器上的真实流量）：

```
{'request_id': '22c7be8af591f34d', 'host': 'www.baidu.com', 'method': 'CONNECT',
 'upstream_name': 'z83-3128', 'attempt_index': 0, 'decision_source': 'rule',
 'rule_origin': 'rules[0]', 'http_status': 200}
```

补的测试分两层，缺一不可：`tests/test_egress_executor.py::TestRequestLog` 验字段取值，`tests/test_protocol_server.py::TestRequestLogging` 验**接线本身**——后者用真实套接字发请求，断言 sink 上真的出现了这一行。已做变异验证：抽掉 `connection.py` 里的三处 `request_id=` 传参，四条端到端测试全部失败。

遗留 `bytes_up` / `bytes_down` 恒为 0，见 [DD_STORAGE §4.9](./DD_STORAGE.md) 的说明。

---

## 10. 第二个实例：host-a.example.internal（Docker CE + Compose，2026-08-17）

第一个实例在 `192.0.2.100`（Docker Compose，§5）。第二个部署在 `host-a.example.internal`（`10.182.67.191`，同时也是 `192.0.2.33`）——企业网现役代理服务器。

最初按 Podman + Quadlet 部署并验证通过（原始记录存档于 §10.7），同日应用户要求改为 Docker CE + Compose，数据目录也从根分区下的 `/opt/r-proxy` 迁到独立数据盘 `/var/lib/r-proxy`。本节描述改造后的最终形态。

### 10.1 目标主机与既有拓扑

Fedora 44 Cloud Edition，x86_64，7.7 GiB 内存。根分区 `/dev/vda4`（btrfs）45 GiB 可用；另有独立数据盘 `/dev/vdb`（ext4，120 GiB，挂载于 `/media/data`，装 Docker 前剩 60 GiB）——数据与镜像层都落在这块盘，不占根分区。

这台机器上已有一条完整代理链，r-proxy 是加在它们前面的智能前端，**不改动任何既有服务**：

| 服务 | 监听 | 作用 |
|------|------|------|
| privoxy | `0.0.0.0:8080` | 企业客户端当前使用的入口，转发给 `127.0.0.1:3127` |
| haproxy | `0.0.0.0:3128` | 轮询三条 ssh 隧道 `127.0.0.1:4128/4129/4130` |
| squid | `*:3126`、`*:3127` | 缓存代理实例 |
| xray | `*:8081` | 代理 |
| litellm | `0.0.0.0:4000` | 与代理无关 |
| openconnect | `tun0` = `198.51.100.132` | VPN，通向 `198.51.100.0/24` |

端口方案与 `192.0.2.100` 配置里指向的 z83（`192.0.2.200`）**完全一致**：`3128` 是隧道均衡器、`8081` 是 xray。因此本机可选的上级出口就是 `127.0.0.1:3128` 与 `127.0.0.1:8081`，与 `z83-3128` / `z83-us-cloud` 角色一一对应。

`haproxy:3128` 在装 Docker **之前**就已探测失败（2.7ms 返回连接失败，likely 三条 ssh 隧道均未建立），这是既有现象，与本次部署无关，写入配置前需要先确认该出口是否可用。

### 10.2 与 §5 Compose 部署的差异

| 项 | `192.0.2.100` | `host-a.example.internal` |
|---|---|---|
| Docker 来源 | 已预装 | 新装，`dnf config-manager --add-repo docker-ce.repo` + `dnf install docker-ce docker-ce-cli containerd.io docker-compose-plugin`，Fedora 44 官方仓库已有 `docker-ce-29.7.2-1.fc44` |
| `data-root` | 默认 `/var/lib/docker` | 改到 `/var/lib/docker-data`（`/etc/docker/daemon.json`），根分区只有 45 GiB，镜像层与容器可写层不能挤占 |
| 出网代理 | 主机直连外网，dockerd 不需要代理 | **dockerd 必须单独配置代理**，见下方说明 |
| 卷路径 | `/var/lib/r-proxy-docker/{config,data}` | `/var/lib/r-proxy/{config,data,src}` |
| 客户端访问控制 | firewalld 源网段 rich rule | **没有**，见 §10.4 |
| `FORWARD` 链策略 | 未特别验证 | 已实测：Docker CE 29.7.2 **未**将其改为 `DROP`，见下方说明 |
| 网络模式 | `network_mode: host` | **`bridge` + 显式 `ports:` 映射**（2026-08-17 由 host 改过来，见 §10.2b），`docker compose ps` 能看到 `0.0.0.0:6060-6061->6060-6061/tcp` |

**dockerd 不继承 shell 的代理环境变量。** 这台机器所有出网（包括 `docker build` 拉取 `python:3.12-slim`）都必须经 `192.0.2.33:8080`（本机的 privoxy）。之前的 Podman 部署能直接拉镜像，是因为 `podman build` 在当前 shell 里执行、继承了已 `export` 的 `http_proxy`；而 `dockerd` 是独立 systemd 服务，不读用户 shell 的环境变量，必须显式配置两处：

```jsonc
// /etc/docker/daemon.json —— 守护进程侧，影响 docker pull / build 时的基础镜像拉取
{
  "data-root": "/var/lib/docker-data",
  "proxies": {
    "http-proxy": "http://192.0.2.33:8080",
    "https-proxy": "http://192.0.2.33:8080",
    "no-proxy": "localhost,127.0.0.1,::1,192.0.2.0/16,198.51.100.0/24,10.182.67.0/24,*.local"
  }
}
```

```jsonc
// /root/.docker/config.json —— CLI 侧，构建参数里的默认代理（供 pip 等安装步骤使用）
{
  "proxies": {
    "default": {
      "httpProxy": "http://192.0.2.33:8080",
      "httpsProxy": "http://192.0.2.33:8080",
      "noProxy": "localhost,127.0.0.1,::1,192.0.2.0/16,198.51.100.0/24,10.182.67.0/24,*.local"
    }
  }
}
```

第一处不配，`docker compose up -d --build` 在拉 `python:3.12-slim` 时会直接 `connection reset by peer`（企业出口对未经代理的直连一律 RST）。改完 `daemon.json` 必须 `systemctl restart docker` 才生效。

**`FORWARD` 链未被 Docker 改成 `DROP`。** 常见认知里 Docker 会把 `iptables FORWARD` 默认策略改为 `DROP` 再自建规则放行容器流量，这是旧版本的行为；实测 Docker CE 29.7.2 装完后 `FORWARD` 策略仍是 `ACCEPT`（改用独立的 `DOCKER-FORWARD` 链插入规则），iptables 总规则数从 7 条增至 34 条。装前装后对 privoxy/haproxy/squid/xray/openconnect 全部服务做了功能复验，结果与基线一致，仅 `haproxy:3128` 保持既有的失败状态（见 §10.1）。这台机器用 `network_mode: host`，容器网络本就不经过 `DOCKER-FORWARD`，此项验证更多是确认 Docker 安装本身没有动到宿主网络栈的其它部分。

`/var/lib/docker-data` 目录在装之前就存在（2024-10 至 2025-07 装过又卸载的 Docker 留下的空壳，仅 2.3 MB、`containers/` 为空），说明这台机器把 Docker 数据放数据盘本就是既有惯例，本次直接复用了这个目录。

### 10.2b 网络模式：改用 bridge + 显式端口发布（2026-08-17）

最初照搬 §2 的结论用了 `network_mode: host`。当天验证 Docker 外部可达性时发现两点，导致改用 bridge：

1. `docker compose ps`／`docker port` 在 host 模式下永远不显示端口（这是 Docker 的既定行为，端口就是宿主端口，不经发布机制），运维上极易被误读为「没开放」。
2. **§2 选 host 模式的前提在这台机器上不成立**：那条决策是为了让 firewalld 的源网段 rich rule 继续生效（bridge 模式的端口发布走 `PREROUTING` DNAT，不经过 `INPUT` 链，会绕过 firewalld）。但 host-a.example.internal **压根没装 firewalld**（见 §10.4），没有需要保护的 `INPUT` 规则，host 模式在这里不产生任何访问控制收益。

权衡之后，为了让 `docker compose ps` 如实反映端口状态、避免运维误判，改为 bridge 网络加显式 `ports:` 映射：

```yaml
services:
  r-proxy:
    # 不再写 network_mode: host
    ports:
      - "6060:6060"
      - "6061:6061"
```

**代价（已确认接受）**：`request_log` 里记录的客户端来源会变成 Docker 网桥地址（如 `172.17.0.1`），不再是请求方的真实 IP——这台机器上 r-proxy 本就没有基于客户端 IP 的访问控制（§10.4 已知没有 ACL），所以只影响审计时定位来源，不削弱现有的访问控制能力。

验证：切换后 `docker compose ps` 显示 `0.0.0.0:6060-6061->6060-6061/tcp, [::]:6060-6061->6060-6061/tcp`，宿主监听进程从 `r-proxy` 直接监听变为 `docker-proxy`（Docker 的用户态转发进程）。功能验证连续 3 轮全部 `200`，无回归。`EXPOSE 6060 6061` 早已写在 `Dockerfile` 里，切换时无需改镜像。

`192.0.2.100` 那台继续用 host 模式不变——那台确实装了 firewalld，§2 的决策依据仍然成立。

### 10.3 部署步骤

```bash
# 1. 加官方仓库、装 Docker CE（若走企业代理见下方 daemon.json）
dnf -y install dnf-plugins-core
dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 2. data-root 与代理配置见上一节，之后启动
mkdir -p /etc/docker && vi /etc/docker/daemon.json
mkdir -p /root/.docker && vi /root/.docker/config.json
systemctl enable --now docker

# 3. 宿主数据目录（UID/GID 10001 与 Dockerfile 里的 rproxy 对齐）
mkdir -p /var/lib/r-proxy/{config,data,src}
chown 10001:10001 /var/lib/r-proxy/config /var/lib/r-proxy/data
chmod 750 /var/lib/r-proxy/config /var/lib/r-proxy/data

# 4. 构建上下文（本机打包推过去）
tar czf - Dockerfile pyproject.toml README.md r_proxy \
  | ssh host-a.example.internal 'tar xzf - -C /var/lib/r-proxy/src'

# 5. 配置：权限 600，属主 10001
scp config.toml host-a.example.internal:/var/lib/r-proxy/config/config.toml
ssh host-a.example.internal 'chown 10001:10001 /var/lib/r-proxy/config/config.toml
                 chmod 600 /var/lib/r-proxy/config/config.toml'

# 6. docker-compose.yml（context 指向 src/），一条命令构建并启动
scp docker-compose.yml host-a.example.internal:/var/lib/r-proxy/
ssh host-a.example.internal 'cd /var/lib/r-proxy && docker compose up -d --build'
```

日常操作与 §6 完全一致，**除了热重载命令**——见下一节的陷阱说明，`docker kill -s HUP` 在这台机器上**不能用**。

### 10.4 这台机器的两个额外风险

**没有防火墙兜底。** `firewalld` 未启用，`firewall-cmd` 根本没装。§2.1 里 `192.0.2.100` 的安全模型是「绑 `0.0.0.0` + firewalld 限源网段」，那半边保护在这里不存在。r-proxy 自身没有客户端 ACL，所以 `6060` 对 `192.0.2.0/24` 与 VPN 的 `198.51.100.0/24` 是完全开放的，`6061` 靠 `auth_token` 保护。这是部署时明确确认过的选择，若要收紧，装 firewalld 加源网段 rich rule 即可，容器用 host 网络正是为了让这类规则生效。

**这是台受监控的企业主机。** 跑着 `wazuh-agent` 与 `filebeat-eusiem`（SIEM 采集）。新增监听端口、容器运行时拉镜像都会被记录，必要时需向 IT 报备。

### 10.5 运维陷阱：`docker kill` 会废止容器的自动重启

§6 给出的热重载命令是 `docker kill -s HUP r-proxy`，这在 `192.0.2.100` 上没问题，但在实测中发现一个所有 Docker 部署都存在、容易被忽视的行为：

**dockerd 把任何 `docker kill <container>`（不论信号是什么）都当作「用户主动操作」，会停止该容器的 restart-manager。** 之后即便进程真的崩溃（如 OOM 被内核杀死），容器也**不会**被自动拉起，`unless-stopped` 策略形同虚设，直到下次 `docker compose up -d` 或宿主重启才会恢复。

实测复现（`journalctl -u docker`）：

```
# docker kill -s HUP r-proxy 之后立刻出现：
level=info msg="stopping restart-manager" container=<id>

# 之后模拟真实崩溃（kill -9 容器内真实 PID）：
# —— 容器保持 exited，RestartCount 未变化，服务中断，直到手工 up -d
```

**正确做法：绕开 `docker kill`，直接对宿主侧的容器真实 PID 发信号**，这不会触碰 restart-manager：

```bash
kill -HUP "$(docker inspect r-proxy --format '{{.State.Pid}}')"
```

复验：用这条命令重载后，`journalctl -u docker` 里没有出现 `stopping restart-manager`；之后再模拟真实崩溃（`kill -9` 同一 PID），`RestartCount` 正常自增，20 秒内容器恢复 `healthy` 且代理功能验证通过。

§6 的操作说明与本文档其余提及 `docker kill -s HUP` 之处，在 host-a.example.internal 上一律替换为上述宿主侧 `kill -HUP` 写法；`docker compose restart`（整体重启，非信号）不受此问题影响，可以正常使用。

### 10.6 数据迁移（Podman → Docker，/opt → /media/data）

Podman 容器优雅停止（`systemctl stop r-proxy`，等待写者线程把 WAL 并入主库）之后再复制，与 §7「从裸跑迁移」同样的顺序要求：SQLite 处于 WAL 模式，运行中的 `-wal` 与主库是两份需要一起解释的状态，热复制得到的快照不一定自洽。

```bash
systemctl stop r-proxy && sleep 3         # 1. 优雅停止，等 WAL 并入主库
mkdir -p /var/lib/r-proxy/{config,data}
cp -a /opt/r-proxy/config/config.toml /var/lib/r-proxy/config/
cp -a /opt/r-proxy/data/. /var/lib/r-proxy/data/
chown -R 10001:10001 /var/lib/r-proxy/{config,data}
chmod 750 /var/lib/r-proxy/{config,data}; chmod 600 /var/lib/r-proxy/config/config.toml
# 2. 迁移后校验：三库均可读，request_log 行数与迁移前一致
rm -f /etc/containers/systemd/r-proxy.container && systemctl daemon-reload
podman rm -f r-proxy && podman rmi r-proxy:0.1.0   # 3. 拆除 Podman 侧
```

旧的 `/opt/r-proxy`（含 `src/`、`config/`、`data/`）暂留作回滚备份，未删除。

### 10.7 已弃用：最初的 Podman + Quadlet 方案（2026-08-17 当天替换）

> ⚠️ 已过时：同日应用户要求改为 Docker CE + Compose（见 §10.2–§10.6），以下为原始部署记录存档，`--format docker` 这条经验教训在纯 Docker 场景下不适用（Docker 默认就是 Docker 格式），但对其他仍用 Podman 的场景仍然成立。

最初这台机器**只有 Podman 5.8.2，没有 Docker，也没有任何 compose provider**（`podman compose` 报 `looking up compose provider failed`），因此选择了 Quadlet：

| 项 | `192.0.2.100`（Docker） | host-a.example.internal（Podman，已弃用） |
|---|---|---|
| 编排 | `docker-compose.yml` | Quadlet 单元 `/etc/containers/systemd/r-proxy.container` |
| 镜像格式 | 默认即 Docker 格式 | **必须 `podman build --format docker`** |
| 重启策略 | `restart: unless-stopped` | `[Service] Restart=always` |
| 卷路径 | `/var/lib/r-proxy-docker/{config,data}` | `/opt/r-proxy/{config,data}` |

**`--format docker` 是硬要求**：Podman 默认产出 OCI 格式镜像，而 OCI 规范没有 HEALTHCHECK 字段，构建时只会打一行 warning 就把它丢掉：

```
level=warning msg="HEALTHCHECK is not supported for OCI image format and will be ignored. Must use `docker` format"
```

镜像照样能跑，但 `podman healthcheck run` 无从执行，容器也不再有健康状态——一个只在你去查健康时才发现的静默降级。

验证记录（2026-08-17，Podman 方案，已随拆除失效）：

| 项 | 结果 |
|---|---|
| 启动 | `active (running)`，无 `W_NOFILE_LOW` |
| HTTP 转发 / CONNECT 隧道 | `200` |
| 只有 direct 的预期失败 | `https://www.google.com/` 超时，`request_log` 记 `TimeoutError` |
| 异常拉起 | `podman kill` 后 12 秒内自动重启 |
| 热重载 | `podman kill -s HUP` → 「规则已重载，0 条；配置未变」 |

初始配置只有 `direct` 一个出口，两地网络环境不同，未迁移 `192.0.2.100` 的 41 条规则——那些规则指向 `z83-3128` / `z83-us-cloud`，出口名在这里不存在，启动校验会以 `E_RULE_TARGET` 拒绝启动。这一决策在改用 Docker 后延续。

### 10.8 验证记录（Docker CE 最终形态，2026-08-17）

| 项 | 方法 | 结果 |
|---|---|---|
| Docker 安装 | `docker --version` / `docker compose version` | `29.7.2` / `v5.4.0` |
| data-root | `docker info --format '{{.DockerRootDir}}'` | `/var/lib/docker-data`，落在数据盘 |
| 镜像构建 | `docker compose up -d --build`（配好 daemon 代理后） | 构建成功，`Started` |
| HEALTHCHECK 保留 | `docker inspect --format '{{.Config.Healthcheck.Test}}'` | 完整判据，`健康: healthy` |
| 监听 | `ss -ltnp` | `0.0.0.0:6060`、`0.0.0.0:6061`（bridge 切换后由 `docker-proxy` 监听） |
| 端口映射可见性 | `docker compose ps` | `0.0.0.0:6060-6061->6060-6061/tcp, [::]:6060-6061->6060-6061/tcp` |
| nofile | `docker exec r-proxy sh -c ulimit -n` | `65535` |
| 时区 | `docker exec r-proxy date` | 与宿主一致（`/etc/localtime` 只读挂载） |
| 数据回填 | 启动日志 | 「回填粘性 1 条、负面记忆 0 条、出口计数 1 项」，迁移数据确实被读到 |
| HTTP 转发 | `curl -x http://127.0.0.1:6060 http://www.baidu.com/` | `200` |
| CONNECT 隧道 | `curl -x http://127.0.0.1:6060 https://www.baidu.com/` | `200` |
| 经 VPN 网段访问 | `curl -x http://198.51.100.132:6060 https://www.baidu.com/` | `200` |
| Web API 鉴权 | 不带 token / 带 token 访问 `/api/upstreams` | `401` / `200` |
| 请求日志持久化 | 迁移前 3 行 → 测试后 | `logs.db` 增长为 7 行，无丢失 |
| 安全热重载 | 宿主侧 `kill -HUP <真实PID>` | 「规则已重载，0 条；配置未变」，且不触发 `stopping restart-manager` |
| 真实崩溃自愈 | 宿主侧 `kill -9 <真实PID>` | `RestartCount` 自增，20 秒内恢复 `healthy`，功能复验 `200` |
| 现役服务未受影响 | 装 Docker 前后对比 privoxy/haproxy/squid/xray/openconnect/litellm/隧道 | 全部 `active`，功能与基线一致（`haproxy:3128` 装前即失败，非本次引入） |
| 开机自启 | `systemctl is-enabled docker`、`docker inspect --format '{{.HostConfig.RestartPolicy.Name}}'` | `enabled`、`unless-stopped` |

**未做整机重启验证**：这台机器承载企业网现役代理流量，`reboot` 不在授权范围内。开机自启以 `docker.service` 的 `enabled` 状态 + 容器 `unless-stopped` 策略为依据。

---

## 11. 第三个实例：host-b.example.internal（取代 host-a.example.internal，2026-08-17）

### 11.1 背景：为什么放弃 host-a.example.internal

§10 的部署在应用层完全正常——本机 `curl`、VPN 网段 `curl`、同 OpenStack 项目内的对等机器（`host-b.example.internal`）访问 `6060`/`6061`、`10060`/`10061` 全部成功。但从真正的公网客户端（`verify-host`，公网 IP `203.0.113.10`，经 `wlp3s0` 直连互联网，不经任何内部代理）访问同样端口，全部超时。

逐层排除：

| 层次 | 检查项 | 结论 |
|---|---|---|
| SELinux | `getenforce` | `Disabled`，非阻塞 |
| 主机防火墙 | firewalld 未安装；`iptables -L INPUT` 策略 `ACCEPT` | 非阻塞 |
| Docker 网络 | `FORWARD` 链策略仍为 `ACCEPT`（29.7.2 未像早期版本那样默认改 `DROP`） | 非阻塞 |
| 应用层 | 本机与同项目对等机验证均 `200` | 应用本身工作正常 |
| **OpenStack 安全组** | 只放行 `22`、`8080`（既有 privoxy 入口）；`6060`/`6061` 及后续追加的 `10000–12000` 段，对 `verify-host` 的公网源地址均无匹配规则 | **根因** |

用户按端口范围 `10000–12000` 追加安全组规则后重测，`10060`/`10061` 依旧从 `verify-host` 不可达——证明问题不在端口号选择，而在安全组规则的**源地址（source CIDR）**没有覆盖真正的公网来源，很可能被限定在内网网段而非 `0.0.0.0/0`。反复调整安全组成本已明显超过换一台已验证外网直通的主机，遂改变策略：**放弃在 host-a.example.internal 上排查安全组，改用已确认外网可达的 host-b.example.internal**（用户明确指示）。

### 11.2 为什么是 8080 / 8081

`host-b.example.internal`（`192.0.2.16`，浮动 IP `10.182.67.146`，与 `host-a.example.internal` 同一 OpenStack 项目、同一 `192.0.2.0/24` 网段）此前已确认外网可直连 `8080`。从 `verify-host` 实测：

| 端口 | 结果 | 含义 |
|---|---|---|
| `8080` | `Connection refused` | 安全组放行，只是当时无服务监听——网络层已通 |
| `8081` | 连接成功（`cp_checklist_frontend` 应答） | 安全组放行且有服务在跑 |
| `11012`、`12000` | 超时 | 即便容器已绑定，安全组未放行这两个端口 |

`Connection refused` 与前面 `6060`/`10060`/`10061` 的“超时”性质不同：refused 说明 TCP SYN 已经到达主机、主机内核主动拒绝（无监听方），超时则说明包在安全组这一层就被丢弃。这是判断安全组是否放行的可靠手段。

### 11.3 端口冲突处理：停用 cp_checklist

`8081` 被既有业务容器 `cp_checklist_frontend`（`80→8081`）占用，同项目下还有 `cp_checklist_backend`（`9000`），两者同属 compose 项目 `cp_checklist`（`/var/lib/cp_checklist`）。停用前核实：

- `cp_checklist_frontend` 访问日志里唯一一条记录就是本次探测产生的 `400`，近期无真实用户流量
- `cp_checklist_backend` 的定时同步任务（每 30 分钟一次）每次都因 `Unknown MySQL server host 'ZEUWPJIRA01.nsn-intra.net'`（内网域名解析失败）而失败，功能本身已处于故障状态

经用户明确指示后执行 `docker compose down`（在 `/var/lib/cp_checklist` 目录），只停用该项目的两个容器，**不删除** compose 文件与镜像，可随时 `docker compose up -d` 恢复。停用后确认其余全部业务容器（`mcp-atlassian`、`collab-roster-*`、`llm-gateway`、`redis` 等）未受影响。

### 11.4 数据与配置迁移

`rules.db` 里的 `*.google.com → us-cloud` 规则和 `state.db` 里的粘性映射不在 `config.toml` 里，只搬配置文件会丢规则，因此三个库文件与 `config.toml` 一并迁移（经本地 `/tmp` 中转 `scp`）：

```bash
# host-a.example.internal：优雅停止（等写队列刷盘）→ 校验 → 拆除
docker compose stop && sleep 3
docker compose down
docker rmi r-proxy:0.1.0

# 经本地中转搬运 config.toml + {state,logs,rules}.db 到 host-b.example.internal
```

迁移后编辑 `config.toml`：`listen.port` `10060→8080`、`webui.port` `10061→8081`；四个上级出口地址（`10.158.100.{3,8,9}:8080`、`192.0.2.33:8081`）均为绝对 IP，不因搬家而失效，迁移前逐一用 `/dev/tcp` 探测确认从 `host-b.example.internal` 同样可达（`host-b.example.internal` 与 `host-a.example.internal` 同网段、同 VPN）。

### 11.5 部署踩坑

| 问题 | 现象 | 原因 | 修复 |
|---|---|---|---|
| 构建缺文件 | `COPY pyproject.toml README.md ./` 报 `"/README.md": not found` | 只打包了 `Dockerfile`/`pyproject.toml`/`r_proxy`，漏了 `Dockerfile` 依赖的 `README.md` | 补 `scp README.md` 到构建上下文 |
| 配置文件读取被拒 | 启动日志反复 `无法读取配置文件 ... Permission denied` | `config.toml` 权限 `600` 属主 `root`，容器内以 `uid 10001`（`rproxy`）身份运行，读不了 root 的 600 文件 | `chown 10001:10001 config.toml` |
| 规则库只读 | `读取规则库失败 rules.db: attempt to write a readonly database` | `scp` 中转带过来的 db 文件属主同样是 `root`，容器内用户无写权限 | `chown -R 10001:10001 data/` |

这两处属主问题在 §10 的迁移（同为 root 到 root，未跨用户边界）中不会触发，是本次因数据落地方式不同才暴露的，記入 `experience/` 备查。

### 11.6 验证记录（2026-08-17）

| 项 | 方法 | 结果 |
|---|---|---|
| 防火墙 | `ufw status`、`iptables -L INPUT` | `inactive`、策略 `ACCEPT` |
| 上级出口连通性 | 迁移前 `/dev/tcp` 探测四个上级地址 | 全部 `通` |
| 镜像构建 | `docker compose up -d --build`（直连外网，无需代理） | 成功 |
| 健康检查 | `docker inspect --format '{{.State.Health.Status}}'` | `healthy` |
| 数据回填 | 启动日志 | 「回填粘性 1 条、负面记忆 0 条、出口计数 6 项」 |
| 规则保留 | `curl .../api/rules`（带 token） | `*.google.com → us-cloud` 规则完整 |
| 本机 HTTP/HTTPS 代理 | `curl -x http://127.0.0.1:8080 http(s)://www.baidu.com` | `200` / `200` |
| **外网真机 HTTP/HTTPS 代理** | 从 `verify-host`（公网 `203.0.113.10`）`curl -x http://10.182.67.146:8080` | `200` / `200`（**根因问题已解决**） |
| **外网真机 Web 鉴权** | 从 `verify-host` 不带 token 访问 `/api/health` | `401`（鉴权按预期生效） |
| 其余业务容器未受影响 | 停用 `cp_checklist` 前后对比 `docker ps` | 其余全部容器状态一致 |

### 11.7 host-a.example.internal 现状

容器与镜像已删除（`docker compose down` + `docker rmi`），`/var/lib/r-proxy/{config,data}` 保留在原地作为迁移前的最后备份，未主动清理；该机器上原有的 privoxy/haproxy/squid/xray/openconnect/litellm 等既有服务不受影响，继续运行。

### 11.8 后续故障：新增上级代理报「服务内部错误」（2026-08-18）

#### 现象与根因

用户经 Web 界面新增上级代理时收到 500。容器日志显示：

```
PermissionError: [Errno 13] Permission denied: '/config/config.toml.tmp'
```

根因是 §11.5 的属主修复**只做了一半**：当时只 `chown` 了 `config.toml` 文件本身，没有 `chown` 它所在的 `config/` 目录（目录仍是 `root:root`）。容器内非 root 用户能读写已存在且属于自己的文件，但 Web 界面写配置走「建临时文件 `config.toml.tmp` → `rename` 替换」的原子写模式，建临时文件需要**目录本身**的写权限——这一步被目录的属主拒绝。

§10.3 的部署步骤原本写的就是 `chown 10001:10001 /var/lib/r-proxy/config /var/lib/r-proxy/data`（目录本身与目录里的文件一起覆盖），§11.4 迁移时偏离了这个已有步骤，才第二次踩坑。修复：

```bash
chown 10001:10001 /var/lib/r-proxy/config
```

#### 顺带发现并修复的代码级安全问题

诊断过程中发现 `r_proxy/web/config_writer.py` 的原子写路径本身有缺陷：`_replace()` 用 `open(tmp, "w")` 建临时文件，走 umask 默认权限（通常 `644`）；`os.replace()` 是纯 `rename`，目标文件的最终权限完全继承自 tmp 文件。也就是说**只要通过 Web 界面写过一次配置，`config.toml` 的权限就会从手工设置的 `600` 被重置成 `644`**——而这个文件含明文 `webui.auth_token`。`_write_backup()` 写出的配置备份文件（`data/backups/config-*.toml`）同样受影响，且备份是全文明文快照。

修复（`r_proxy/web/config_writer.py`）：

- `_replace()`：`open()` 拿到 handle 后立即 `os.fchmod(handle.fileno(), 0o600)`，再写内容——顺序上先限权、后落笔，任何时刻文件都不会以宽松权限存在
- `_write_backup()`：`target.write_bytes(data)` 之后补 `os.chmod(target, 0o600)`

新增回归测试 `tests/test_web_config_write.py::TestAtomicWrite::test_the_written_file_keeps_owner_only_permissions` 与 `test_the_backup_file_keeps_owner_only_permissions`，`pytest`/`ruff`/`mypy` 全部通过（1234 项测试）。host-b.example.internal 上现存的历史 `config.toml` 与全部 `data/backups/config-*.toml` 已手工 `chmod 600` 补救。

#### 重新构建时的意外：BuildKit 内部 DNS 解析失败

修复代码后 `docker compose up -d --build` 两次都在 `pip install` 阶段稳定报错：

```
Failed to establish a new connection: [Errno -3] Temporary failure in name resolution
```

但宿主机 `curl -4 https://pypi.org` 直接成功（`200`），`docker run --rm python:3.12-slim getent hosts pypi.org` 也能解析——主机网络与常规容器网络都正常，只有 BuildKit 构建时的网络/DNS 路径失败（该机器 IPv6 出网本身不通，`pypi.org` 的 DNS 记录里只有 AAAA，怀疑与 BuildKit 处理 IPv6-only 解析结果的方式有关，未继续深挖）。绕过方法：

```bash
DOCKER_BUILDKIT=0 docker compose build   # 退回传统 builder，构建成功
docker compose up -d                      # 传统 builder 不接 up --build 里的构建步骤，需分两步
```

这台机器上后续需要重新构建镜像时，优先直接尝试 `DOCKER_BUILDKIT=0`，不必重复上述排查过程。

#### 验证记录

| 项 | 方法 | 结果 |
|---|---|---|
| 修复前复现 | `docker logs r-proxy` | `PermissionError: ... config.toml.tmp` |
| 目录属主修复 | `chown 10001:10001 config/`，重试 `PUT /api/settings` | `200` |
| 代码修复后镜像 | `docker exec r-proxy grep -c fchmod .../config_writer.py` | `1`（新代码已在运行的镜像里） |
| 端到端：新增出口 | `POST /api/upstreams`（`test-permcheck2`） | `201` |
| 写回后文件权限 | `stat` `config.toml` | `600`（不再被重置为 `644`） |
| 写回后备份权限 | `stat` 新生成的 `data/backups/config-*.toml` | `600` |
| 容器健康与自愈 | `docker inspect --format Health.Status/RestartPolicy` | `healthy` / `unless-stopped` |

---

## 12. 相关文档

- [ARCH_OVERVIEW.md](./ARCH_OVERVIEW.md) — 启动与关闭时序
- [DD_CONFIG.md](./DD_CONFIG.md) — 配置路径解析与 `R_PROXY_CONFIG`
- [DD_STORAGE.md](./DD_STORAGE.md) — 三库布局、单一写者、启动回填
- [DD_WEB.md](./DD_WEB.md) — Web 绑定与 token 约束
- [PRD_OVERVIEW.md](../requirements/PRD_OVERVIEW.md) §4.8 — 依赖与部署
