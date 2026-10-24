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
  focus() { document.activeElement = this; }
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
const conditions = (count) => Array.from({ length: count }, (_, i) => `host-${String(i).padStart(3, "0")}.test`);
const scenario = process.argv[2];
let rows = (scenario === "conflict" ? conditions(205).reverse() : ["z.test", "B.test", "a.test", "_service.test", "Ä.test"])
  .map((condition) => ({ condition, upstream: "direct" }));
let revision = 1;
let sent = null;
let issues = [];
let resolveValidation = null;
let holdValidation = false;
let holdSave = false;
let resolveSave = null;
globalThis.fetch = async (url, options) => {
  let data;
  if (url === "/api/upstreams") data = { upstreams: [{ name: "direct", enabled: true }] };
  else if (url === "/api/rules" && options.method === "GET") {
    if (scenario === "save_snapshot_race" && revision === 2) revision = 3;
    data = { rules: structuredClone(rows), revision, enabled: true };
  }
  else if (url === "/api/rules/validate") {
    if (holdValidation) await new Promise((resolve) => { resolveValidation = resolve; });
    data = { ok: issues.length === 0, issues, rule_count: JSON.parse(options.body).rules.length };
  } else if (url === "/api/rules" && options.method === "PUT") {
    sent = JSON.parse(options.body);
    if (holdSave) await new Promise((resolve) => { resolveSave = resolve; });
    rows = sent.rules.map((row) => ({ ...row, condition: row.condition.toLowerCase() }));
    rows.sort((a, b) => a.condition < b.condition ? -1 : a.condition > b.condition ? 1 : 0);
    revision += 1;
    data = { revision, issues: [] };
  } else throw new Error(`Unexpected request ${url}`);
  return { ok: true, status: 200, text: async () => JSON.stringify(data) };
};
const { page } = await import(pathToFileURL(path.resolve("r_proxy/web/static/js/pages/rules.js")));
page.mount(root);
await page.refresh();
const tick = () => new Promise((resolve) => setTimeout(resolve, 10));
const button = (label) => all(root).find((n) => n.tagName === "BUTTON" && n.textContent === label);
const inputs = () => all(root).filter((n) => n.getAttribute("id")?.startsWith("rule-condition-"));
const field = (value) => inputs().find((n) => n.value === value);
const edit = (control, value) => { control.value = value; control.emit("input"); };
const leave = (control, target) => {
  control.emit("blur", { relatedTarget: target });
  control.emit("focusout", { relatedTarget: target, bubbles: true });
  target?.focus();
};

if (scenario === "sort") {
  assert.deepEqual(inputs().map((n) => n.value), ["_service.test", "a.test", "B.test", "z.test", "Ä.test"]);
  assert.equal(all(root).some((n) => n.attributes.has("draggable") || n.getAttribute("type") === "radio"), false);
  assert.equal(root.querySelector("#rules-display-order"), null);
  assert.equal(root.querySelector("#rules-move-position"), null);
  const filter = root.querySelector("#rules-filter-host");
  edit(filter, "a.test");
  assert.equal(inputs().length, 1);
  edit(field("a.test"), "alpha.test");
  button("保存").click();
  await tick();
  assert.equal(sent.rules.length, 5);
  assert.ok(sent.rules.some((row) => row.condition === "alpha.test"));
  assert.equal(page.isDirty(), false);
} else if (scenario === "delete_after_blur") {
  const condition = field("z.test");
  const row = condition.parentNode.parentNode;
  const remove = all(row).find((n) => n.tagName === "BUTTON");
  edit(condition, "0.test");
  leave(condition, remove);
  assert.ok(root.contains(remove), "blur detached the pending delete target");
  remove.click();
  await tick();
  button("保存").click();
  await tick();
  assert.equal(sent.rules.length, 4);
  assert.equal(sent.rules.some((item) => item.condition === "0.test"), false);
} else if (scenario === "new_row") {
  edit(root.querySelector("#rules-filter-host"), "no-match");
  button("添加规则").click();
  const fresh = inputs().find((n) => n.value === "");
  assert.ok(fresh, "new draft hidden by filter");
  assert.equal(document.activeElement, fresh);
  edit(fresh, "new.test");
  const upstream = fresh.parentNode.parentNode.querySelector("select");
  leave(fresh, upstream);
  await tick();
  assert.ok(root.contains(upstream), "blur replaced a select before it could be opened");
  upstream.value = "direct";
  upstream.emit("change");
  button("保存").click();
  await tick();
  assert.equal(sent.rules.length, 6);
  assert.ok(sent.rules.some((row) => row.condition === "new.test"));
} else if (scenario === "conflict") {
  assert.equal(inputs().length, 100);
  issues = [{ level: "error", location: "rules[0]", code: "E_RULE_OVERLAP", message: "冲突", conflict: {
    related_location: "rules[204]", condition: "host-204.test", upstream: "direct",
    related_condition: "host-000.test", related_upstream: "direct", example_host: "shared.test",
  } }];
  edit(root.querySelector("#rules-filter-host"), "host-100");
  button("校验").click();
  await tick();
  assert.ok(root.textContent.includes("shared.test"));
  button("定位规则一").click();
  assert.equal(document.activeElement?.value, "host-204.test");
  button("定位规则二").click();
  assert.equal(document.activeElement?.value, "host-000.test");
  await page.refresh();
  assert.ok(field("host-000.test").className.includes("invalid"), "same-revision polling lost conflict highlights");
} else if (scenario === "stale_validation") {
  holdValidation = true;
  issues = [{ level: "error", location: "rules[0]", code: "E_RULE_OVERLAP", message: "过期错误" }];
  button("校验").click();
  await tick();
  edit(field("a.test"), "changed.test");
  resolveValidation();
  await tick();
  assert.equal(root.textContent.includes("过期错误"), false);
} else if (scenario === "save_during_edit") {
  const condition = field("a.test");
  edit(condition, "first.test");
  holdSave = true;
  button("保存").click();
  await tick();
  edit(condition, "second.test");
  holdSave = false;
  resolveSave();
  await tick();
  assert.equal(page.isDirty(), true);
  assert.equal(field("second.test"), condition);
  button("保存").click();
  await tick();
  assert.equal(sent.revision, 2);
  assert.ok(sent.rules.some((row) => row.condition === "second.test"));
  assert.equal(page.isDirty(), false);
} else if (scenario === "save_snapshot_race") {
  edit(field("a.test"), "changed.test");
  button("保存").click();
  await tick();
  assert.ok(root.textContent.includes("本次保存后又出现新版本"));
  assert.equal(page.isDirty(), true);
  assert.ok(field("changed.test"));
} else throw new Error(`Unknown scenario ${scenario}`);
page.unmount();
console.log(`PASS ${scenario}`);
