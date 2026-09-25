/**
 * 监控看板：概览、出口健康、请求日志、切换事件流。
 *
 * 对应需求：WEBUI_SPEC.md §2.1。
 *
 * 三块数据来自三个端点，一轮里并发发出；失败的那块单独降级，不影响另两块。
 */

import { api } from "../api.js";
import { badge, button, clear, el, emptyRow, input, table, td, text } from "../dom.js";
import { ago, bytes, circuit, duration, millis, percent, timestamp } from "../format.js";
import { clearNotice, confirmAction, describeError, notify, section, toolbar } from "../ui.js";

const PAGE_SIZE = 50;
const LOG_COLUMNS = 9;
const HEALTH_COLUMNS = 9;
const SWITCH_COLUMNS = 5;
const TRAFFIC_COLUMNS = 4;
// 主机流量榜是一次聚合查询，成本明显高于其余板块，独立走 30 秒一档，
// 不跟随全局的 3 秒轮询（WEBUI_SPEC §2.1、DD_WEB §10.6）。
const TRAFFIC_REFRESH_MS = 30000;

const nodes = {};
const filters = { host: "", upstream: "", clientAddr: "", status: "", page: 1 };
const traffic = { lastFetchedAt: 0 };

function mount(root) {
  nodes.notice = el("div", { hidden: true });
  nodes.cards = el("div", { class: "cards" });
  nodes.healthBody = el("tbody");
  nodes.logBody = el("tbody");
  nodes.switchBody = el("tbody");
  nodes.trafficBody = el("tbody");
  nodes.pageLabel = text("span", "第 1 页", { class: "muted" });

  root.appendChild(nodes.notice);
  root.appendChild(section("概览", nodes.cards));
  root.appendChild(
    section("出口健康", [
      shell(
        ["出口", "优先级", "熔断", "连续失败", "累计成功", "累计失败", "成功率", "流量", "操作"],
        nodes.healthBody,
      ),
    ]),
  );
  root.appendChild(section("请求日志", [filterForm(), logShell(), pager()]));
  root.appendChild(
    section("切换事件", [
      text("p", "只列出尝试过多个出口的请求。", { class: "hint" }),
      shell(["时间", "方法", "host", "切换路径", "结果"], nodes.switchBody),
    ]),
  );
  root.appendChild(
    section("主机流量榜（当日）", [
      text("p", "按今日总流量降序，每 30 秒刷新一次。", { class: "hint" }),
      shell(["host", "上行", "下行", "请求数"], nodes.trafficBody),
    ]),
  );
}

function shell(headers, body) {
  const built = table(headers, []);
  built.replaceChild(body, built.tBodies[0]);
  return el("div", { class: "table-wrap" }, [built]);
}

function logShell() {
  return shell(
    ["时间", "客户端", "host", "方法", "出口", "来源", "状态/错误", "耗时", "流量"],
    nodes.logBody,
  );
}

function filterForm() {
  nodes.host = input("filter-host", { type: "text", placeholder: "example.com", maxlength: 255 });
  nodes.upstream = input("filter-upstream", { type: "text", placeholder: "出口名", maxlength: 64 });
  nodes.clientAddr = input("filter-client-addr", {
    type: "text",
    placeholder: "203.0.113.1",
    maxlength: 255,
  });
  nodes.status = input("filter-status", { type: "number", min: 100, max: 599, placeholder: "200" });
  const apply = button("筛选", () => {
    filters.host = nodes.host.value.trim();
    filters.upstream = nodes.upstream.value.trim();
    filters.clientAddr = nodes.clientAddr.value.trim();
    filters.status = nodes.status.value.trim();
    filters.page = 1;
    void refresh();
  }, { class: "primary" });
  const reset = button("清空", () => {
    nodes.host.value = "";
    nodes.upstream.value = "";
    nodes.clientAddr.value = "";
    nodes.status.value = "";
    filters.host = filters.upstream = filters.clientAddr = filters.status = "";
    filters.page = 1;
    void refresh();
  });
  return toolbar([
    labelled("host", nodes.host),
    labelled("出口", nodes.upstream),
    labelled("客户端", nodes.clientAddr),
    labelled("状态码", nodes.status),
    apply,
    reset,
  ]);
}

function labelled(labelText, control) {
  const label = text("label", labelText, { for: control.getAttribute("id") });
  return el("span", { class: "inline-field" }, [label, control]);
}

function pager() {
  const prev = button("上一页", () => {
    if (filters.page <= 1) return;
    filters.page -= 1;
    void refresh();
  });
  const next = button("下一页", () => {
    filters.page += 1;
    void refresh();
  });
  nodes.prev = prev;
  nodes.next = next;
  return toolbar([prev, nodes.pageLabel, next]);
}

async function refresh() {
  const results = await Promise.allSettled([
    api.status(),
    api.health(),
    api.logs(logParams()),
    api.switches({ page: 1, page_size: 20 }),
  ]);
  const [status, health, logs, switches] = results;

  if (status.status === "fulfilled") renderCards(status.value);
  if (health.status === "fulfilled") renderHealth(health.value.upstreams);
  if (logs.status === "fulfilled") renderLogs(logs.value);
  if (switches.status === "fulfilled") renderSwitches(switches.value.items);

  const failure = results.find((result) => result.status === "rejected");

  // 独立节流，不占用上面那组结果的失败判定：这一块慢或暂时失败，不该让
  // 整轮 refresh() 被标记为失败（那会盖掉概览/健康/日志已经成功的数据）。
  if (Date.now() - traffic.lastFetchedAt >= TRAFFIC_REFRESH_MS) {
    traffic.lastFetchedAt = Date.now();
    try {
      renderTraffic((await api.hostTraffic({ limit: 20 })).items);
    } catch (err) {
      // 失败不清空表格：保留上一次的榜单，与其余板块的降级方式一致。
    }
  }

  if (failure) throw failure.reason;
}

function logParams() {
  return {
    host: filters.host,
    upstream: filters.upstream,
    client_addr: filters.clientAddr,
    status: filters.status,
    page: filters.page,
    page_size: PAGE_SIZE,
  };
}

function renderCards(status) {
  const items = [
    ["出口尝试总数", String(status.requests.attempts)],
    ["成功率", percent(status.requests.success_rate)],
    ["活跃连接", `${status.connections.active} / ${status.connections.limit}`],
    ["拒绝连接", String(status.connections.rejected)],
    ["运行时长", duration(status.uptime_seconds)],
    ["监听地址", `${status.proxy.host}:${status.proxy.port}`],
    ["写队列", `${status.storage.queue_size} / ${status.storage.queue_capacity}`],
    ["队列峰值", String(status.storage.queue_high_water)],
    ["落盘 p99", millis(status.storage.flush_duration_p99_ms)],
    ["写入错误", String(status.storage.write_errors)],
  ];
  const container = clear(nodes.cards);
  for (const [label, value] of items) {
    container.appendChild(
      el("div", { class: "card" }, [
        text("span", label, { class: "card-label" }),
        text("strong", value, { class: "card-value" }),
      ]),
    );
  }
}

function renderHealth(upstreams) {
  const body = clear(nodes.healthBody);
  if (upstreams.length === 0) {
    body.appendChild(emptyRow(HEALTH_COLUMNS, "没有配置出口"));
    return;
  }
  for (const item of upstreams) {
    const state = circuit(item.circuit_state);
    const reset = button("重置熔断", () => void resetCircuit(item.name), {
      disabled: item.circuit_state === "closed",
    });
    body.appendChild(
      el("tr", {}, [
        td(item.enabled ? item.name : `${item.name}（已禁用）`),
        td(item.priority),
        el("td", {}, [badge(state.label, state.kind)]),
        td(item.consecutive_failures),
        td(item.total_success),
        td(item.total_failure),
        td(percent(item.success_rate)),
        td(`${bytes(item.bytes_up_total)} ↑ / ${bytes(item.bytes_down_total)} ↓`),
        el("td", {}, [reset]),
      ]),
    );
  }
}

async function resetCircuit(name) {
  if (!confirmAction(`重置出口 ${name} 的熔断状态？累计计数会保留。`)) return;
  try {
    await api.resetCircuit(name);
    notify(nodes.notice, "ok", `已重置 ${name} 的熔断状态。`);
    await refresh();
  } catch (err) {
    notify(nodes.notice, "bad", describeError(err));
  }
}

function renderLogs(page) {
  const body = clear(nodes.logBody);
  if (page.items.length === 0) {
    body.appendChild(emptyRow(LOG_COLUMNS, "暂无请求日志"));
  }
  for (const item of page.items) {
    body.appendChild(
      el("tr", {}, [
        td(timestamp(item.created_at)),
        td(item.client_addr),
        td(item.host),
        td(item.method),
        td(item.upstream_name),
        td(item.decision_source),
        td(item.error ?? item.http_status),
        td(millis(item.elapsed_ms)),
        td(trafficCell(item)),
      ]),
    );
  }
  nodes.pageLabel.textContent = `第 ${page.page} 页`;
  nodes.prev.disabled = page.page <= 1;
  nodes.next.disabled = !page.has_more;
}

// 传输量来自与 traffic_log 的联表，`null` 表示这次尝试从未传输过数据
// （被切换掉）或响应体仍在流式转发中——与「确实传输了 0 字节」是两回事，
// 因此展示「—」而不是「0 B」，避免看起来像是统计坏掉了。
function trafficCell(item) {
  if (item.traffic_bytes_up === null || item.traffic_bytes_down === null) return "—";
  return `${bytes(item.traffic_bytes_up)} / ${bytes(item.traffic_bytes_down)}`;
}

function renderSwitches(items) {
  const body = clear(nodes.switchBody);
  if (items.length === 0) {
    body.appendChild(emptyRow(SWITCH_COLUMNS, "没有发生过切换"));
    return;
  }
  for (const item of items) {
    const last = item.attempts[item.attempts.length - 1];
    body.appendChild(
      el("tr", {}, [
        td(timestamp(item.attempts[0].created_at)),
        td(item.method),
        td(item.host),
        td(item.attempts.map(describeAttempt).join(" → ")),
        td(last.error ?? last.http_status),
      ]),
    );
  }
}

function describeAttempt(attempt) {
  const priority = attempt.upstream_priority === null ? "" : `(P${attempt.upstream_priority})`;
  const outcome = attempt.error ?? attempt.http_status ?? "无响应";
  return `${attempt.upstream_name}${priority} ${outcome}`;
}

function renderTraffic(items) {
  const body = clear(nodes.trafficBody);
  if (items.length === 0) {
    body.appendChild(emptyRow(TRAFFIC_COLUMNS, "今日暂无流量"));
    return;
  }
  for (const item of items) {
    body.appendChild(
      el("tr", {}, [
        td(item.host),
        td(bytes(item.bytes_up)),
        td(bytes(item.bytes_down)),
        td(item.requests),
      ]),
    );
  }
}

function unmount() {
  clearNotice(nodes.notice);
}

export const page = { id: "dashboard", title: "监控看板", mount, refresh, unmount };
