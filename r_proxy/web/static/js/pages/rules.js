/**
 * 规则编辑：互斥条件、固定字母序、筛选分页、整表提交与路由测试。
 *
 * 对应需求：WEBUI_SPEC.md §2.4；设计：docs/design/DD_WEB.md §10.7.1。
 *
 * 前端只处理 `[{condition, upstream}]` 这个数组，不拼任何规则文本：序列化归
 * 服务端，前端连生成畸形格式的机会都没有。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, input, select, table, td, text } from "../dom.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const COLUMNS = 3;
const DIRECT = "direct";
const PAGE_SIZE = 100;

// 条件匹配主机名，带端口一定是误解。带方括号
// 的 IPv6 要放过，`[2001:db8::1]` 里的冒号是地址的一部分。
const PORT_SUFFIX = /^(?:\[.*\]|[^:[\]]+):\d{1,5}$/;

const nodes = {};
const state = {
  revision: null,
  checkedRevision: null,
  enabled: true,
  rows: [],
  upstreams: [],
  dirty: false,
  badRows: new Set(),
  filterUpstream: "",
  filterHost: "",
  page: 1,
  draftVersion: 0,
  newRow: null,
  saving: false,
};
let deferredRender = null;

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.disabledBanner = el("div", { hidden: true });
  nodes.tableBody = el("tbody");
  nodes.status = el("span", { class: "muted" });
  nodes.issues = el("div", { class: "issues" });
  nodes.filterUpstream = select("rules-filter-upstream", [], "");
  nodes.filterHost = input("rules-filter-host", {
    type: "search",
    placeholder: "搜索 host / 条件",
    maxlength: 1000,
    class: "rule-filter-host",
    "aria-label": "搜索 host 或规则条件",
  });
  nodes.pageInfo = el("span", { class: "muted" });
  nodes.previousPage = button("上一页", () => changePage(-1));
  nodes.nextPage = button("下一页", () => changePage(1));
  nodes.tableBody.addEventListener("focusout", deferSortAfterEditing);

  nodes.filterUpstream.addEventListener("change", () => {
    state.filterUpstream = nodes.filterUpstream.value;
    state.page = 1;
    render();
  });
  nodes.filterHost.addEventListener("input", () => {
    state.filterHost = nodes.filterHost.value;
    state.page = 1;
    render();
  });

  root.appendChild(nodes.notice);
  root.appendChild(nodes.disabledBanner);
  root.appendChild(
    section("规则表", [
      text("p", "规则按条件字母排序；条件不能相互覆盖，出口相同也不能重叠。纯数字 IPv4 前缀通配符仅匹配 IPv4 地址；其他条件支持 * 通配符，不支持新增正则。", {
        class: "hint",
      }),
      toolbar([
        text("label", "出口", { for: "rules-filter-upstream" }),
        nodes.filterUpstream,
        text("label", "条件", { for: "rules-filter-host" }),
        nodes.filterHost,
      ]),
      tableShell(),
      text("p", "未命中任何规则的流量：自动选择出口，并按优先级与健康状态故障切换。", {
        class: "hint",
      }),
      toolbar([nodes.previousPage, nodes.pageInfo, nodes.nextPage]),
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
  const built = table(["条件", "出口", "操作"], []);
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

async function refresh(expectedRevision = null) {
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
  if (expectedRevision !== null && rules.revision !== expectedRevision) {
    state.dirty = true;
    renderConflict({ details: { actual: rules.revision } }, true);
    renderStatus();
    return;
  }
  if (state.revision === rules.revision && state.checkedRevision === rules.revision) {
    // 相同快照轮询不重建草稿对象或抹掉冲突标记，也不抢走正在查看的输入焦点。
    if (!nodes.tableBody.contains(document.activeElement)) render();
    return;
  }
  state.revision = rules.revision;
  state.rows = rules.rules.map((row) => ({ condition: row.condition, upstream: row.upstream }));
  state.draftVersion += 1;
  state.newRow = null;
  state.badRows = new Set();
  render();
  if (state.checkedRevision === state.revision) return;
  const revision = state.revision;
  const version = state.draftVersion;
  state.checkedRevision = revision;
  try {
    const result = await api.validateRules({ rules: payload() });
    if (state.dirty || state.revision !== revision || state.draftVersion !== version) return;
    if (result.issues.length === 0) {
      state.badRows = new Set();
      clear(nodes.issues);
      render();
    } else {
      renderIssues(result.issues, result.ok, result.rule_count, true);
    }
  } catch (err) {
    // 规则读取成功时仍可编辑；预检不可用不应使整个规则页失效。
    notify(nodes.notice, "warn", `无法检查已保存规则：${describeError(err)}`);
  }
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
  renderFilterOptions();
  const body = clear(nodes.tableBody);
  const matchingRows = visibleRows();
  const pageCount = Math.max(1, Math.ceil(matchingRows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pageCount);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageRows = matchingRows.slice(start, start + PAGE_SIZE);

  if (state.rows.length === 0) {
    body.appendChild(emptyRow(COLUMNS, "还没有规则，全部流量走自动路由"));
  } else if (matchingRows.length === 0) {
    body.appendChild(emptyRow(COLUMNS, "没有符合筛选条件的规则"));
  } else {
    pageRows.forEach(({ row, index }) => body.appendChild(ruleRow(row, index)));
  }
  nodes.previousPage.disabled = state.page <= 1;
  nodes.nextPage.disabled = state.page >= pageCount;
  nodes.pageInfo.textContent = `第 ${state.page} / ${pageCount} 页 · 显示 ${pageRows.length} / ${matchingRows.length} 条`;
  renderStatus();
}

function renderFilterOptions() {
  const names = new Set([
    DIRECT,
    ...state.upstreams.map((item) => item.name),
    ...state.rows.map((row) => row.upstream),
  ]);
  const options = [el("option", { value: "" }, ["全部出口"])];
  for (const name of [...names].sort(compareConditions)) {
    options.push(el("option", { value: name }, [name]));
  }
  nodes.filterUpstream.replaceChildren(...options);
  if (names.has(state.filterUpstream)) nodes.filterUpstream.value = state.filterUpstream;
  else {
    state.filterUpstream = "";
    nodes.filterUpstream.value = "";
  }
  if (nodes.filterHost.value !== state.filterHost) nodes.filterHost.value = state.filterHost;
}

function asciiFold(value) {
  return value.replace(/[A-Z]/g, (letter) => letter.toLowerCase());
}

function compareConditions(a, b) {
  const foldedA = asciiFold(a.trim());
  const foldedB = asciiFold(b.trim());
  return compareCodePoints(foldedA, foldedB) || compareCodePoints(a, b);
}

function compareCodePoints(a, b) {
  // SQLite BINARY/Python 字符序与 UTF-16 的 < 对补充平面字符排序不同。
  const left = Array.from(a, (letter) => letter.codePointAt(0));
  const right = Array.from(b, (letter) => letter.codePointAt(0));
  for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return left.length - right.length;
}

function visibleRows() {
  const query = state.filterHost.trim().toLowerCase();
  const rows = state.rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => {
      if (row === state.newRow) return true;
      if (state.filterUpstream && row.upstream !== state.filterUpstream) return false;
      return !query || row.condition.toLowerCase().includes(query);
    });
  rows.sort((a, b) => {
    if (a.row === b.row) return a.index - b.index;
    if (a.row === state.newRow) return -1;
    if (b.row === state.newRow) return 1;
    return compareConditions(a.row.condition, b.row.condition) || a.index - b.index;
  });
  return rows;
}

function changePage(delta) {
  const matchingCount = visibleRows().length;
  const pageCount = Math.max(1, Math.ceil(matchingCount / PAGE_SIZE));
  state.page = Math.max(1, Math.min(pageCount, state.page + delta));
  render();
}

function deferSortAfterEditing(event) {
  // 在同一表格里换输入框/出口时不拆掉刚获得焦点的控件；删除行的点击自己重画。
  if (nodes.tableBody.contains(event.relatedTarget)) return;
  // pointerdown 会先触发 blur，click 尚未到来。等按钮完成操作再重画，避免吞点击。
  if (event.relatedTarget?.tagName === "BUTTON") {
    event.relatedTarget.addEventListener("click", scheduleRender, { once: true });
  } else {
    scheduleRender();
  }
}

function scheduleRender() {
  if (deferredRender !== null) clearTimeout(deferredRender);
  deferredRender = setTimeout(() => {
    deferredRender = null;
    if (nodes.tableBody.contains(document.activeElement)) return;
    if (state.newRow !== null && conditionComplaint(state.newRow.condition.trim()) === null) {
      state.newRow = null;
    }
    render();
  }, 0);
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
    state.badRows.delete(index);
    condition.classList.remove("invalid");
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
    state.badRows.delete(index);
    markDirty();
    scheduleRender();
  });

  return el("tr", {}, [
    el("td", { class: "cell-grow" }, [condition]),
    el("td", {}, [upstream, ...warnBadge(row.upstream)]),
    el("td", { class: "actions" }, [button("删除", () => remove(index), { class: "danger" })]),
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
  state.draftVersion += 1;
  state.checkedRevision = null;
  clear(nodes.issues);
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
  const row = { condition: "", upstream: DIRECT };
  state.rows.push(row);
  state.newRow = row;
  state.filterUpstream = "";
  state.filterHost = "";
  state.page = 1;
  markDirty();
  render();
  const last = nodes.tableBody.querySelector(`#rule-condition-${state.rows.length - 1}`);
  if (last !== null) last.focus();
}

function remove(index) {
  const [removed] = state.rows.splice(index, 1);
  if (state.newRow === removed) state.newRow = null;
  state.badRows = new Set();
  markDirty();
  render();
}

/**
 * 本地形状预检。只挡空条件、显式正则和端口误用，其余交给服务端：前端复刻一份条件分类必然
 * 与服务端漂移，而漂移的方向总是「前端放过了服务端拒绝的东西」。
 */
function conditionComplaint(condition) {
  if (condition === "") return "条件不能为空。";
  if (condition.startsWith("^") || (condition.startsWith("/") && condition.endsWith("/"))) {
    return "不支持新增正则规则，请使用主机名或 * 通配符。";
  }
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
  const version = state.draftVersion;
  try {
    const result = await api.validateRules({ rules: payload() });
    if (state.draftVersion !== version) return;
    renderIssues(result.issues, result.ok, result.rule_count);
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function save() {
  if (state.saving) return;
  if (!state.dirty) {
    notify(nodes.notice, "info", "没有改动。");
    return;
  }
  const version = state.draftVersion;
  state.saving = true;
  try {
    const result = await api.saveRules({ revision: state.revision, rules: payload() });
    state.revision = result.revision;
    state.checkedRevision = null;
    if (state.draftVersion !== version) {
      notify(nodes.notice, "warn", "已保存提交时的规则；提交后的编辑仍未保存。");
      renderStatus();
      return;
    }
    state.dirty = false;
    state.badRows = new Set();
    state.newRow = null;
    try {
      // 保存后以相同 revision 的规范字母序快照刷新；旧输入下标不套用到新顺序。
      await refresh(result.revision);
      if (!state.dirty) notify(nodes.notice, "ok", "已保存并热重载。");
    } catch (err) {
      notify(nodes.notice, "warn", `已保存，但无法重新读取规则：${describeError(err)}`);
    }
  } catch (err) {
    if (err.status === 409) {
      renderConflict(err);
      return;
    }
    notify(nodes.notice, "bad", describeError(err));
    if (state.draftVersion === version && Array.isArray(err.details)) {
      renderIssues(err.details, false, state.rows.length);
    }
  } finally {
    state.saving = false;
  }
}

function renderConflict(err, saved = false) {
  const box = clear(nodes.notice);
  box.className = "notice notice-bad";
  box.hidden = false;
  const actual = err.details && err.details.actual;
  box.appendChild(
    text(
      "span",
      `规则已被其他会话修改${actual === undefined ? "" : `（当前版本 ${actual}）`}，${saved ? "本次保存后又出现新版本，请重新载入。" : "本次未写入。"}`,
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
  state.draftVersion += 1;
  state.checkedRevision = null;
  clear(nodes.issues);
  clearNotice(nodes.notice);
  await refresh();
}

function renderIssues(issues, ok, ruleCount, persisted = false) {
  const box = clear(nodes.issues);
  state.badRows = new Set();
  if (issues.length === 0) {
    box.appendChild(
      text("p", `校验通过，共 ${ruleCount} 条规则。`, { class: "notice notice-ok" }),
    );
    render();
    return;
  }
  let heading;
  if (persisted) {
    heading = ok
      ? "当前已保存规则有以下提示："
      : "当前已保存规则有问题，修复这些行后才能保存：";
  } else {
    heading = ok ? "有提示，不影响保存：" : "校验未通过，未写入：";
  }
  box.appendChild(
    text("p", heading, { class: ok ? "notice notice-warn" : "notice notice-bad" }),
  );
  const list = el("ul", { class: "issue-list" });
  const errorPositions = [];
  for (const issue of issues) {
    const position = positionOf(issue.location);
    const relatedPosition = positionOf(issue.conflict?.related_location);
    const item = text("li", `${issue.location} ${issue.code} ${issue.message}`, {
      class: issue.level === "error" ? "bad-text" : "warn-text",
    });
    if (issue.conflict) {
      const conflict = issue.conflict;
      item.appendChild(text("p", `${conflict.condition} → ${conflict.upstream} 与 ${conflict.related_condition} → ${conflict.related_upstream}`));
      if (conflict.example_host) {
        item.appendChild(text("p", `共同匹配的 host：${conflict.example_host}`));
      }
    }
    for (const [index, label] of [
      [position, issue.conflict ? "定位规则一" : "定位"],
      [relatedPosition, "定位规则二"],
    ]) {
      if (index === null || index >= state.rows.length || issue.level !== "error") continue;
      state.badRows.add(index);
      errorPositions.push(index);
      item.appendChild(button(label, () => focusRule(index), { class: "link" }));
    }
    list.appendChild(item);
  }
  if (errorPositions.length > 0) {
    state.filterUpstream = "";
    state.filterHost = "";
    const firstVisibleError = visibleRows().findIndex(({ index }) => state.badRows.has(index));
    state.page = Math.floor(Math.max(0, firstVisibleError) / PAGE_SIZE) + 1;
  }
  box.appendChild(list);
  render();
}

function focusRule(position) {
  state.filterUpstream = "";
  state.filterHost = "";
  const visibleIndex = visibleRows().findIndex(({ index }) => index === position);
  if (visibleIndex < 0) return;
  state.page = Math.floor(visibleIndex / PAGE_SIZE) + 1;
  render();
  const field = nodes.tableBody.querySelector(`#rule-condition-${position}`);
  if (field !== null) field.focus();
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
  if (deferredRender !== null) clearTimeout(deferredRender);
  deferredRender = null;
  state.draftVersion += 1;
  state.dirty = false;
  state.newRow = null;
  state.checkedRevision = null;
  state.badRows = new Set();
  clearNotice(nodes.notice);
  clearNotice(nodes.disabledBanner);
}

export const page = { id: "rules", title: "规则编辑", mount, refresh, unmount, isDirty };
