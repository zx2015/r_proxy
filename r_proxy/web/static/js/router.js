/**
 * history 路由。页面列表与服务端的 SPA_PAGES 白名单一一对应
 * （docs/design/DD_WEB.md §10.4）。
 */

const routes = new Map();
let current = null;
let onChange = () => {};
let guard = () => true;

export function register(page) {
  routes.set(page.id, page);
}

/**
 * 离开当前页前的放行判断。返回 false 即留在原页。
 *
 * 存在的理由只有一个：规则页有未保存的整表改动时，切走会静默丢弃它们
 * （WEBUI_SPEC §2.4）。
 */
export function setGuard(fn) {
  guard = fn;
}

export function pageFor(path) {
  const name = path.replace(/^\/+|\/+$/g, "");
  if (name === "") return routes.get("dashboard");
  return routes.get(name) ?? null;
}

export function currentPage() {
  return current;
}

export function start(handler) {
  onChange = handler;
  window.addEventListener("popstate", () => activate(window.location.pathname, false));
  document.addEventListener("click", interceptLinks);
  activate(window.location.pathname, false);
}

export function navigate(path) {
  activate(path, true);
}

function activate(path, push) {
  const page = pageFor(path);
  if (page === null) {
    // 未知路径交给服务端（那里会是 404）。前端伪造一个页面只会掩盖笔误。
    window.location.assign(path);
    return;
  }
  if (current !== null && page !== current && !guard(current)) {
    // 用户选择留下。popstate 已经改过地址栏，推回去，否则地址与内容不符。
    if (!push) window.history.pushState({}, "", pathOf(current));
    return;
  }
  if (push && window.location.pathname !== path) {
    window.history.pushState({}, "", path);
  }
  const previous = current;
  current = page;
  onChange(page, previous);
}

function pathOf(page) {
  return page.id === "dashboard" ? "/" : `/${page.id}`;
}

function interceptLinks(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest("a[data-page]");
  if (link === null) return;
  event.preventDefault();
  navigate(new URL(link.href).pathname);
}
