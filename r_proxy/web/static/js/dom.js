/**
 * 建节点的唯一入口。转义机制在这里，不在调用方的纪律里。
 *
 * 对应设计：docs/design/DD_WEB.md §10.3。
 *
 * 本模块不提供任何能解析 HTML 的入口：业务代码拿不到 innerHTML，因为它连
 * 字符串模板都不用。日志里的 host 与 URL 来自不可信流量，任何一处解析 HTML
 * 的赋值都是 XSS。
 */

// href / src 能装 javascript: 与 data:text/html；style 能通过 url() 拉外部资源；
// on* 直接就是脚本。禁掉整类属性比逐处判断 URL 协议可靠——判断要处理
// "\tjavascript:"、"JaVaScRiPt:" 等一长串变体。
const FORBIDDEN_ATTRS = /^(on|href$|src$|srcdoc$|formaction$|style$)/i;

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (FORBIDDEN_ATTRS.test(name)) {
      // 抛而不是忽略：忽略会让「链接点不动」变成难查的功能 bug，抛异常第一次
      // 打开页面就暴露。
      throw new Error(`属性 ${name} 可执行脚本或加载外部资源，不允许动态设置`);
    }
    if (value === true) {
      node.setAttribute(name, "");
    } else if (value !== false && value !== null && value !== undefined) {
      node.setAttribute(name, String(value));
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(
      typeof child === "object" ? child : document.createTextNode(String(child)),
    );
  }
  return node;
}

/** 文本节点容器。`null` 显示为破折号：空单元格分不清「没这个字段」与「值是空串」。 */
export function td(value, attrs = {}) {
  const cell = el("td", attrs);
  cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return cell;
}

export function th(value, attrs = {}) {
  const cell = el("th", { scope: "col", ...attrs });
  cell.textContent = String(value);
  return cell;
}

export function text(tag, value, attrs = {}) {
  const node = el(tag, attrs);
  node.textContent = value === null || value === undefined ? "" : String(value);
  return node;
}

export function button(label, onClick, attrs = {}) {
  const node = el("button", { type: "button", ...attrs });
  node.textContent = label;
  node.addEventListener("click", onClick);
  return node;
}

export function table(headers, rows, attrs = {}) {
  const head = el(
    "thead",
    {},
    [el("tr", {}, headers.map((h) => th(h)))],
  );
  const body = el("tbody", {}, rows);
  return el("table", attrs, [head, body]);
}

/** 空表提示。列数要跟表头一致，否则边框会错位。*/
export function emptyRow(columns, message) {
  return el("tr", {}, [td(message, { colspan: columns, class: "empty" })]);
}

export function badge(label, kind) {
  return text("span", label, { class: `badge badge-${kind}` });
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function field(labelText, control, hint) {
  const id = control.getAttribute("id");
  const label = text("label", labelText, id ? { for: id } : {});
  const parts = [label, control];
  if (hint) parts.push(text("p", hint, { class: "hint" }));
  return el("div", { class: "field" }, parts);
}

export function input(id, attrs = {}) {
  return el("input", { id, name: id, ...attrs });
}

export function select(id, options, selected) {
  const node = el("select", { id, name: id });
  for (const option of options) {
    const item = text("option", option.label, { value: option.value });
    if (option.value === selected) item.setAttribute("selected", "");
    node.appendChild(item);
  }
  return node;
}
