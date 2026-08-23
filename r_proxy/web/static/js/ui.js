/**
 * 全局提示、确认与 token 面板。DOM 骨架在 index.html 里。
 *
 * 对应设计：docs/design/DD_WEB.md §10.5、§10.6。
 */

import { ApiError, setToken } from "./api.js";
import { el, clear } from "./dom.js";

const banner = () => document.getElementById("banner");

export function showBanner(kind, message) {
  const node = banner();
  node.className = `banner banner-${kind}`;
  node.textContent = message;
  node.hidden = false;
}

export function hideBanner() {
  const node = banner();
  node.hidden = true;
  node.textContent = "";
}

/** 一次性提示，挂在指定容器里，不遮挡内容。*/
export function notify(container, kind, message) {
  const box = clear(container);
  box.className = `notice notice-${kind}`;
  box.textContent = message;
  box.hidden = false;
}

export function clearNotice(container) {
  clear(container);
  container.hidden = true;
}

export function describeError(err) {
  if (err instanceof ApiError) {
    const detail = detailText(err.details);
    return detail ? `${err.message}（${detail}）` : err.message;
  }
  return err && err.message ? err.message : String(err);
}

/**
 * 把服务端的 details 压成一行。三种形状都要认：规则/配置校验是问题数组、
 * 版本冲突是 {expected, actual}、引用检查是 {file, line} 数组。
 */
function detailText(details) {
  if (!details) return "";
  if (Array.isArray(details)) {
    return details
      .map((item) => {
        if (item.location) return `${item.location}: ${item.message}`;
        if (item.file) return `${item.file}:${item.line}`;
        return JSON.stringify(item);
      })
      .join("；");
  }
  if (details.actual) return `磁盘上的版本 ${details.actual}`;
  return "";
}

export function confirmAction(message) {
  return window.confirm(message);
}

/**
 * 弹出 token 输入面板，resolve 为「是否拿到了新 token」。
 *
 * 只在收到 401 之后调用，不在启动时无条件索要：默认部署（回环、未配置 token）
 * 不该被一个输入框拦住。
 */
export function requestToken() {
  const dialog = document.getElementById("token-dialog");
  const form = document.getElementById("token-form");
  const field = document.getElementById("token-input");
  if (!dialog.hidden) return Promise.resolve(false);

  dialog.hidden = false;
  field.value = "";
  field.focus();

  return new Promise((resolve) => {
    const submit = (event) => {
      event.preventDefault();
      const value = field.value.trim();
      if (!value) return;
      setToken(value);
      close();
      resolve(true);
    };
    const cancel = (event) => {
      if (event.key !== "Escape") return;
      close();
      resolve(false);
    };
    const close = () => {
      form.removeEventListener("submit", submit);
      document.removeEventListener("keydown", cancel);
      dialog.hidden = true;
      field.value = "";
    };
    form.addEventListener("submit", submit);
    document.addEventListener("keydown", cancel);
  });
}

export function section(title, children) {
  const heading = el("h2");
  heading.textContent = title;
  return el("section", { class: "panel" }, [heading, ...[].concat(children)]);
}

export function toolbar(children) {
  return el("div", { class: "toolbar" }, children);
}
