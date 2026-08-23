/**
 * 上级代理管理：增删改、启用开关、优先级（数值编辑 + 分组拖拽/上下移）、连通性测试。
 *
 * 对应需求：WEBUI_SPEC.md §2.2、§3.2；设计：docs/design/DD_WEB.md §8.2、§10.7。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, field, input, select, table, td, text } from "../dom.js";
import { ago, circuit, millis, percent } from "../format.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const COLUMNS = 9;
const NAME_PATTERN = "[A-Za-z0-9][A-Za-z0-9._-]*";

// 只认「结尾是冒号加数字」，故意宽松：IPv6 的冒号歧义交给后端判定，前端宁可
// 放过也不能拦住合法地址。
const PORT_SUFFIX = /:\d{1,5}$/;

const nodes = {};
const state = {
  version: null,
  upstreams: [],
  editing: null,
  pendingGroups: null,
  dragIndex: null,
};

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.tableBody = el("tbody");
  nodes.groupList = el("ol", { class: "group-list" });
  nodes.groupActions = el("div", { class: "toolbar" });

  root.appendChild(nodes.notice);
  root.appendChild(
    section("出口列表", [
      text("p", "优先级数字越小越优先；同一数值的出口组成轮询组。", { class: "hint" }),
      tableShell(),
    ]),
  );
  root.appendChild(
    section("优先级顺序", [
      text("p", "拖动或用 ↑ / ↓ 调整分组顺序（同优先级的出口整体移动），保存后由服务端按组数选步长重算数值。", {
        class: "hint",
      }),
      nodes.groupList,
      nodes.groupActions,
    ]),
  );
  root.appendChild(section("新增出口", [createForm()]));
}

function tableShell() {
  const built = table(
    ["名称", "类型", "地址", "优先级", "启用", "熔断", "成功率", "最近成功", "操作"],
    [],
  );
  built.replaceChild(nodes.tableBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function createForm() {
  const name = input("new-name", {
    type: "text",
    required: true,
    maxlength: 64,
    pattern: NAME_PATTERN,
    placeholder: "home-proxy",
  });
  const type = select(
    "new-type",
    [
      { value: "http", label: "http（上级代理）" },
      { value: "direct", label: "direct（本机直连）" },
    ],
    "http",
  );
  const address = input("new-address", {
    type: "text",
    maxlength: 255,
    placeholder: "198.51.100.100:7890",
  });
  const priority = input("new-priority", { type: "number", min: 1, max: 999, value: 100 });
  const enabled = input("new-enabled", { type: "checkbox", checked: true });
  const form = el("form", { class: "form-grid" }, [
    field("名称", name, "字母或数字开头，可含 . _ -"),
    field("类型", type),
    field("地址", address, "http 类型写成 host:port；direct 类型留空"),
    field("优先级", priority, "1–999，越小越优先"),
    field("启用", enabled),
    el("div", { class: "field" }, [
      el("button", { type: "submit", class: "primary" }, ["新增"]),
    ]),
  ]);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void create({
      name: name.value.trim(),
      type: type.value,
      address: address.value.trim() || null,
      priority: Number(priority.value),
      enabled: enabled.checked,
    }, form);
  });
  return form;
}

async function refresh() {
  const [status, list] = await Promise.all([api.status(), api.upstreams()]);
  state.version = status.config_version;
  state.upstreams = list.upstreams;
  // 有人正在编辑某一行、或有未保存的顺序调整时不重画：重画会把已经键入的内容
  // 连同光标一起抹掉，而轮询每 3 秒来一次，等于让编辑根本无法完成。
  if (state.editing === null) renderTable();
  if (state.pendingGroups === null) renderGroups(groupsOf(state.upstreams));
}

function renderTable() {
  const body = clear(nodes.tableBody);
  if (state.upstreams.length === 0) {
    body.appendChild(emptyRow(COLUMNS, "还没有出口"));
    return;
  }
  for (const item of state.upstreams) {
    body.appendChild(state.editing === item.name ? editorRow(item) : displayRow(item));
  }
}

function displayRow(item) {
  const health = circuit(item.health.circuit_state);
  return el("tr", {}, [
    td(item.has_auth ? `${item.name}（含凭据）` : item.name),
    td(item.type),
    td(item.address),
    td(item.priority),
    el("td", {}, [badge(item.enabled ? "启用" : "停用", item.enabled ? "ok" : "muted")]),
    el("td", {}, [badge(health.label, health.kind)]),
    td(percent(item.health.success_rate)),
    td(ago(item.health.last_success_age_seconds)),
    el("td", { class: "actions" }, [
      button("编辑", () => {
        state.editing = item.name;
        renderTable();
      }),
      button(item.enabled ? "禁用" : "启用", () =>
        void save(item.name, { enabled: !item.enabled }),
      ),
      button("测试", () => void probe(item.name)),
      button("删除", () => void remove(item.name), { class: "danger" }),
    ]),
  ]);
}

function editorRow(item) {
  const address = input(`edit-address-${item.name}`, {
    type: "text",
    maxlength: 255,
    value: item.address ?? "",
  });
  const priority = input(`edit-priority-${item.name}`, {
    type: "number",
    min: 1,
    max: 999,
    value: item.priority,
  });
  const enabled = input(`edit-enabled-${item.name}`, {
    type: "checkbox",
    checked: item.enabled,
  });
  return el("tr", { class: "editing" }, [
    td(item.name),
    td(item.type),
    el("td", {}, [address]),
    el("td", {}, [priority]),
    el("td", {}, [enabled]),
    td("—"),
    td("—"),
    td("—"),
    el("td", { class: "actions" }, [
      button(
        "保存",
        () =>
          void save(
            item.name,
            {
              address: address.value.trim() || null,
              priority: Number(priority.value),
              enabled: enabled.checked,
            },
            item.type,
          ),
        { class: "primary" },
      ),
      button("取消", () => {
        state.editing = null;
        renderTable();
      }),
    ]),
  ]);
}

/** 按优先级分组，组内保持列表顺序。列表本身已按优先级升序。*/
function groupsOf(upstreams) {
  const groups = [];
  let currentPriority = null;
  for (const item of upstreams) {
    if (item.priority !== currentPriority) {
      groups.push([]);
      currentPriority = item.priority;
    }
    groups[groups.length - 1].push(item.name);
  }
  return groups;
}

function renderGroups(groups) {
  const list = clear(nodes.groupList);
  groups.forEach((group, index) => {
    // draggable 是枚举属性而非布尔属性：写成空串会退回 auto（不可拖）。
    const item = el("li", { class: "group-item", draggable: "true" }, [
      text("span", `${index + 1}`, { class: "group-order" }),
      text("span", group.join("、"), { class: "group-names" }),
      el("span", { class: "actions" }, [
        button("↑", () => moveGroup(groups, index, -1), {
          disabled: index === 0,
          "aria-label": "上移一组",
        }),
        button("↓", () => moveGroup(groups, index, 1), {
          disabled: index === groups.length - 1,
          "aria-label": "下移一组",
        }),
      ]),
    ]);
    item.addEventListener("dragstart", () => {
      state.dragIndex = index;
    });
    item.addEventListener("dragover", (event) => event.preventDefault());
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      if (state.dragIndex === null || state.dragIndex === index) return;
      const reordered = groups.slice();
      const [moved] = reordered.splice(state.dragIndex, 1);
      reordered.splice(index, 0, moved);
      state.dragIndex = null;
      stageGroups(reordered);
    });
    list.appendChild(item);
  });
  renderGroupActions(groups);
}

function renderGroupActions(groups) {
  const bar = clear(nodes.groupActions);
  if (state.pendingGroups === null) {
    bar.appendChild(text("span", "顺序与当前配置一致。", { class: "muted" }));
    return;
  }
  bar.appendChild(
    button("保存顺序", () => void savePriorities(groups), { class: "primary" }),
  );
  bar.appendChild(
    button("撤销", () => {
      state.pendingGroups = null;
      renderGroups(groupsOf(state.upstreams));
    }),
  );
  bar.appendChild(text("span", "未保存的顺序调整", { class: "warn-text" }));
}

function moveGroup(groups, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= groups.length) return;
  const reordered = groups.slice();
  [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
  stageGroups(reordered);
}

function stageGroups(groups) {
  state.pendingGroups = groups;
  renderGroups(groups);
}

async function savePriorities(groups) {
  try {
    const result = await api.setPriorities({ config_version: state.version, groups });
    state.version = result.config_version;
    state.pendingGroups = null;
    notify(nodes.notice, "ok", "优先级已重算并生效。");
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

/** 挡住漏写端口这一最常见手误，省一次往返。地址合法性仍以后端校验为准。 */
function addressComplaint(type, address) {
  if (type !== "http") return null;
  if (!address) return "http 类型必须填写地址。";
  if (!PORT_SUFFIX.test(address)) {
    return "地址缺少端口，需写成 host:port，如 192.168.1.100:8080。";
  }
  return null;
}

async function create(body, form) {
  const complaint = addressComplaint(body.type, body.address);
  if (complaint) {
    notify(nodes.notice, "bad", complaint);
    return;
  }
  try {
    const result = await api.createUpstream({ ...body, config_version: state.version });
    state.version = result.config_version;
    form.reset();
    notify(nodes.notice, "ok", `已新增出口 ${body.name}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function save(name, changes, type) {
  const complaint = addressComplaint(type, changes.address);
  if (complaint) {
    notify(nodes.notice, "bad", complaint);
    return;
  }
  try {
    const result = await api.updateUpstream(name, { ...changes, config_version: state.version });
    state.version = result.config_version;
    state.editing = null;
    notify(nodes.notice, "ok", `已更新 ${name}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function remove(name) {
  if (!confirmAction(`删除出口 ${name}？被规则引用时会被拒绝。`)) return;
  try {
    const result = await api.deleteUpstream(name, state.version);
    state.version = result.config_version;
    notify(nodes.notice, "ok", `已删除 ${name}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function probe(name) {
  notify(nodes.notice, "info", `正在探测 ${name}…`);
  try {
    const result = await api.probeUpstream(name);
    const outcome = result.ok ? "连通" : `失败：${result.error ?? result.http_status}`;
    notify(
      nodes.notice,
      result.ok ? "ok" : "bad",
      `${name} → ${result.target}：${outcome}，耗时 ${millis(result.elapsed_ms)}。探测不影响健康状态。`,
    );
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function unmount() {
  state.editing = null;
  state.pendingGroups = null;
  state.dragIndex = null;
  clearNotice(nodes.notice);
}

export const page = { id: "upstreams", title: "上级代理", mount, refresh, unmount };
