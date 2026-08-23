# 需求文档索引

| 文档 | 版本 | 说明 |
|------|------|------|
| [PRD_OVERVIEW.md](./PRD_OVERVIEW.md) | v1.7.0 | 产品需求总览：实施路线图、优先级切换策略、并发与一致性、数据模型、性能基线与资源限制 |
| [RULES_CONFIG.md](./RULES_CONFIG.md) | v2.0.0 | 路由规则说明：六种条件类型、首匹配胜出、只匹配主机名、`rules.db` 存储与救场开关 |
| [WEBUI_SPEC.md](./WEBUI_SPEC.md) | v2.1.0 | Web 管理界面需求：五大功能模块、REST API、配置持久化与操作审计、安全要求、粘性映射固化为规则 |

主配置文件的完整结构以 PRD_OVERVIEW.md §4.2.1 为准，其余文档只引用不复制。

## 两个权威来源

配置有两处权威来源，各自互不重叠（见 [WEBUI_SPEC §6.1](./WEBUI_SPEC.md)）：

| 内容 | 来源 | 编辑方式 |
|------|------|----------|
| 出口、超时、切换策略、监听地址 | `config.toml` | Web 表单 + 允许手工编辑 |
| 路由规则 | `rules.db` | **仅** Web 表格界面 |

> **v2.0.0 的重构**：规则原为 `rules.files` 声明的 Privoxy 风格文本文件，现改为数据库存储 + 表格界面，匹配语义同时由 last-match-wins 改为 first-match-wins、匹配对象收窄为仅主机名。不提供兼容层，旧格式的换算表见 [RULES_CONFIG §9](./RULES_CONFIG.md)。

## 相关文档

- [设计文档](../design/index.md)
