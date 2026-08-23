/**
 * 全局设置、重载、备份恢复与操作审计。
 *
 * 对应需求：WEBUI_SPEC.md §2.5、§3.5、§6.5；设计：docs/design/DD_WEB.md §8.8、§10.7。
 *
 * 表单字段与服务端的白名单一一对应：服务端只认字段名并自行映射到点分键，
 * 界面这边多填一个也写不进 config.toml。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, field, input, select, table, td, text } from "../dom.js";
import { bytes, timestamp } from "../format.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const BACKUP_COLUMNS = 4;
const AUDIT_COLUMNS = 6;

// 三档呈现（WEBUI_SPEC §2.5）：建议、有风险、劝阻。劝阻档不禁止勾选，
// 用户可能有特殊场景，但必须让他知道代价。
const STATUS_GROUPS = [
  {
    kind: "ok",
    title: "建议开启",
    hint: "这些码基本只可能来自代理本身或链路问题。",
    codes: [407, 408, 502, 503, 504, 511],
  },
  {
    kind: "warn",
    title: "有风险",
    hint:
      "这三个码可能来自目标站的正常业务逻辑：开启后正常的权限拒绝或限流会触发出口遍历，" +
      "429 尤其可能让所有代理 IP 被依次限流。切换限流（下方）是配套的防护。",
    codes: [403, 429, 451],
  },
  {
    kind: "bad",
    title: "不建议",
    hint: "开启后会白白遍历所有出口且不可能成功：目标应用自身的错误、Cloudflare 回源故障、资源不存在，换出口得到的都是同一结果。",
    codes: [500, 501, 505, 520, 521, 522, 523, 524, 525, 526, 404, 410],
  },
];

const AUDIT_ACTIONS = [
  "",
  "settings.update",
  "upstream.create",
  "upstream.update",
  "upstream.delete",
  "upstream.priorities",
  "rules.update",
  "config.restore",
  "config.reload",
  "bind_sticky",
  "clear_sticky",
  "clear_sticky_batch",
  "clear_route_block",
  "circuit.reset",
];

const nodes = {};
const fields = {};
const state = { version: null, dirty: false, statusBoxes: new Map(), auditPage: 1 };

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.form = el("form", { class: "settings-form" });
  nodes.readonly = el("div", { class: "form-grid" });
  nodes.statusPanel = el("div", { class: "status-groups" });
  nodes.statusWarning = el("p", { class: "hint" });
  nodes.backupBody = el("tbody");
  nodes.auditBody = el("tbody");

  root.appendChild(nodes.notice);
  root.appendChild(section("路由与存储", [nodes.form]));
  root.appendChild(
    section("切换状态码", [
      text("p", "收到这些状态码时尝试下一个出口。勾选 2xx / 3xx 会被拒绝保存。", {
        class: "hint",
      }),
      nodes.statusPanel,
      nodes.statusWarning,
    ]),
  );
  root.appendChild(
    section("监听（只读）", [
      text("p", "修改监听地址与端口需要重启服务，界面不提供编辑。", { class: "hint" }),
      nodes.readonly,
    ]),
  );
  root.appendChild(
    section("配置维护", [
      toolbar([
        button("重新载入配置与规则", () => void reload(), { class: "primary" }),
        text("span", "重载会重新读取磁盘上的文件，不改动任何内容。", { class: "muted" }),
      ]),
      backupShell(),
    ]),
  );
  root.appendChild(section("操作审计", [auditBar(), auditShell()]));

  nodes.form.addEventListener("input", () => {
    state.dirty = true;
  });
  nodes.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });
}

function backupShell() {
  const built = table(["备份文件", "大小", "时间", "操作"], []);
  built.replaceChild(nodes.backupBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function auditShell() {
  const built = table(["时间", "来源", "动作", "目标", "版本变化", "变更摘要"], []);
  built.replaceChild(nodes.auditBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function auditBar() {
  nodes.auditAction = select(
    "audit-action",
    AUDIT_ACTIONS.map((action) => ({ value: action, label: action || "全部动作" })),
    "",
  );
  nodes.auditAction.addEventListener("change", () => {
    state.auditPage = 1;
    void loadAudit();
  });
  nodes.auditPrev = button("上一页", () => {
    if (state.auditPage <= 1) return;
    state.auditPage -= 1;
    void loadAudit();
  });
  nodes.auditNext = button("下一页", () => {
    state.auditPage += 1;
    void loadAudit();
  });
  nodes.auditLabel = text("span", "", { class: "muted" });
  return toolbar([nodes.auditAction, nodes.auditPrev, nodes.auditLabel, nodes.auditNext]);
}

async function refresh() {
  const [settings, backups] = await Promise.all([api.settings(), api.backups()]);
  state.version = settings.config_version;
  // 表单被动过就不重画：3 秒一次的轮询会把正在输入的值冲掉。
  if (!state.dirty) {
    renderForm(settings);
    renderStatusCodes(settings.routing.switch_on_status);
    renderReadonly(settings);
  }
  renderBackups(backups.backups);
  await loadAudit();
}

function renderForm(settings) {
  const r = settings.routing;
  const d = settings.storage;
  const spec = [
    ["connect_timeout", "连接超时（秒）", { type: "number", step: "0.1", min: 0.1, max: 300 }, r.connect_timeout],
    ["read_timeout", "读超时（秒）", { type: "number", step: "0.1", min: 0.1, max: 3600 }, r.read_timeout],
    ["sticky_fail_threshold", "粘性失败阈值", { type: "number", min: 1, max: 100 }, r.sticky_fail_threshold],
    ["route_block_ttl", "负面记忆有效期（秒）", { type: "number", min: 1, max: 86400 }, r.route_block_ttl],
    ["tunnel_probe_window", "隧道早夭窗口（秒）", { type: "number", step: "0.1", min: 0.1, max: 300 }, r.tunnel_probe_window],
    ["max_switches_per_host", "每 host 最大切换次数", { type: "number", min: 1, max: 1000 }, r.status_switch_rate_limit.max_switches_per_host],
    ["window_seconds", "切换限流窗口（秒）", { type: "number", min: 1, max: 3600 }, r.status_switch_rate_limit.window_seconds],
    ["circuit_breaker_fail_threshold", "熔断失败阈值", { type: "number", min: 1, max: 1000 }, r.circuit_breaker.fail_threshold],
    ["circuit_breaker_cooldown_seconds", "熔断冷却（秒）", { type: "number", min: 1, max: 86400 }, r.circuit_breaker.cooldown_seconds],
    ["retention_days", "日志保留天数", { type: "number", min: 1, max: 3650 }, d.retention_days],
    ["max_log_rows", "日志最大行数", { type: "number", min: 1000, max: 100000000 }, d.max_log_rows],
    ["backup_keep", "配置备份保留份数", { type: "number", min: 1, max: 100 }, d.backup_keep],
  ];

  const grid = el("div", { class: "form-grid" });
  for (const [name, label, attrs, value] of spec) {
    fields[name] = input(`set-${name}`, { ...attrs, value });
    grid.appendChild(field(label, fields[name]));
  }
  fields.circuit_breaker_enabled = input("set-circuit_breaker_enabled", {
    type: "checkbox",
    checked: r.circuit_breaker.enabled,
  });
  grid.appendChild(field("启用熔断", fields.circuit_breaker_enabled, "只统计 upstream_error"));

  const form = clear(nodes.form);
  form.appendChild(grid);
  form.appendChild(
    toolbar([
      el("button", { type: "submit", class: "primary" }, ["保存并生效"]),
      button("放弃修改", () => {
        state.dirty = false;
        void refresh();
      }),
      text("span", `当前配置版本 ${settings.config_version}`, { class: "muted" }),
    ]),
  );
}

function renderStatusCodes(selected) {
  const chosen = new Set(selected);
  const panel = clear(nodes.statusPanel);
  state.statusBoxes = new Map();
  for (const group of STATUS_GROUPS) {
    const boxes = el("div", { class: "code-row" });
    for (const code of group.codes) {
      const box = input(`code-${code}`, { type: "checkbox", checked: chosen.has(code) });
      box.addEventListener("change", () => {
        state.dirty = true;
        renderStatusWarning();
      });
      state.statusBoxes.set(code, box);
      boxes.appendChild(
        el("label", { class: "code-box" }, [box, text("span", String(code))]),
      );
    }
    panel.appendChild(
      el("div", { class: `code-group code-${group.kind}` }, [
        el("div", { class: "code-title" }, [badge(group.title, group.kind)]),
        text("p", group.hint, { class: "hint" }),
        boxes,
      ]),
    );
  }
  const extra = [...chosen].filter(
    (code) => !STATUS_GROUPS.some((group) => group.codes.includes(code)),
  );
  fields.extra_status = input("set-extra-status", {
    type: "text",
    value: extra.join(", "),
    placeholder: "如 418, 460",
  });
  fields.extra_status.addEventListener("input", () => {
    state.dirty = true;
  });
  panel.appendChild(field("其他状态码（逗号分隔）", fields.extra_status));
  renderStatusWarning();
}

function codesOfKind(kind) {
  return STATUS_GROUPS.find((group) => group.kind === kind).codes;
}

function renderStatusWarning() {
  const codes = collectStatusCodes();
  const risky = codes.filter((code) => codesOfKind("warn").includes(code));
  const useless = codes.filter((code) => codesOfKind("bad").includes(code));
  const parts = [];
  if (risky.length > 0) parts.push(`已勾选有风险的 ${risky.join("、")}`);
  if (useless.length > 0) parts.push(`已勾选不建议的 ${useless.join("、")}`);
  nodes.statusWarning.textContent = parts.join("；");
  nodes.statusWarning.className = parts.length > 0 ? "notice notice-warn" : "hint";
}

function collectStatusCodes() {
  const codes = new Set();
  for (const [code, box] of state.statusBoxes) {
    if (box.checked) codes.add(code);
  }
  for (const piece of (fields.extra_status?.value ?? "").split(",")) {
    const value = Number(piece.trim());
    if (Number.isInteger(value) && value > 0) codes.add(value);
  }
  return [...codes].sort((a, b) => a - b);
}

function renderReadonly(settings) {
  const grid = clear(nodes.readonly);
  const rows = [
    ["代理监听", `${settings.listen.listen_host}:${settings.listen.listen_port}`],
    ["管理界面", `${settings.listen.webui_host}:${settings.listen.webui_port}`],
    ["切换缓冲上限", bytes(settings.routing.switch_buffer_bytes)],
    ["Happy Eyeballs 延迟", `${settings.routing.happy_eyeballs_delay} 秒`],
    ["需重启才生效的项", settings.restart_required_fields.join("、")],
  ];
  for (const [label, value] of rows) {
    grid.appendChild(
      el("div", { class: "field" }, [
        text("span", label, { class: "card-label" }),
        text("strong", value),
      ]),
    );
  }
}

async function submit() {
  const codes = collectStatusCodes();
  // 前端先拦一道：勾了 2xx/3xx 等于把成功响应当失败，每个正常请求都会遍历
  // 全部出口。服务端也会拒（E_SWITCH_STATUS_2XX），这里只是让反馈更快。
  const bad = codes.filter((code) => code >= 200 && code < 400);
  if (bad.length > 0) {
    notify(nodes.notice, "bad", `不能把 ${bad.join("、")} 作为切换条件：它们是成功响应。`);
    return;
  }
  if (codes.length === 0) {
    notify(nodes.notice, "bad", "至少要保留一个切换状态码。");
    return;
  }

  const body = { config_version: state.version, switch_on_status: codes };
  for (const [name, control] of Object.entries(fields)) {
    if (name === "extra_status") continue;
    body[name] = control.type === "checkbox" ? control.checked : Number(control.value);
  }
  try {
    const result = await api.updateSettings(body);
    state.version = result.config_version;
    state.dirty = false;
    notify(nodes.notice, "ok", "设置已保存并生效。");
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function reload() {
  try {
    const result = await api.reload();
    state.version = result.config_version;
    state.dirty = false;
    notify(nodes.notice, "ok", `已重新载入，当前版本 ${result.config_version}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function renderBackups(backups) {
  const body = clear(nodes.backupBody);
  if (backups.length === 0) {
    body.appendChild(emptyRow(BACKUP_COLUMNS, "还没有备份。每次写回配置前都会自动生成。"));
    return;
  }
  for (const item of backups) {
    body.appendChild(
      el("tr", {}, [
        td(item.filename),
        td(bytes(item.size)),
        td(timestamp(item.created_at)),
        el("td", { class: "actions" }, [
          button("恢复", () => void restore(item.filename), { class: "danger" }),
        ]),
      ]),
    );
  }
}

async function restore(filename) {
  if (!confirmAction(`用 ${filename} 覆盖当前配置？当前内容会先被备份。`)) return;
  try {
    const result = await api.restore({ filename, config_version: state.version });
    state.version = result.config_version;
    state.dirty = false;
    notify(nodes.notice, "ok", `已恢复 ${filename}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function loadAudit() {
  try {
    const result = await api.audit({
      action: nodes.auditAction.value,
      page: state.auditPage,
      page_size: 20,
    });
    renderAudit(result);
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function renderAudit(result) {
  const body = clear(nodes.auditBody);
  if (result.items.length === 0) {
    body.appendChild(emptyRow(AUDIT_COLUMNS, "没有审计记录"));
  }
  for (const item of result.items) {
    body.appendChild(
      el("tr", {}, [
        td(timestamp(item.created_at)),
        td(item.actor),
        td(item.action),
        td(item.target),
        td(`${item.version_before ?? "—"} → ${item.version_after ?? "—"}`),
        // diff 是多行统一 diff，塞进普通单元格会被 nowrap 压成一长条。
        // 内容在入库前已脱敏（口令一律为 ***），这里原样以文本呈现。
        el("td", {}, [text("div", item.diff ?? "—", { class: "diff-cell" })]),
      ]),
    );
  }
  nodes.auditLabel.textContent = `第 ${result.page} 页`;
  nodes.auditPrev.disabled = result.page <= 1;
  nodes.auditNext.disabled = !result.has_more;
}

function unmount() {
  state.dirty = false;
  state.auditPage = 1;
  clearNotice(nodes.notice);
}

export const page = { id: "settings", title: "全局设置", mount, refresh, unmount };
