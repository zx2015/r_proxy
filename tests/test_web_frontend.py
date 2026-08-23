"""单页应用：静态托管、深链接、以及 M4-09 的转义守护。

对应设计：docs/design/DD_WEB.md §10.3、§10.4、§10.8。

浏览器不在测试环境里，因此**不断言「渲染结果是纯文本」**——没有 DOM 可断言。
改为断言两件在 Python 里可验证、合起来足以支撑 M4-09 的事实：

1. 前端代码里**不存在**任何能把字符串交给 HTML 解析器的汇点，属性设置全部收在
   ``dom.js`` 且危险属性被拒；
2. 服务端**原样**返回带 ``<script>`` 的日志值，转义是渲染层的职责。

第二条是正向断言：服务端提前转义会让 API 的值与库里的值不一致，反而掩盖问题。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import httpx
import pytest

from r_proxy.app import Application
from r_proxy.storage.schema import Database, open_write
from r_proxy.web.app import SPA_PAGES, create_app

STATIC_DIR = Path(__file__).resolve().parent.parent / "r_proxy" / "web" / "static"

XSS_URL = 'http://evil.example/<script>alert("1")</script>?q=<img src=x onerror=alert(1)>'
XSS_HOST = "<script>alert(2)</script>.example"

CONFIG = """
[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[database]
state_path = "{state}"
logs_path = "{logs}"

[[upstreams]]
name = "direct"
type = "direct"
"""

_LOG_COLUMNS = (
    "request_id",
    "host",
    "url",
    "method",
    "upstream_name",
    "upstream_priority",
    "attempt_index",
    "decision_source",
    "http_status",
    "elapsed_ms",
    "bytes_up",
    "bytes_down",
    "created_at",
)


def seed_log(path: Path, **overrides: object) -> None:
    row: dict[str, object] = {
        "request_id": "req-xss",
        "host": XSS_HOST,
        "url": XSS_URL,
        "method": "GET",
        "upstream_name": "direct",
        "upstream_priority": 100,
        "attempt_index": 0,
        "decision_source": "priority",
        "http_status": 200,
        "elapsed_ms": 3,
        "bytes_up": 0,
        "bytes_down": 7,
        "created_at": 1_700_000_000,
    }
    row.update(overrides)
    statement = (
        f"INSERT INTO request_log ({', '.join(_LOG_COLUMNS)}) "
        f"VALUES ({', '.join(':' + column for column in _LOG_COLUMNS)})"
    )
    connection = open_write(path, Database.LOGS)
    try:
        connection.execute(statement, row)
    finally:
        connection.close()


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    logs = tmp_path / "logs.db"
    seed_log(logs)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=tmp_path / "state.db", logs=logs), encoding="utf-8")
    app = Application(config_path=config)
    await app.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    ) as session:
        yield session
    await app.stop()


# --------------------------------------------------------------------------
# 源码扫描：把字符串与注释剔掉之后再找汇点
# --------------------------------------------------------------------------

# 注释里提到 innerHTML、禁列常量里写着 href 都不是汇点。不剔掉它们就只能靠
# 逐处豁免，而豁免清单一长，守护就失效了。
_CODE = "code"
_LINE_COMMENT = "line"
_BLOCK_COMMENT = "block"


def strip_literals(source: str) -> str:
    """把字符串字面量与注释替换成等长空白，保留行号与其余代码。"""
    out: list[str] = []
    state = _CODE
    quote = ""
    index = 0
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == _CODE:
            if char in "\"'`":
                state, quote = "string", char
                out.append(" ")
            elif char == "/" and nxt == "/":
                state = _LINE_COMMENT
                out.append("  ")
                index += 1
            elif char == "/" and nxt == "*":
                state = _BLOCK_COMMENT
                out.append("  ")
                index += 1
            else:
                out.append(char)
        elif state == "string":
            if char == "\\":
                out.append("  ")
                index += 1
            elif char == quote:
                state = _CODE
                out.append(" ")
            else:
                out.append("\n" if char == "\n" else " ")
        elif state == _LINE_COMMENT:
            if char == "\n":
                state = _CODE
                out.append("\n")
            else:
                out.append(" ")
        else:  # 块注释
            if char == "*" and nxt == "/":
                state = _CODE
                out.append("  ")
                index += 1
            else:
                out.append("\n" if char == "\n" else " ")
        index += 1
    return "".join(out)


# 能把字符串交给 HTML 解析器、或让属性值变成可执行代码的写法。
SINKS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write\b",
    "eval": r"\beval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "href / src 赋值": r"\.\s*(?:href|src|srcdoc|formaction)\s*=[^=]",
    "on* 赋值": r"\.\s*on[a-z]+\s*=[^=]",
}


def sinks_in(source: str) -> list[tuple[int, str]]:
    code = strip_literals(source)
    found: list[tuple[int, str]] = []
    for name, pattern in SINKS.items():
        for match in re.finditer(pattern, code):
            found.append((code[: match.start()].count("\n") + 1, name))
    return sorted(found)


def js_files() -> list[Path]:
    """``static/`` 下的全部脚本。

    **不用手写清单**：漏掉的那个文件恰好就是出问题的那个（教训见 M3 的唯一
    写者守护——初版只扫按包遍历的结果，漏掉了顶层模块）。
    """
    files = sorted(STATIC_DIR.rglob("*.js"))
    assert files, "static/ 下没有脚本，扫描范围为空"
    return files


class HtmlProbe(HTMLParser):
    """收集内联脚本、事件属性、导航链接与外链资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.event_attrs: list[tuple[str, str]] = []
        self.javascript_urls: list[tuple[str, str]] = []
        self.nav_links: list[str] = []
        self.script_srcs: list[str] = []
        self.inline_scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {name: (value or "") for name, value in attrs}
        for name, value in mapping.items():
            if name.startswith("on"):
                self.event_attrs.append((tag, name))
            if value.strip().lower().startswith("javascript:"):
                self.javascript_urls.append((tag, name))
        if tag == "script":
            self._in_script = True
            self.script_srcs.append(mapping.get("src", ""))
        if tag == "a" and "data-page" in mapping:
            self.nav_links.append(mapping.get("href", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts.append(data.strip())


def probe(html: str) -> HtmlProbe:
    parser = HtmlProbe()
    parser.feed(html)
    parser.close()
    return parser


def html_files() -> list[Path]:
    files = sorted(STATIC_DIR.rglob("*.html"))
    assert files, "static/ 下没有 HTML"
    return files


# --------------------------------------------------------------------------
# M4-09：日志里的 <script> 显示为纯文本
# --------------------------------------------------------------------------


class TestNoHtmlSinks:
    """前端不存在把字符串交给 HTML 解析器的路径（M4-09）。"""

    @pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
    def test_m4_09_scripts_never_reach_the_html_parser(self, path: Path) -> None:
        found = sinks_in(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} 出现 HTML 汇点：{found}"

    @pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
    def test_m4_09_attributes_are_set_only_through_dom_js(self, path: Path) -> None:
        """``setAttribute`` 只允许出现在 ``dom.js``。

        属性名是动态的时候，静态扫描分不清设的是 ``class`` 还是 ``href``。把这个
        动作收到一个文件里，那里的禁列在运行时逐个检查，扫描只需确认没有第二处。
        """
        code = strip_literals(path.read_text(encoding="utf-8"))
        if path.name == "dom.js":
            assert "setAttribute" in code, "dom.js 应当是唯一设置属性的地方"
            return
        assert "setAttribute" not in code, f"{path.name} 绕过 dom.js 直接设置属性"

    def test_m4_09_the_deny_list_covers_every_executable_attribute(self) -> None:
        """禁列必须罩住 on*、href、src、srcdoc、formaction、style。

        光有 ``textContent`` 不够：``<a href="javascript:...">`` 一点即执行，而日志
        里的 URL 完全由客户端流量决定。
        """
        source = (STATIC_DIR / "js" / "dom.js").read_text(encoding="utf-8")
        pattern = re.search(r"FORBIDDEN_ATTRS\s*=\s*/([^/]+)/", source)
        assert pattern is not None, "dom.js 里找不到 FORBIDDEN_ATTRS"
        deny = re.compile(pattern.group(1).replace("$", ""), re.IGNORECASE)
        for attribute in ("onclick", "onerror", "href", "src", "srcdoc", "formaction", "style"):
            assert deny.match(attribute), f"禁列漏掉了 {attribute}"

    @pytest.mark.parametrize("path", html_files(), ids=lambda p: p.name)
    def test_m4_09_html_has_no_inline_script_or_handler(self, path: Path) -> None:
        """CSP 的 ``script-src 'self'`` 会阻止内联脚本，页面因此必须全部外链。"""
        parsed = probe(path.read_text(encoding="utf-8"))
        assert parsed.inline_scripts == []
        assert parsed.event_attrs == []
        assert parsed.javascript_urls == []
        assert all(src for src in parsed.script_srcs), "存在没有 src 的 <script>"


class TestGuardsCatchViolations:
    """守护测试本身也要被守护：这些反例必须被扫出来。"""

    @pytest.mark.parametrize(
        "snippet",
        [
            "row.innerHTML = item.url;",
            "cell.outerHTML = value;",
            'box.insertAdjacentHTML("beforeend", html);',
            "document.write(payload);",
            "eval(payload);",
            "const f = new Function(payload);",
            "link.href = item.url;",
            "img.src = item.url;",
            "node.onclick = handler;",
        ],
    )
    def test_a_sink_is_reported(self, snippet: str) -> None:
        assert sinks_in(snippet), f"扫描器漏掉了 {snippet}"

    @pytest.mark.parametrize(
        "snippet",
        [
            "cell.textContent = item.url;",
            "const url = new URL(link.href).pathname;",
            "// 说明：不要用 innerHTML",
            "/* innerHTML 会解析 HTML */",
            'const marker = "innerHTML";',
            "const pattern = /^(on|href$|src$)/i;",
            "size /= 1024;",
        ],
    )
    def test_safe_code_is_not_reported(self, snippet: str) -> None:
        assert sinks_in(snippet) == [], f"扫描器误报了 {snippet}"

    def test_the_html_probe_catches_inline_script_and_handlers(self) -> None:
        bad = probe('<button onclick="x()">go</button><script>alert(1)</script>')
        assert bad.event_attrs == [("button", "onclick")]
        assert bad.inline_scripts == ["alert(1)"]
        assert probe('<a href="javascript:alert(1)">x</a>').javascript_urls == [("a", "href")]

    def test_the_attribute_scan_sees_through_comments(self) -> None:
        """注释里的 setAttribute 不算，代码里的算。"""
        assert "setAttribute" not in strip_literals("// node.setAttribute('href', u)")
        assert "setAttribute" in strip_literals("node.setAttribute('class', 'x')")


class TestApiKeepsValuesRaw:
    async def test_m4_09_the_api_returns_the_script_tag_unescaped(
        self, client: httpx.AsyncClient
    ) -> None:
        """服务端不转义，渲染层才转义。

        提前转义会让 API 的值与数据库里的值不一致：日志里存的是 ``<script>``，
        接口回的是 ``&lt;script&gt;``，按 host 精确筛选反而查不到自己刚看到的那条。
        """
        page = (await client.get("/api/logs")).json()
        assert [item["url"] for item in page["items"]] == [XSS_URL]
        assert [item["host"] for item in page["items"]] == [XSS_HOST]

    async def test_m4_09_static_responses_carry_the_csp(self, client: httpx.AsyncClient) -> None:
        """纵深防御：即便渲染层某处疏漏，内联脚本仍会被 CSP 拦下。"""
        response = await client.get("/")
        assert response.status_code == 200
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"


class TestCaching:
    """静态资源必须回源校验，接口一律不落盘（DD_WEB §7.2.1）。"""

    @pytest.mark.parametrize("path", ["/", "/static/js/pages/sticky.js"])
    async def test_static_assets_must_be_revalidated(
        self, client: httpx.AsyncClient, path: str
    ) -> None:
        """资源 URL 不带版本号，不发缓存头就会在升级后继续跑旧脚本。

        实际踩过一次：粘性页新加的按钮在界面上不出现，而服务端发的是新文件。
        """
        assert (await client.get(path)).headers["cache-control"] == "no-cache"

    async def test_api_responses_are_never_stored(self, client: httpx.AsyncClient) -> None:
        """请求日志含完整 URL、出口列表含内网地址，不该写进磁盘缓存。"""
        assert (await client.get("/api/logs")).headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------
# 静态托管与深链接
# --------------------------------------------------------------------------


class TestStaticHosting:
    async def test_the_index_is_served_at_the_root(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        assert "r-proxy" in response.text

    @pytest.mark.parametrize(
        "asset",
        ["/static/css/app.css", "/static/js/app.js", "/static/js/pages/dashboard.js"],
    )
    async def test_assets_are_served(self, client: httpx.AsyncClient, asset: str) -> None:
        assert (await client.get(asset)).status_code == 200

    @pytest.mark.parametrize("page", SPA_PAGES)
    async def test_deep_links_return_the_single_page_app(
        self, client: httpx.AsyncClient, page: str
    ) -> None:
        """`/upstreams` 这类路径磁盘上没有对应文件，必须回 index.html。"""
        response = await client.get(f"/{page}")
        assert response.status_code == 200
        assert response.text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    async def test_a_typo_is_a_404_not_the_app(self, client: httpx.AsyncClient) -> None:
        """深链接走白名单而不是兜底。

        兜底会让 `/static/js/app.jsx` 这类笔误拿到一个 200 的 HTML，浏览器只报
        「MIME 类型不匹配」，指不到真正的原因。
        """
        assert (await client.get("/static/js/app.jsx")).status_code == 404
        assert (await client.get("/dashboards")).status_code == 404

    async def test_the_static_mount_shadows_nothing(self, tmp_path: Path) -> None:
        """静态资源不能挂在 `/`。

        挂在 `/` 的 StaticFiles 匹配一切，**此后注册的任何路由都到不了**——包括
        `/api/*`。这个陷阱是静默的：`create_app` 里的路由都在挂载之前注册，所以
        现有接口照常工作，只有新加的（以及测试里临时加的）会莫名其妙地 404。
        """
        config = tmp_path / "config.toml"
        config.write_text(
            CONFIG.format(state=tmp_path / "state.db", logs=tmp_path / "logs.db"),
            encoding="utf-8",
        )
        app = Application(config_path=config)
        await app.start()
        web = create_app(app)

        @web.get("/api/added-later")
        async def added_later() -> dict[str, bool]:
            return {"reached": True}

        transport = httpx.ASGITransport(app=web, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://webui.test") as session:
            response = await session.get("/api/added-later")
        await app.stop()
        assert response.json() == {"reached": True}

    def test_every_navigation_link_has_a_server_route(self) -> None:
        """`index.html` 的导航与服务端白名单一一对应。

        两处重复是深链接白名单的代价，用这条断言消掉：加了页面忘了加路由，
        表现是刷新页面就 404，而开发时点导航永远不会触发它。
        """
        links = probe((STATIC_DIR / "index.html").read_text(encoding="utf-8")).nav_links
        assert links, "index.html 里没有带 data-page 的导航链接"
        served = {"/"} | {f"/{page}" for page in SPA_PAGES}
        assert set(links) <= served, f"导航链接缺少服务端路由：{set(links) - served}"

    def test_every_page_module_is_registered_in_the_shell(self) -> None:
        """每个页面模块都要被 `app.js` 引入，且 id 与服务端路由一致。"""
        shell = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
        modules = sorted(path.stem for path in (STATIC_DIR / "js" / "pages").glob("*.js"))
        assert modules, "pages/ 下没有页面模块"
        for module in modules:
            assert f'./pages/{module}.js"' in shell, f"app.js 没有引入 {module}"
        assert set(SPA_PAGES) | {"dashboard"} == set(modules)


class TestModuleGraph:
    """每个命名导入都要能在目标模块里找到对应的导出。

    这类错误在浏览器里表现为**整页空白**：链接阶段失败，一行脚本都不会执行，
    控制台里只有一句 SyntaxError，页面上什么都没有。它也不会被任何后端测试
    看到，因此必须专门守。
    """

    @pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
    def test_named_imports_resolve(self, path: Path) -> None:
        for target, names in _imports_of(path).items():
            resolved = (path.parent / target).resolve()
            assert resolved.is_file(), f"{path.name} 引入了不存在的模块 {target}"
            exported = _exports_of(resolved)
            missing = names - exported
            assert not missing, f"{path.name} 从 {target} 引入了未导出的 {missing}"

    def test_the_import_scan_reads_aliases_and_skips_namespaces(self) -> None:
        """守护测试本身也要被守护。"""
        source = (
            'import { page as dashboard, el } from "./pages/dashboard.js";\n'
            'import * as router from "./router.js";\n'
        )
        assert _parse_imports(source) == {"./pages/dashboard.js": {"page", "el"}}
        assert _parse_exports("export const page = 1;\nexport function el() {}\n") == {
            "page",
            "el",
        }


def _imports_of(path: Path) -> dict[str, set[str]]:
    return _parse_imports(path.read_text(encoding="utf-8"))


def _parse_imports(source: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for clause, target in re.findall(r"import\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"", source):
        names = found.setdefault(target, set())
        for piece in clause.split(","):
            name = piece.strip().split(" as ")[0].strip()
            if name:
                names.add(name)
    return found


def _exports_of(path: Path) -> set[str]:
    return _parse_exports(path.read_text(encoding="utf-8"))


def _parse_exports(source: str) -> set[str]:
    return set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)", source))


class TestRulesPage:
    """M5-26：规则页表格化之后的三条源码守护。"""

    def test_the_diff_module_is_gone(self) -> None:
        """表格化之后 diff 预览失去意义，`diff.js` 随之删除。

        留着一个没人引用的模块比删掉更糟：下次改规则页的人会以为它还在链路上。
        """
        assert not (STATIC_DIR / "js" / "diff.js").exists()
        for path in js_files():
            assert "diff.js" not in path.read_text(encoding="utf-8"), path.name

    def test_rule_rows_are_draggable_with_the_string_true(self) -> None:
        """`draggable` 是枚举属性：赋空串时元素**不可拖动**，且没有任何报错。

        这个坑在出口页的分组拖拽上踩过一次，这里守住第二处。
        """
        source = (STATIC_DIR / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
        assert re.search(r'draggable:\s*"true"', source), "规则行没有可拖动标记"
        assert not re.search(r'draggable:\s*(?:""|true\b)', source), "draggable 必须是字符串 true"

    def test_keyboard_reordering_exists_alongside_dragging(self) -> None:
        """拖拽在键盘与触屏上不可达，↑/↓ 不是可选项（WEBUI_SPEC §2.4.2、§5）。"""
        source = (STATIC_DIR / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
        assert '"↑"' in source and '"↓"' in source

    def test_the_frontend_never_serialises_rule_text(self) -> None:
        """前端只提交 `[{condition, upstream}]`，序列化是服务端的事。

        这从结构上消灭了「前端生成畸形格式」这一整类 bug。
        """
        source = (STATIC_DIR / "js" / "pages" / "rules.js").read_text(encoding="utf-8")
        for marker in ("\\t", "forward ", 'join("\\n")'):
            assert marker not in source, f"规则页出现了规则文本序列化的痕迹：{marker}"


class TestStickyPage:
    """粘性页的「固化为规则」入口（WEBUI_SPEC §2.3、DD_WEB §8.9）。"""

    def test_the_promote_action_is_wired_to_the_api(self) -> None:
        source = (STATIC_DIR / "js" / "pages" / "sticky.js").read_text(encoding="utf-8")
        assert "api.promoteSticky(" in source

    def test_opening_a_panel_repaints_without_going_through_refresh(self) -> None:
        """展开面板必须直接重画。

        `refresh()` 为了不冲掉展开中的面板，在 `binding`/`promoting` 非空时跳过
        `renderSticky`。于是「设状态 → refresh()」这条路径画不出刚打开的面板——
        按钮点下去毫无反应。改绑按钮带着这个缺陷活了一阵，直到固化按钮踩到同一处。
        """
        source = (STATIC_DIR / "js" / "pages" / "sticky.js").read_text(encoding="utf-8")
        opener = re.search(r"function openPanel\(.*?\n\}", source, re.DOTALL)
        assert opener is not None, "sticky.js 里找不到 openPanel"
        assert "renderSticky(" in opener.group(0), "openPanel 必须自己重画，不能只改状态"

    def test_the_panel_says_switching_stops(self) -> None:
        """固化改变的是语义，不只是存续时间。

        确认前不写明「失败不再自动切换」，用户会以为这只是「记得更牢一点」，
        直到某天那个出口挂了、该 host 直接 502 才发现兜底早就没了。
        """
        source = (STATIC_DIR / "js" / "pages" / "sticky.js").read_text(encoding="utf-8")
        assert "不再自动切换" in source


class TestPackaging:
    def test_the_static_tree_is_declared_as_package_data(self) -> None:
        """静态资源不是 Python 包，漏了 package-data 就打不进 wheel。

        症状是安装版界面全白而源码运行正常——最难联想到打包配置的一种故障。

        匹配必须用 glob 语义（``PurePath.match``）而不是 ``fnmatch``：后者的 ``*``
        会跨过 ``/``，于是 ``static/js/*.js`` 看起来也能罩住 ``static/js/pages/`` 下
        的文件，而 setuptools 按 glob 展开，实际一个都不收——这条断言因此会在
        真正漏掉子目录时静默通过。
        """
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        patterns = _package_data_patterns(pyproject)
        for path in _static_files():
            relative = PurePosixPath(path.relative_to(STATIC_DIR.parent).as_posix())
            assert any(relative.match(pattern) for pattern in patterns), (
                f"{relative} 不在 package-data 的模式里"
            )


def _package_data_patterns(pyproject: str) -> Sequence[str]:
    block = re.search(r'"r_proxy\.web"\s*=\s*\[(.*?)\]', pyproject, re.DOTALL)
    assert block is not None, "pyproject.toml 里没有 r_proxy.web 的 package-data"
    return re.findall(r'"([^"]+)"', block.group(1))


def _static_files() -> list[Path]:
    return sorted(path for path in STATIC_DIR.rglob("*") if path.is_file())
