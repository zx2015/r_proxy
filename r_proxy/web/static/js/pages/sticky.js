/**
 * 粘性映射与负面记忆：搜索、排序、手动改绑、单条与批量清除、解除屏蔽。
 *
 * 对应需求：WEBUI_SPEC.md §2.3、§3.3。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, input, select, table, td, text } from "../dom.js";
import { ago, duration } from "../format.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const PAGE_SIZE = 50;
const STICKY_COLUMNS = 7;
const BLOCK_COLUMNS = 5;

const nodes = {};
const state = { page: 1, total: 0, upstreams: [], items: [], binding: null, promoting: null };

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.stickyBody = el("tbody");
  nodes.blockBody = el("tbody");
  nodes.pageLabel = text("span", "", { class: "muted" });

  root.appendChild(nodes.notice);
  root.appendChild(section("粘性映射", [searchBar(), stickyShell(), pager(), batchBar()]));
  root.appendChild(
    section("负面记忆", [
      text("p", "被判定为 route_error 的 (host, 出口) 组合，到期后自动恢复。", { class: "hint" }),
      blockShell(),
    ]),
  );
}

function stickyShell() {
  const built = table(
    ["host", "出口", "来源", "命中", "失败", "最近使用", "操作"],
    [],
  );
  built.replaceChild(nodes.stickyBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function blockShell() {
  const built = table(["host", "出口", "失败次数", "原因", "剩余屏蔽"], []);
  built.replaceChild(nodes.blockBody, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function searchBar() {
  nodes.query = input("sticky-q", { type: "text", placeholder: "host 包含…", maxlength: 255 });
  nodes.upstreamFilter = input("sticky-upstream", { type: "text", placeholder: "出口名", maxlength: 64 });
  nodes.sort = select(
    "sticky-sort",
    [
      { value: "recent", label: "按最近使用" },
      { value: "fails", label: "按失败次数" },
    ],
    "recent",
  );
  // 换条件、换排序、翻页都用 closePanel 收尾：列表内容要变了，展开中的面板留在
  // 屏幕上没有意义，而 refresh() 在面板开着时不重画——直接调它会「点了没反应」。
  nodes.sort.addEventListener("change", () => {
    state.page = 1;
    closePanel();
  });
  return toolbar([
    labelled("搜索", nodes.query),
    labelled("出口", nodes.upstreamFilter),
    labelled("排序", nodes.sort),
    button("查询", () => {
      state.page = 1;
      closePanel();
    }, { class: "primary" }),
  ]);
}

function batchBar() {
  nodes.batchUpstream = input("batch-upstream", { type: "text", placeholder: "出口名", maxlength: 64 });
  return toolbar([
    labelled("批量清除某出口的全部绑定", nodes.batchUpstream),
    button("清除", () => void clearByUpstream(), { class: "danger" }),
  ]);
}

function labelled(labelText, control) {
  const label = text("label", labelText, { for: control.getAttribute("id") });
  return el("span", { class: "inline-field" }, [label, control]);
}

function pager() {
  nodes.prev = button("上一页", () => {
    if (state.page <= 1) return;
    state.page -= 1;
    closePanel();
  });
  nodes.next = button("下一页", () => {
    state.page += 1;
    closePanel();
  });
  return toolbar([nodes.prev, nodes.pageLabel, nodes.next]);
}

async function refresh() {
  const [list, blocks, upstreams] = await Promise.all([
    api.sticky({
      q: nodes.query.value.trim(),
      upstream: nodes.upstreamFilter.value.trim(),
      sort: nodes.sort.value,
      page: state.page,
      page_size: PAGE_SIZE,
    }),
    api.routeBlocks({ page: 1, page_size: PAGE_SIZE }),
    api.upstreams(),
  ]);
  state.total = list.total;
  state.upstreams = upstreams.upstreams;
  state.items = list.items;
  // 改绑或固化面板展开时不重画：重画会关掉刚打开的下拉框、清掉改了一半的条件。
  // 面板的打开因此**不能**走这里，见 openPanel。
  if (state.binding === null && state.promoting === null) renderSticky(list.items);
  renderBlocks(blocks.items);
  nodes.pageLabel.textContent = `第 ${list.page} 页 / 共 ${list.total} 条`;
  nodes.prev.disabled = list.page <= 1;
  nodes.next.disabled = list.page * list.page_size >= list.total;
}

function renderSticky(items) {
  const body = clear(nodes.stickyBody);
  if (items.length === 0) {
    body.appendChild(emptyRow(STICKY_COLUMNS, "没有粘性映射"));
    return;
  }
  for (const item of items) {
    body.appendChild(rowFor(item));
  }
}

function rowFor(item) {
  if (state.binding === item.host) return bindRow(item);
  if (state.promoting === item.host) return promoteRow(item);
  return stickyRow(item);
}

/**
 * 展开某一行的改绑或固化面板。
 *
 * 直接重画，**不走 `refresh()`**：那里为了不冲掉展开中的面板会跳过重画，于是
 * 「设状态 → refresh」这条路径画不出刚打开的面板——点击看起来毫无反应。数据本来
 * 就在屏幕上，打开面板也不需要再拉一次。
 *
 * 两个面板互斥：另一个还开着时打开这个，那个自然关掉。
 */
function openPanel(host, mode) {
  state.binding = mode === "bind" ? host : null;
  state.promoting = mode === "promote" ? host : null;
  renderSticky(state.items);
}

function closePanel() {
  state.binding = null;
  state.promoting = null;
  void refresh();
}

function stickyRow(item) {
  return el("tr", {}, [
    td(item.host),
    td(item.upstream),
    el("td", {}, [badge(sourceLabel(item.source), sourceKind(item.source))]),
    td(item.hit_count),
    td(item.fail_count),
    td(ago(item.last_used_age_seconds)),
    el("td", { class: "actions" }, [
      button("改绑", () => openPanel(item.host, "bind")),
      button("固化", () => openPanel(item.host, "promote"), {
        title: "写成一条规则，此后强制走该出口",
      }),
      button("清除", () => void clearOne(item.host), { class: "danger" }),
    ]),
  ]);
}

function bindRow(item) {
  const picker = select(
    `bind-${item.host}`,
    state.upstreams
      .filter((candidate) => candidate.enabled)
      .map((candidate) => ({ value: candidate.name, label: candidate.name })),
    item.upstream,
  );
  return el("tr", { class: "editing" }, [
    td(item.host),
    el("td", { colspan: 5 }, [picker]),
    el("td", { class: "actions" }, [
      button("绑定", () => void bind(item.host, picker.value), { class: "primary" }),
      button("取消", closePanel),
    ]),
  ]);
}

/**
 * 固化面板：条件默认取 host，可改成域名及子域。
 *
 * 面板本身就是确认环节，不再弹 window.confirm——那会把最要紧的一句提示挤进一个
 * 没法排版的系统对话框里。
 */
function promoteRow(item) {
  const condition = input(`promote-${item.host}`, {
    type: "text",
    value: item.host,
    maxlength: 1000,
  });
  const suffix = suffixFor(item.host);
  return el("tr", { class: "editing" }, [
    td(item.host),
    el("td", { colspan: 5 }, [
      condition,
      suffix
        ? button(`改为 ${suffix}`, () => {
            condition.value = suffix;
          })
        : null,
      text(
        "p",
        `写成规则表的第 1 条：命中后强制走 ${item.upstream}，失败不再自动切换；` +
          "本条粘性映射同时清除。",
        { class: "hint" },
      ),
    ]),
    el("td", { class: "actions" }, [
      button("固化", () => void promote(item, condition.value.trim()), { class: "primary" }),
      button("取消", closePanel),
    ]),
  ]);
}

/**
 * 「域名及子域」的快捷条件。三段以上取上级域（`api.github.com` → `*.github.com`），
 * 两段取自身（`example.com` → `*.example.com`，后缀匹配含 apex）。
 *
 * IP 字面量没有域名层级，不给这个快捷项：`*.1.10` 之类的条件既不会命中 IP，
 * 也可能误伤别的域名。公共后缀同样判不出来（没有 PSL），因此只降一级——由用户
 * 决定要不要再往上改。
 */
function suffixFor(host) {
  if (host.includes(":") || !host.includes(".") || /^[0-9.]+$/.test(host)) return null;
  const parts = host.split(".");
  return `*.${parts.length >= 3 ? parts.slice(1).join(".") : host}`;
}

function sourceLabel(source) {
  return source === "manual" ? "手动" : "自动";
}

function sourceKind(source) {
  return source === "manual" ? "ok" : "info";
}

function renderBlocks(items) {
  const body = clear(nodes.blockBody);
  if (items.length === 0) {
    body.appendChild(emptyRow(BLOCK_COLUMNS, "没有负面记忆"));
    return;
  }
  for (const item of items) {
    body.appendChild(
      el("tr", {}, [
        td(item.host),
        td(item.upstream),
        td(item.fail_count),
        td(item.reason),
        el("td", { class: "actions" }, [
          text("span", duration(item.expires_in_seconds), { class: "muted" }),
          button("解除", () => void unblock(item.host, item.upstream)),
        ]),
      ]),
    );
  }
}

async function bind(host, upstream) {
  try {
    await api.bindSticky(host, upstream);
    state.binding = null;
    notify(nodes.notice, "ok", `已把 ${host} 手动绑定到 ${upstream}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function promote(item, condition) {
  if (!condition) {
    notify(nodes.notice, "bad", "条件不能为空。");
    return;
  }
  try {
    const result = await api.promoteSticky(item.host, { condition, upstream: item.upstream });
    state.promoting = null;
    const kind = result.rules_enabled ? "ok" : "warn";
    notify(nodes.notice, kind, promoteMessage(condition, item, result));
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

/** 一次说清三件事：规则落在哪、它盖住了谁、规则功能是不是开着。*/
function promoteMessage(condition, item, result) {
  const parts = [`已固化为 rules[${result.position}]：${condition} → ${item.upstream}。`];
  if (result.previous_match) {
    parts.push(
      `原先命中的 rules[${result.previous_match.position}]（${result.previous_match.condition}）` +
        "对该 host 不再生效。",
    );
  }
  if (!result.rules_enabled) parts.push("注意：规则功能当前已关闭，这条规则暂不生效。");
  return parts.join("");
}

async function clearOne(host) {
  if (!confirmAction(`清除 ${host} 的绑定？下次请求会重新按优先级选路。`)) return;
  try {
    await api.clearSticky(host);
    notify(nodes.notice, "ok", `已清除 ${host}。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function clearByUpstream() {
  const upstream = nodes.batchUpstream.value.trim();
  if (!upstream) {
    notify(nodes.notice, "bad", "请填写出口名。空条件的批量清除会被服务端拒绝。");
    return;
  }
  if (!confirmAction(`清除绑定到 ${upstream} 的全部映射？`)) return;
  try {
    const result = await api.clearStickyBatch({ upstream });
    nodes.batchUpstream.value = "";
    notify(nodes.notice, "ok", `已清除 ${result.cleared} 条绑定。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

async function unblock(host, upstream) {
  try {
    await api.clearRouteBlock(host, upstream);
    notify(nodes.notice, "ok", `已解除 ${host} 上对 ${upstream} 的屏蔽。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function unmount() {
  state.binding = null;
  state.promoting = null;
  state.page = 1;
  clearNotice(nodes.notice);
}

export const page = { id: "sticky", title: "粘性映射", mount, refresh, unmount };
