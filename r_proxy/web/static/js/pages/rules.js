/**
 * 规则编辑：两列表格（条件 + 出口）、拖拽与 ↑/↓ 排序、整表提交、路由测试。
 *
 * 对应需求：WEBUI_SPEC.md §2.4；设计：docs/design/DD_WEB.md §10.7.1。
 *
 * 前端只处理 `[{condition, upstream}]` 这个数组，不拼任何规则文本：序列化归
 * 服务端，前端连生成畸形格式的机会都没有。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, input, select, table, td, text } from "../dom.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const COLUMNS = 4;
const DIRECT = "direct";

// 前端预检只认这一种「明显写错」：条件匹配主机名，带端口一定是误解。带方括号
// 的 IPv6 要放过，`[2001:db8::1]` 里的冒号是地址的一部分。
const PORT_SUFFIX = /^(?:\[.*\]|[^:[\]]+):\d{1,5}$/;

const nodes = {};
const state = {
  revision: null,
  enabled: true,
  rows: [],
  upstreams: [],
  dirty: false,
  dragIndex: null,
  badRows: new Set(),
};

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.disabledBanner = el("div", { hidden: true });
  nodes.tableBody = el("tbody");
  nodes.status = el("span", { class: "muted" });
  nodes.issues = el("div", { class: "issues" });

  root.appendChild(nodes.notice);
  root.appendChild(nodes.disabledBanner);
  root.appendChild(
    section("规则表", [
      text("p", "自上而下匹配，第一条命中即生效；条件只匹配主机名，不含路径。", {
        class: "hint",
      }),
      tableShell(),
      toolbar([
        button("添加规则", addRow),
        button("校验", () => void validate()),
        button("保存", () => void save(), { class: "primary" }),
        button("放弃修改", () => void discard()),
        nodes.status,
      ]),
      nodes.issues,
    ]),
  );
  root.appendChild(section("路由测试", [routeTestPanel()]));
}

function tableShell() {
  const built = table(["", "条件", "出口", "操作"], []);
  built.replaceChild(nodes.tableBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function routeTestPanel() {
  nodes.testUrl = input("route-url", {
    type: "text",
    placeholder: "http://www.example.com/path",
    maxlength: 2048,
  });
  nodes.testResult = el("div", { class: "result" });
  const form = el("form", { class: "toolbar" }, [
    text("label", "URL", { for: "route-url" }),
    nodes.testUrl,
    el("button", { type: "submit", class: "primary" }, ["测试"]),
  ]);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void routeTest();
  });
  return el("div", {}, [form, nodes.testResult]);
}

async function refresh() {
  const [rules, list] = await Promise.all([api.rules(), api.upstreams()]);
  state.upstreams = list.upstreams;
  state.enabled = rules.enabled;
  renderDisabledBanner();
  if (state.dirty) {
    // 有未保存改动就不覆盖。轮询本身已被 app.js 暂停，这里挡的是手动刷新。
    if (rules.revision !== state.revision) {
      notify(nodes.notice, "warn", "规则已被其他会话修改，保存时会发生版本冲突。");
    }
    return;
  }
  state.revision = rules.revision;
  state.rows = rules.rules.map((row) => ({ condition: row.condition, upstream: row.upstream }));
  state.badRows = new Set();
  render();
}

/** `isDirty` 供 app.js 决定暂停轮询与拦截导航（DD_WEB §10.7.1）。*/
function isDirty() {
  return state.dirty;
}

function renderDisabledBanner() {
  if (state.enabled) {
    clearNotice(nodes.disabledBanner);
    return;
  }
  notify(
    nodes.disabledBanner,
    "warn",
    "配置中 [rules] enabled = false，下面的规则当前不生效，全部流量走自动路由。仍可编辑保存。",
  );
}

function render() {
  const body = clear(nodes.tableBody);
  if (state.rows.length === 0) {
    body.appendChild(emptyRow(COLUMNS, "还没有规则，全部流量走自动路由"));
  } else {
    state.rows.forEach((row, index) => body.appendChild(ruleRow(row, index)));
  }
  body.appendChild(fallbackRow());
  renderStatus();
}

function ruleRow(row, index) {
  const condition = input(`rule-condition-${index}`, {
    type: "text",
    value: row.condition,
    maxlength: 1000,
    spellcheck: "false",
    placeholder: "*.example.com",
    class: state.badRows.has(index) ? "invalid" : "",
    "aria-label": `第 ${index + 1} 条规则的条件`,
  });
  // 输入时只改内存，不重画：重画会连光标一起抹掉，一行都打不完。
  condition.addEventListener("input", () => {
    row.condition = condition.value;
    markDirty();
  });
  condition.addEventListener("blur", () => {
    const complaint = conditionComplaint(condition.value.trim());
    condition.classList.toggle("invalid", complaint !== null);
    if (complaint !== null) notify(nodes.notice, "warn", `第 ${index + 1} 行：${complaint}`);
  });

  const upstream = select(`rule-upstream-${index}`, upstreamOptions(row.upstream), row.upstream);
  upstream.addEventListener("change", () => {
    row.upstream = upstream.value;
    markDirty();
    render();
  });

  // draggable 是枚举属性：赋空串会退回 auto（不可拖），且没有任何报错。
  const tr = el("tr", { draggable: "true" }, [
    text("td", "⠿", { class: "drag-handle", title: "拖动调整顺序" }),
    el("td", { class: "cell-grow" }, [condition]),
    el("td", {}, [upstream, ...warnBadge(row.upstream)]),
    el("td", { class: "actions" }, [
      button("↑", () => move(index, -1), { disabled: index === 0, "aria-label": "上移" }),
      button("↓", () => move(index, 1), {
        disabled: index === state.rows.length - 1,
        "aria-label": "下移",
      }),
      button("删除", () => remove(index), { class: "danger" }),
    ]),
  ]);
  tr.addEventListener("dragstart", () => {
    state.dragIndex = index;
  });
  tr.addEventListener("dragover", (event) => event.preventDefault());
  tr.addEventListener("drop", (event) => {
    event.preventDefault();
    if (state.dragIndex === null || state.dragIndex === index) return;
    const [moved] = state.rows.splice(state.dragIndex, 1);
    state.rows.splice(index, 0, moved);
    state.dragIndex = null;
    state.badRows = new Set();
    markDirty();
    render();
  });
  return tr;
}

/** 兜底行只读，不做成下拉框：那等价于一条 `*` 规则，会关掉自动故障切换。*/
function fallbackRow() {
  return el("tr", { class: "fallback-row" }, [
    td(""),
    td("其余流量"),
    el("td", { colspan: 2 }, [
      text("span", "自动选择（按优先级与健康状态，失败时自动切换）", { class: "muted" }),
    ]),
  ]);
}

/**
 * 下拉选项。已配置出口 + 保留名 `direct`；当前值不在其中时也要补进去，否则
 * 浏览器会静默选中第一项，把一条引用未知出口的规则悄悄改到别的出口上。
 */
function upstreamOptions(current) {
  const names = state.upstreams.map((item) => item.name);
  if (!names.includes(DIRECT)) names.unshift(DIRECT);
  if (current && !names.includes(current)) names.push(current);
  return names.map((name) => ({ value: name, label: name }));
}

function warnBadge(name) {
  const item = state.upstreams.find((u) => u.name === name);
  if (item === undefined) {
    return name === DIRECT ? [] : [badge("未配置", "bad")];
  }
  return item.enabled ? [] : [badge("已禁用", "warn")];
}

function markDirty() {
  if (state.dirty) return;
  state.dirty = true;
  renderStatus();
}

function renderStatus() {
  nodes.status.textContent = state.dirty
    ? `共 ${state.rows.length} 条 · 有未保存改动，自动刷新已暂停`
    : `共 ${state.rows.length} 条 · 版本 ${state.revision ?? "—"}`;
  nodes.status.className = state.dirty ? "warn-text" : "muted";
}

function addRow() {
  state.rows.push({ condition: "", upstream: DIRECT });
  markDirty();
  render();
  const last = nodes.tableBody.querySelector(`#rule-condition-${state.rows.length - 1}`);
  if (last !== null) last.focus();
}

function remove(index) {
  state.rows.splice(index, 1);
  state.badRows = new Set();
  markDirty();
  render();
}

function move(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.rows.length) return;
  [state.rows[index], state.rows[target]] = [state.rows[target], state.rows[index]];
  state.badRows = new Set();
  markDirty();
  render();
}

/**
 * 本地形状预检。只挡明显写错的两种，其余交给服务端：前端复刻一份条件分类必然
 * 与服务端漂移，而漂移的方向总是「前端放过了服务端拒绝的东西」。
 */
function conditionComplaint(condition) {
  if (condition === "") return "条件不能为空。";
  if (PORT_SUFFIX.test(condition)) return "条件只匹配主机名，不能带端口。";
  return null;
}

function payload() {
  return state.rows.map((row) => ({
    condition: row.condition.trim(),
    upstream: row.upstream,
  }));
}

async function validate() {
  try {
    const result = await api.validateRules({ rules: payload() });
    renderIssues(result.issues, result.ok, result.rule_count);
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function save() {
  if (!state.dirty) {
    notify(nodes.notice, "info", "没有改动。");
    return;
  }
  try {
    const result = await api.saveRules({ revision: state.revision, rules: payload() });
    state.revision = result.revision;
    state.dirty = false;
    state.badRows = new Set();
    notify(nodes.notice, "ok", "已保存并热重载。");
    renderIssues(result.issues, true, state.rows.length);
  } catch (err) {
    if (err.status === 409) {
      renderConflict(err);
      return;
    }
    notify(nodes.notice, "bad", describeError(err));
    if (Array.isArray(err.details)) renderIssues(err.details, false, state.rows.length);
  }
}

function renderConflict(err) {
  const box = clear(nodes.notice);
  box.className = "notice notice-bad";
  box.hidden = false;
  const actual = err.details && err.details.actual;
  box.appendChild(
    text(
      "span",
      `规则已被其他会话修改${actual === undefined ? "" : `（当前版本 ${actual}）`}，本次未写入。`,
    ),
  );
  box.appendChild(
    button("重新载入", () => void discard(), { class: "link" }),
  );
  box.appendChild(text("span", " 会丢弃本地改动。", { class: "muted" }));
}

async function discard() {
  if (state.dirty && !confirmAction("放弃未保存的改动，重新载入服务端的规则？")) return;
  state.dirty = false;
  clear(nodes.issues);
  clearNotice(nodes.notice);
  await refresh();
}

function renderIssues(issues, ok, ruleCount) {
  const box = clear(nodes.issues);
  state.badRows = new Set();
  if (issues.length === 0) {
    box.appendChild(
      text("p", `校验通过，共 ${ruleCount} 条规则。`, { class: "notice notice-ok" }),
    );
    render();
    return;
  }
  box.appendChild(
    text("p", ok ? "有提示，不影响保存：" : "校验未通过，未写入：", {
      class: ok ? "notice notice-warn" : "notice notice-bad",
    }),
  );
  const list = el("ul", { class: "issue-list" });
  for (const issue of issues) {
    const position = positionOf(issue.location);
    if (position !== null && issue.level === "error") state.badRows.add(position);
    list.appendChild(
      text("li", `${issue.location} ${issue.code} ${issue.message}`, {
        class: issue.level === "error" ? "bad-text" : "warn-text",
      }),
    );
  }
  box.appendChild(list);
  render();
}

/** 位置形如 `rules[3]`。取不到就不标行，不猜。*/
function positionOf(location) {
  const match = /^rules\[(\d+)\]$/.exec(location ?? "");
  return match === null ? null : Number(match[1]);
}

async function routeTest() {
  const url = nodes.testUrl.value.trim();
  if (!url) return;
  const box = clear(nodes.testResult);
  try {
    const result = await api.routeTest(url);
    const rows = [
      ["目标", `${result.host}:${result.port}`],
      ["决策来源", result.decision],
      ["最终出口", result.upstream ?? "无可用出口"],
      [
        "命中规则",
        result.matched_rule === null
          ? "未命中，走自动路由"
          : `第 ${result.matched_rule.position + 1} 条：${result.matched_rule.condition}`,
      ],
      ["候选链", result.candidate_chain.join(" → ") || "空"],
    ];
    const built = table(["项", "值"], rows.map(([key, value]) => el("tr", {}, [td(key), td(value)])));
    box.appendChild(el("div", { class: "table-wrap" }, [built]));
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function unmount() {
  state.dirty = false;
  state.dragIndex = null;
  state.badRows = new Set();
  clearNotice(nodes.notice);
  clearNotice(nodes.disabledBanner);
}

export const page = { id: "rules", title: "规则编辑", mount, refresh, unmount, isDirty };
