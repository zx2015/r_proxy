"""架构约束的自动化守护。

对应设计：docs/design/MIGRATION.md §6.2、docs/design/ARCH_OVERVIEW.md §4.1。

架构约束靠人工审查守不住——它们会在赶进度时第一个被牺牲。这里的检查随
里程碑逐步启用：尚不存在的包自动跳过，包一落地约束立即生效。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "r_proxy"

# 层与层之间只能自上而下依赖（ARCH_OVERVIEW §4.1）。
FORBIDDEN_IMPORTS = {
    "decision": {
        "sqlite3",
        "socket",
        "asyncio",
        # 时间一律由调用方注入：决策层自己取时钟会让冷却期、TTL、限流窗口
        # 无法在测试中确定性复现，只能靠 sleep 或 mock 时钟。
        "time",
        "r_proxy.storage",
        "r_proxy.protocol",
        "r_proxy.egress",
        "r_proxy.web",
    },
    "config": {"tomlkit", "fastapi", "uvicorn", "r_proxy.web"},
    "rules": {"r_proxy.protocol", "r_proxy.egress", "r_proxy.storage", "r_proxy.web"},
    "state": {
        "time",
        # 内存权威状态自己不落盘：热路径禁止同步 I/O，且落盘一律经写者线程。
        "sqlite3",
        "r_proxy.storage",
        "r_proxy.protocol",
        "r_proxy.egress",
        "r_proxy.decision",
        "r_proxy.web",
    },
    "protocol": {"r_proxy.web", "fastapi", "uvicorn", "tomlkit"},
    "egress": {"r_proxy.web", "r_proxy.protocol", "fastapi", "uvicorn"},
}

# 代理核心的任何模块都不得引入第三方运行时依赖。
CORE_PACKAGES = ("config", "rules", "decision", "egress", "protocol", "state", "storage")
THIRD_PARTY = frozenset({"fastapi", "uvicorn", "tomlkit", "pydantic", "starlette", "yaml"})


def modules_of(package: str) -> list[Path]:
    directory = PACKAGE_ROOT / package
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.py") if p.name != "__init__.py")


def all_modules() -> list[Path]:
    """包内全部模块，含 ``app.py``、``persistence.py`` 等顶层模块。

    按包遍历会漏掉顶层模块，而装配代码正好都在那里——最可能出现第二个写者
    的地方恰恰是 ``app.py``。
    """
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def imports_of(path: Path) -> set[str]:
    """模块导入的全部顶层与点分名称。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.add(node.module.split(".")[0])
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


@pytest.mark.parametrize("package", sorted(FORBIDDEN_IMPORTS))
def test_layer_does_not_import_upward(package: str) -> None:
    forbidden = FORBIDDEN_IMPORTS[package]
    for module in modules_of(package):
        violations = imports_of(module) & forbidden
        assert not violations, f"{module.relative_to(PACKAGE_ROOT)} 违规导入 {violations}"


@pytest.mark.parametrize("package", CORE_PACKAGES)
def test_core_has_no_third_party_runtime_dependency(package: str) -> None:
    for module in modules_of(package):
        violations = imports_of(module) & THIRD_PARTY
        assert not violations, f"{module.relative_to(PACKAGE_ROOT)} 引入第三方依赖 {violations}"


def test_web_dependencies_are_confined_to_the_web_package() -> None:
    """fastapi / uvicorn / tomlkit 只允许出现在 ``r_proxy/web/`` 里（M4-01）。

    按包遍历的 ``CORE_PACKAGES`` 检查漏掉了 ``app.py`` 与 ``cli.py``，而它们正是
    最容易顺手 ``import uvicorn`` 的地方——一旦漏进去，``--no-web`` 形态就在
    没装 fastapi 的机器上直接崩了。
    """
    for module in all_modules():
        relative = module.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "web":
            continue
        violations = imports_of(module) & THIRD_PARTY
        assert not violations, f"{relative} 引入 Web 依赖 {violations}"


def test_the_web_package_has_exactly_one_import_point() -> None:
    """``r_proxy.web`` 只能由 ``app.py`` 导入（DD_WEB §2.2）。

    多一个导入点就多一条绕过「依赖缺失时降级」的路径：那条路径上缺 fastapi
    的表现是 ``ModuleNotFoundError`` 把代理一起带走。
    """
    for module in all_modules():
        relative = module.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "web" or relative == Path("app.py"):
            continue
        assert "r_proxy.web" not in imports_of(module), f"{relative} 直接导入了 Web 包"


def test_web_queries_are_reached_only_through_to_thread() -> None:
    """``queries.X`` 只能作为 ``to_thread`` 的实参出现（DD_WEB §4.2）。

    漏掉一次就等于在事件循环里跑 SQLite：查询期间代理完全停止转发。症状是
    「打开管理界面时代理卡住」，几乎不会有人联想到某一个日志查询。
    """
    for module in sorted((PACKAGE_ROOT / "web").rglob("*.py")):
        if module.name == "queries.py":
            continue
        lines = _queries_outside_to_thread(module.read_text(encoding="utf-8"))
        relative = module.relative_to(PACKAGE_ROOT)
        assert not lines, f"{relative} 第 {lines} 行在 to_thread 之外碰了 queries"


def test_the_to_thread_guard_catches_a_direct_call() -> None:
    """守护测试本身也要被守护。"""
    assert _queries_outside_to_thread("await asyncio.to_thread(queries.query_logs, pool, p)") == []
    assert _queries_outside_to_thread("rows = queries.query_logs(pool, p)") == [1]
    # 交给别的包装函数也不行：只有 to_thread 能保证换线程。
    assert _queries_outside_to_thread("rows = await helper(queries.query_logs, pool)") == [1]


def _queries_outside_to_thread(source: str) -> list[int]:
    tree = ast.parse(source)
    passed_to_thread = {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_to_thread(node.func)
        for arg in node.args
        if _is_queries_reference(arg)
    }
    return [
        node.lineno
        for node in ast.walk(tree)
        if _is_queries_reference(node) and id(node) not in passed_to_thread
    ]


def _is_to_thread(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "to_thread"
    return isinstance(func, ast.Name) and func.id == "to_thread"


def _is_queries_reference(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "queries"
    )


def test_config_package_never_imports_tomlkit() -> None:
    """配置读路径只用标准库 tomllib；tomlkit 属于 [web] extra（M1-15）。"""
    for module in modules_of("config"):
        assert "tomlkit" not in imports_of(module), module


def test_core_imports_without_web_dependencies() -> None:
    """代理核心在缺少全部 Web 依赖时可正常导入（M1-15）。

    在子进程里做：本进程内 reload 会产生新的类对象，让其它测试里的
    ``pytest.raises(ConnectorError)`` 对不上号。
    """
    script = """
import sys

BLOCKED = {"fastapi", "uvicorn", "tomlkit", "pydantic", "starlette", "yaml"}


class Blocker:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} 被测试屏蔽")
        return None


sys.meta_path.insert(0, Blocker())
for module in ("r_proxy.app", "r_proxy.cli", "r_proxy.config.loader",
               "r_proxy.protocol.server", "r_proxy.egress.connector"):
    __import__(module)
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PACKAGE_ROOT.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_write_connections_are_opened_only_by_the_writer() -> None:
    """写连接只能由每个库各自的唯一写者打开（PRD §4.9.1）。

    ``open_write`` 是唯一产出写连接的函数。它一旦被第四处调用，「每个库的写入
    都串行经过一个地方」的前提就没了，而症状是并发下随机丢更新——极难定位。
    """
    # schema.py 定义它；writer.py 是 state.db 与 logs.db 的写者线程；
    # rules_store.py 是 rules.db 的唯一写者（DD_STORAGE §4.8）；
    # storage/__init__.py 只是重新导出。
    allowed = {
        Path("storage/schema.py"),
        Path("storage/writer.py"),
        Path("storage/rules_store.py"),
        Path("storage/__init__.py"),
    }
    for module in all_modules():
        relative = module.relative_to(PACKAGE_ROOT)
        if relative in allowed:
            continue
        assert "open_write" not in module.read_text(encoding="utf-8"), relative


def test_readers_never_open_a_writable_connection() -> None:
    """reader.py 的每个 sqlite3.connect 都必须带 mode=ro。"""
    source = (PACKAGE_ROOT / "storage" / "reader.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
        ):
            assert "mode=ro" in ast.unparse(node), f"reader.py 第 {node.lineno} 行打开了可写连接"


def test_contracts_module_performs_no_io() -> None:
    """跨层契约必须是纯类型定义，任何层都能安全导入。"""
    forbidden = {"asyncio", "socket", "sqlite3", "pathlib", "os"}
    assert not (imports_of(PACKAGE_ROOT / "contracts.py") & forbidden)


def test_no_legacy_modules_remain() -> None:
    """handler.py 与顶层 server.py 的能力已迁入 protocol/（MIGRATION §3.1）。"""
    assert not (PACKAGE_ROOT / "handler.py").exists()
    assert not (PACKAGE_ROOT / "server.py").exists()


def test_error_messages_are_never_built_by_interpolation() -> None:
    """错误响应体只能用预定义常量，绝不拼接地址或异常信息（PRD §4.3.9）。

    唯一的例外是 ``str(exc)``：它转述客户端自己发来的请求的格式问题，
    不含任何服务端信息。
    """
    source = (PACKAGE_ROOT / "protocol" / "connection.py").read_text(encoding="utf-8")
    for node in _send_error_messages(source):
        assert _is_safe_error_message(node), (
            f"_send_error 第 {node.lineno} 行拼接了消息，只允许预定义常量或 str(exc)"
        )


def test_the_error_message_guard_actually_catches_interpolation() -> None:
    """守护测试本身也要被守护：断言它能挡住 f-string 与字符串拼接。"""
    bad = [
        'self._send_error(502, f"无法连接 {host}:{port}")',
        'self._send_error(502, "无法连接 " + host)',
        'self._send_error(502, "无法连接 {}".format(host))',
        "self._send_error(502, MSG + repr(exc))",
    ]
    for snippet in bad:
        nodes = _send_error_messages(snippet)
        assert nodes and not any(_is_safe_error_message(n) for n in nodes), snippet

    good = ["self._send_error(502, MSG_NO_UPSTREAM)", "self._send_error(400, str(exc))"]
    for snippet in good:
        assert all(_is_safe_error_message(n) for n in _send_error_messages(snippet))


def _send_error_messages(source: str) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_send_error"
            and len(node.args) > 1
        ):
            found.append(node.args[1])
    return found


def _is_safe_error_message(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id.startswith("MSG_")
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"


def test_readuntil_callers_handle_limit_overrun() -> None:
    """凡调用 ``readuntil``/``readline`` 的地方必须接住 ``asyncio.LimitOverrunError``。

    ``asyncio.LimitOverrunError`` 直接继承 ``Exception``——既不是 ``OSError``
    也不是 ``ValueError`` 的子类。漏接的后果不是「归类错一点」，而是异常一路
    逃出连接处理函数：客户端只看到连接被关闭，收不到任何响应（抓包表现为
    ``<empty: no response>``），服务端日志里只剩一条没有上下文的栈。
    """
    for module in all_modules():
        source = module.read_text(encoding="utf-8")
        if ".readuntil(" not in source and ".readline(" not in source:
            continue
        relative = module.relative_to(PACKAGE_ROOT)
        unprotected = _readuntil_without_overrun_handling(source)
        assert not unprotected, (
            f"{relative} 第 {unprotected} 行调用 readuntil/readline，"
            "但没有接住 asyncio.LimitOverrunError"
        )


def test_the_readuntil_guard_actually_catches_a_missing_handler() -> None:
    """守护测试本身也要被守护。"""
    bad = (
        "async def f(reader):\n"
        "    try:\n"
        "        raw = await reader.readuntil(b'\\r\\n')\n"
        "    except (OSError, ValueError) as exc:\n"
        "        raise RuntimeError from exc\n"
    )
    assert _readuntil_without_overrun_handling(bad) == [3]

    good = (
        "async def f(reader):\n"
        "    try:\n"
        "        raw = await reader.readuntil(b'\\r\\n')\n"
        "    except (OSError, ValueError, asyncio.LimitOverrunError) as exc:\n"
        "        raise RuntimeError from exc\n"
    )
    assert _readuntil_without_overrun_handling(good) == []

    # 完全不在 try 里同样算漏接。
    naked = "async def f(reader):\n    return await reader.readuntil(b'\\r\\n')\n"
    assert _readuntil_without_overrun_handling(naked) == [2]


def _readuntil_without_overrun_handling(source: str) -> list[int]:
    """返回「调用了 ``readuntil``/``readline`` 但所在 try 没接超限异常」的行号。

    以「所在 try 的 except 子句是否提到 ``LimitOverrunError``」判定，不做语义
    推断：``except (OSError, ValueError, asyncio.LimitOverrunError)`` 与
    ``except asyncio.LimitOverrunError`` 都要算接住了。
    """
    tree = ast.parse(source)
    all_sites: set[int] = set()
    protected: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_line_read_call(node):
            all_sites.add(node.lineno)
        if not isinstance(node, ast.Try):
            continue
        catches = any(
            handler.type is not None and "LimitOverrunError" in ast.unparse(handler.type)
            for handler in node.handlers
        )
        if not catches:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call) and _is_line_read_call(sub):
                    protected.add(sub.lineno)

    return sorted(all_sites - protected)


def _is_line_read_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in ("readuntil", "readline")
