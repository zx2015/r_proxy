// Execute the shipped page, DOM helpers and API client with a small DOM adapter.
// This checks page state/events; it does not claim browser layout coverage.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

class Node {
  constructor(tag = "#text", value = "") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this._text = value;
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.classList = {
      add: (name) => this.classList.toggle(name, true),
      remove: (name) => this.classList.toggle(name, false),
      toggle: (name, on) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        if (on ?? !names.has(name)) names.add(name);
        else names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map((n) => n.textContent).join(""); }
  setAttribute(name, value) {
    this.attributes.set(name, value);
    if (name === "class") this.className = value;
    if (name === "value") this.value = value;
    if (name === "hidden") this.hidden = true;
    if (name === "checked") this.checked = true;
    if (name === "disabled") this.disabled = true;
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  appendChild(node) {
    node.parentNode = this;
    this.children.push(node);
    if (this.tagName === "SELECT" && (this.children.length === 1 || node.attributes.has("selected"))) {
      this.value = node.value;
    }
    return node;
  }
  replaceChildren(...nodes) {
    for (const node of this.children) node.parentNode = null;
    this.children = [];
    this._text = "";
    for (const node of nodes) this.appendChild(node);
  }
  replaceChild(node, previous) {
    const index = this.children.indexOf(previous);
    assert.ok(index >= 0);
    previous.parentNode = null;
    node.parentNode = this;
    this.children[index] = node;
  }
  get tBodies() { return this.children.filter((n) => n.tagName === "TBODY"); }
  contains(node) { return node === this || this.children.some((n) => n.contains(node)); }
  querySelector(selector) { return all(this).find((n) => selector.startsWith("#") ? n.getAttribute("id") === selector.slice(1) : n.tagName === selector.toUpperCase()) ?? null; }
  addEventListener(type, callback, options = {}) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push({ callback, once: options.once });
    this.listeners.set(type, listeners);
  }
  emit(type, attrs = {}) {
    const event = { type, target: this, relatedTarget: null, preventDefault() {}, ...attrs };
    const targets = [this];
    if (attrs.bubbles) for (let n = this.parentNode; n; n = n.parentNode) targets.push(n);
    for (const target of targets) {
      for (const entry of [...(target.listeners.get(type) ?? [])]) {
        if (entry.once) target.listeners.set(type, target.listeners.get(type).filter((item) => item !== entry));
        entry.callback(event);
      }
    }
  }
  get type() { return this.getAttribute("type"); }
  get ariaExpanded() { return this.getAttribute("aria-expanded"); }
  set ariaExpanded(value) { this.setAttribute("aria-expanded", value); }
  focus() { document.activeElement = this; this.emit("focusin", { bubbles: true }); }
  click() { if (root.contains(this) && !this.disabled) this.emit("click"); }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
}
const all = (node) => [node, ...node.children.flatMap(all)];
const root = new Node("main");
globalThis.document = {
  activeElement: null,
  createElement: (tag) => new Node(tag),
  createTextNode: (value) => new Node("#text", value),
  getElementById: (id) => root.querySelector(`#${id}`),
};
globalThis.window = { confirm: () => true, sessionStorage: { getItem: () => null } };
const scenario = process.argv[2];
const original = scenario === "unchanged" ? 1 : 2592000;
const settings = {
  config_version: "v1", routing: {
    sticky_ttl: original, connect_timeout: 10, read_timeout: 30, sticky_fail_threshold: 3,
    route_block_ttl: 600, tunnel_probe_window: 5, switch_buffer_bytes: 65536, happy_eyeballs_delay: 0.25,
    switch_on_status: [502], status_switch_rate_limit: { max_switches_per_host: 10, window_seconds: 60 },
    circuit_breaker: { enabled: true, fail_threshold: 5, cooldown_seconds: 60 },
  }, storage: { retention_days: 30, max_log_rows: 10000, backup_keep: 5 },
  listen: { listen_host: "127.0.0.1", listen_port: 6060, webui_host: "127.0.0.1", webui_port: 6061 },
  restart_required_fields: [],
};
let sent = null;
globalThis.fetch = async (url, options) => {
  let data;
  if (url === "/api/settings" && options.method === "GET") data = settings;
  else if (url === "/api/settings" && options.method === "PUT") {
    sent = JSON.parse(options.body); settings.routing.sticky_ttl = sent.sticky_ttl; data = { config_version: "v2" };
  } else if (url === "/api/config/backups") data = { backups: [] };
  else if (url.startsWith("/api/audit")) data = { items: [], page: 1, has_more: false };
  else throw new Error(url);
  return { ok: true, status: 200, text: async () => JSON.stringify(data) };
};
const { page } = await import(pathToFileURL(path.resolve("r_proxy/web/static/js/pages/settings.js")));
page.mount(root); await page.refresh();
const ttl = root.querySelector("#set-sticky_ttl");
assert.ok(root.textContent.includes("自动粘性映射有效期（小时）"));
assert.equal(Number(ttl.value), original / 3600);
assert.equal(ttl.getAttribute("max"), "8760");
const form = all(root).find((n) => n.tagName === "FORM");
const save = async () => { form.emit("submit"); await new Promise((r) => setTimeout(r, 20)); };
if (scenario === "tooltip") {
  const help = root.querySelector("#sticky-ttl-help");
  const tip = root.querySelector("#sticky-ttl-tip");
  assert.ok(help && tip); assert.equal(help.getAttribute("type"), "button");
  assert.equal(help.getAttribute("aria-describedby"), "sticky-ttl-tip");
  assert.equal(tip.getAttribute("role"), "tooltip"); assert.equal(tip.hidden, true);
  help.parentNode.emit("pointerenter"); assert.equal(tip.hidden, false); assert.equal(help.ariaExpanded, "true");
  help.parentNode.emit("pointerleave"); assert.equal(tip.hidden, true); assert.equal(help.ariaExpanded, "false");
  help.focus(); assert.equal(tip.hidden, false);
  help.emit("keydown", { key: "Escape", bubbles: true }); assert.equal(tip.hidden, true);
  help.click(); assert.equal(tip.hidden, false); assert.equal(sent, null);
  help.click(); assert.equal(tip.hidden, true);
  help.focus(); help.parentNode.emit("focusout"); assert.equal(tip.hidden, true);
} else {
  const values = { zero: "0", fraction: "1.5", maximum: "8760", tiny: "0.000001", negative: "-1", over: "8760.1", empty: "", nan: "abc" };
  if (scenario in values) { ttl.value = values[scenario]; ttl.emit("input", { bubbles: true }); }
  else {
    const other = root.querySelector("#set-connect_timeout"); other.value = "12"; other.emit("input", { bubbles: true });
    await page.refresh(); assert.equal(Number(ttl.value), original / 3600);
  }
  await save();
  if (["tiny", "negative", "over", "empty", "nan"].includes(scenario)) assert.equal(sent, null);
  else {
    assert.equal(sent.sticky_ttl, ({ zero: 0, fraction: 5400, maximum: 31536000 })[scenario] ?? original);
    assert.equal(Number(root.querySelector("#set-sticky_ttl").value), sent.sticky_ttl / 3600);
  }
}
page.unmount();
console.log(`settings ${scenario}: ok`);
