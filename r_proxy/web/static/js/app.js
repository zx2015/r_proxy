/**
 * 入口：导航、页面装载、轮询调度、401 处理。
 *
 * 对应设计：docs/design/DD_WEB.md §10.2、§10.6。
 *
 * 定时器只有这一个。页面自己不持有定时器——忘记清理是这类界面最常见的泄漏，
 * 表现为切几次页面后请求频率翻倍。
 */

import { ApiError, api, hasToken, setToken } from "./api.js";
import * as router from "./router.js";
import { describeError, hideBanner, requestToken, showBanner } from "./ui.js";
import { clear } from "./dom.js";

import { page as dashboard } from "./pages/dashboard.js";
import { page as upstreams } from "./pages/upstreams.js";
import { page as sticky } from "./pages/sticky.js";
import { page as rules } from "./pages/rules.js";
import { page as settings } from "./pages/settings.js";

const POLL_INTERVAL_MS = 3000;

const state = {
  timer: null,
  inFlight: false,
  paused: false,
  askingToken: false,
};

for (const page of [dashboard, upstreams, sticky, rules, settings]) {
  router.register(page);
}

function start() {
  document.getElementById("poll-enabled").addEventListener("change", (event) => {
    state.paused = !event.target.checked;
    updatePollState();
  });
  document.getElementById("refresh-now").addEventListener("click", () => {
    void refresh();
  });
  document.getElementById("forget-token").addEventListener("click", () => {
    setToken(null);
    showBanner("info", "已清除本会话的访问令牌。");
  });
  // 后台标签页里一个忘了关的界面会 3 秒一次永久查库，而管理员根本看不到它。
  document.addEventListener("visibilitychange", updatePollState);
  window.addEventListener("beforeunload", (event) => {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  router.setGuard(confirmLeave);
  router.start(mount);
  state.timer = window.setInterval(tick, POLL_INTERVAL_MS);
  updatePollState();
  void loadVersion();
}

function mount(page, previous) {
  if (previous && previous.unmount) previous.unmount();
  document.getElementById("page-title").textContent = page.title;
  document.title = `${page.title} · r-proxy`;
  for (const link of document.querySelectorAll(".nav-link")) {
    link.classList.toggle("active", link.dataset.page === page.id);
  }
  hideBanner();
  page.mount(clear(document.getElementById("page-root")));
  void refresh();
}

function tick() {
  if (state.paused || document.hidden) return;
  // 有未保存改动时轮询会拿服务端数据盖掉正在编辑的内容。
  if (isDirty()) return;
  void refresh();
}

function isDirty() {
  const page = router.currentPage();
  return page !== null && typeof page.isDirty === "function" && page.isDirty();
}

function confirmLeave(page) {
  if (typeof page.isDirty !== "function" || !page.isDirty()) return true;
  return window.confirm("有未保存的规则改动，离开将丢弃这些改动。确定离开？");
}

async function refresh() {
  // 上一轮没回来就跳过本轮：慢查询下继续叠加只会让队列越来越长。
  if (state.inFlight) return;
  const page = router.currentPage();
  if (page === null || !page.refresh) return;
  state.inFlight = true;
  try {
    await page.refresh();
    hideBanner();
  } catch (err) {
    await handleFailure(err);
  } finally {
    state.inFlight = false;
  }
}

async function handleFailure(err) {
  if (err instanceof ApiError && err.status === 401) {
    if (state.askingToken) return;
    state.askingToken = true;
    try {
      showBanner("warn", "需要访问令牌。");
      if (await requestToken()) {
        state.inFlight = false;
        await refresh();
      }
    } finally {
      state.askingToken = false;
    }
    return;
  }
  if (err instanceof ApiError && err.status === 429) {
    // 自动重试只会把限流窗口一直续上。
    showBanner("bad", "认证失败次数过多，请稍后再试。");
    return;
  }
  // 失败不清空表格：网络抖一下就把满屏数据清空，比显示略旧的数据更糟。
  showBanner("bad", describeError(err));
}

function updatePollState() {
  const label = document.getElementById("poll-state");
  if (state.paused) label.textContent = "自动刷新已暂停";
  else if (document.hidden) label.textContent = "标签页隐藏，已暂停刷新";
  else label.textContent = "自动刷新中";
}

async function loadVersion() {
  try {
    const status = await api.status();
    document.getElementById("brand-version").textContent = `v${status.version}`;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401 && !hasToken()) return;
    document.getElementById("brand-version").textContent = "";
  }
}

start();
