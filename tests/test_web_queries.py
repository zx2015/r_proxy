"""日志查询与健康接口：参数化 WHERE、分页上限、切换链、熔断重置。

对应设计：docs/design/DD_WEB.md §4.2、§4.3、§8.4。

日志库在 ``Application.start()`` **之前**预先灌数据：此时写者线程还没起来，写
入不会与它抢 WAL 锁，也不必等落盘时间窗。查询层用例直接打 ``queries``，接口层
用例经 ASGITransport 打应用。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application
from r_proxy.contracts import FailureKind
from r_proxy.state.health import HealthState
from r_proxy.storage.reader import ReadOnlyPool
from r_proxy.storage.schema import Database, open_write
from r_proxy.web import queries
from r_proxy.web.app import create_app
from r_proxy.web.schemas import LogQuery

_COLUMNS = (
    "request_id",
    "client_addr",
    "host",
    "url",
    "method",
    "upstream_name",
    "upstream_priority",
    "attempt_index",
    "decision_source",
    "rule_origin",
    "http_status",
    "error",
    "failure_kind",
    "keep_reason",
    "elapsed_ms",
    "bytes_up",
    "bytes_down",
    "created_at",
)

_INSERT = (
    f"INSERT INTO request_log ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _COLUMNS)})"
)

BASE_TS = 1_700_000_000


def log_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "request_id": "req-1",
        "client_addr": "203.0.113.1",
        "host": "example.com",
        "url": "http://example.com/",
        "method": "GET",
        "upstream_name": "direct",
        "upstream_priority": 100,
        "attempt_index": 0,
        "decision_source": "priority",
        "rule_origin": None,
        "http_status": 200,
        "error": None,
        "failure_kind": None,
        "keep_reason": None,
        "elapsed_ms": 12,
        "bytes_up": 0,
        "bytes_down": 128,
        "created_at": BASE_TS,
    }
    row.update(overrides)
    return row


def seed_logs(path: Path, rows: Sequence[dict[str, object]]) -> None:
    conn = open_write(path, Database.LOGS)
    try:
        for row in rows:
            conn.execute(_INSERT, row)
    finally:
        conn.close()


def query(**overrides: object) -> LogQuery:
    return LogQuery.model_validate(overrides)


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
name = "proxy-a"
type = "http"
address = "127.0.0.1:1"
priority = 10

[[upstreams]]
name = "direct"
type = "direct"
priority = 100
"""


async def running(tmp_path: Path, rows: Sequence[dict[str, object]] = ()) -> Application:
    logs = tmp_path / "logs.db"
    if rows:
        seed_logs(logs, rows)
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(state=tmp_path / "state.db", logs=logs),
        encoding="utf-8",
    )
    app = Application(config_path=path)
    await app.start()
    return app


def client_for(app: Application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    )


@pytest.fixture
def pool(tmp_path: Path) -> ReadOnlyPool:
    """只有查询层参与的固定数据集。"""
    seed_logs(
        tmp_path / "logs.db",
        [
            log_row(request_id="a", host="a.example", created_at=BASE_TS + 1),
            log_row(request_id="b", host="b.example", created_at=BASE_TS + 2, http_status=502),
            log_row(
                request_id="b",
                host="b.example",
                created_at=BASE_TS + 3,
                attempt_index=1,
                upstream_name="proxy-a",
                upstream_priority=10,
            ),
            log_row(
                request_id="c",
                host="c.example",
                created_at=BASE_TS + 4,
                client_addr="198.51.100.1",
            ),
        ],
    )
    return ReadOnlyPool(tmp_path / "logs.db")


class TestLogFilters:
    def test_no_filter_returns_everything_newest_first(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query())
        assert [row["created_at"] for row in rows] == [
            BASE_TS + 4,
            BASE_TS + 3,
            BASE_TS + 2,
            BASE_TS + 1,
        ]

    def test_host_filter_is_exact(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query(host="b.example"))
        assert {row["host"] for row in rows} == {"b.example"}
        assert len(rows) == 2

    def test_client_addr_filter_is_exact(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query(client_addr="198.51.100.1"))
        assert [row["request_id"] for row in rows] == ["c"]

    def test_upstream_status_and_time_filters_combine(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query(upstream="proxy-a", since=BASE_TS + 3))
        assert [row["request_id"] for row in rows] == ["b"]

    def test_until_is_inclusive(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query(until=BASE_TS + 1))
        assert [row["created_at"] for row in rows] == [BASE_TS + 1]

    def test_a_quote_in_the_host_filter_is_data_not_sql(self, pool: ReadOnlyPool) -> None:
        """注入尝试必须表现为「查无此 host」，而且不能动到数据。"""
        rows = queries.query_logs(pool, query(host="' OR 1=1 --"))
        assert rows == []
        assert len(queries.query_logs(pool, query())) == 4

    def test_a_drop_table_attempt_leaves_the_table(self, pool: ReadOnlyPool) -> None:
        queries.query_logs(pool, query(upstream="x'; DROP TABLE request_log; --"))
        assert len(queries.query_logs(pool, query())) == 4


class TestLogTrafficJoin:
    """`request_log.bytes_up/down` 恒为 0；真实传输量来自与 `traffic_log` 的联表。"""

    def test_a_row_with_matching_traffic_log_reports_real_bytes(self, tmp_path: Path) -> None:
        seed_logs(
            tmp_path / "logs.db",
            [log_row(request_id="a", host="a.example", bytes_up=0, bytes_down=0)],
        )
        seed_traffic(
            tmp_path / "logs.db",
            [traffic_row(request_id="a", host="a.example", bytes_up=111, bytes_down=222)],
        )
        pool = ReadOnlyPool(tmp_path / "logs.db")
        (row,) = queries.query_logs(pool, query())
        assert (row["bytes_up"], row["bytes_down"]) == (0, 0)
        assert (row["traffic_bytes_up"], row["traffic_bytes_down"]) == (111, 222)

    def test_a_row_without_a_matching_traffic_log_reports_null(self, tmp_path: Path) -> None:
        """被切换掉的尝试从未真正传输过数据，须与「传输了 0 字节」区分开。"""
        seed_logs(tmp_path / "logs.db", [log_row(request_id="a", host="a.example")])
        pool = ReadOnlyPool(tmp_path / "logs.db")
        (row,) = queries.query_logs(pool, query())
        assert row["traffic_bytes_up"] is None
        assert row["traffic_bytes_down"] is None

    def test_filters_still_work_alongside_the_join(self, tmp_path: Path) -> None:
        """联表不能让 host/upstream 等既有筛选条件产生「ambiguous column」。"""
        seed_logs(
            tmp_path / "logs.db",
            [
                log_row(request_id="a", host="a.example"),
                log_row(request_id="b", host="b.example"),
            ],
        )
        seed_traffic(
            tmp_path / "logs.db",
            [traffic_row(request_id="a", host="a.example", bytes_up=1, bytes_down=1)],
        )
        pool = ReadOnlyPool(tmp_path / "logs.db")
        rows = queries.query_logs(pool, query(host="b.example"))
        assert [row["request_id"] for row in rows] == ["b"]


class TestPagination:
    def test_one_extra_row_signals_a_next_page(self, pool: ReadOnlyPool) -> None:
        """多取一条是 ``has_more`` 的来源，不用 ``COUNT(*)``。"""
        rows = queries.query_logs(pool, query(page_size=2))
        assert len(rows) == 3

    def test_the_last_page_has_no_extra_row(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_logs(pool, query(page=2, page_size=2))
        assert len(rows) == 2

    def test_offset_follows_the_page(self, pool: ReadOnlyPool) -> None:
        assert query(page=3, page_size=50).offset == 100

    def test_pages_do_not_overlap_within_the_same_second(self, tmp_path: Path) -> None:
        """``created_at`` 只到秒，同秒内的行必须由 ``id`` 决定稳定次序。

        少了这个次级排序键，翻页时同一行可能出现两次而另一行被跳过。
        """
        seed_logs(
            tmp_path / "logs.db",
            [log_row(request_id=f"r{i}", created_at=BASE_TS) for i in range(6)],
        )
        pool = ReadOnlyPool(tmp_path / "logs.db")
        first = queries.query_logs(pool, query(page=1, page_size=3))[:3]
        second = queries.query_logs(pool, query(page=2, page_size=3))[:3]
        ids = [row["id"] for row in first] + [row["id"] for row in second]
        assert sorted(ids) == sorted({*ids})
        assert len(ids) == 6


class TestSwitchChains:
    def test_only_switched_requests_are_listed(self, pool: ReadOnlyPool) -> None:
        assert queries.query_switch_request_ids(pool, query()) == ["b"]

    def test_attempts_come_back_in_order(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_attempts(pool, ["b"])
        assert [row["attempt_index"] for row in rows] == [0, 1]

    def test_no_request_ids_means_no_query(self, pool: ReadOnlyPool) -> None:
        """空列表会拼出 ``IN ()``，是语法错误——必须提前返回。"""
        assert queries.query_attempts(pool, []) == []

    def test_placeholders_scale_with_the_id_count(self, pool: ReadOnlyPool) -> None:
        rows = queries.query_attempts(pool, ["a", "c"])
        assert {row["request_id"] for row in rows} == {"a", "c"}


_TRAFFIC_INSERT = (
    "INSERT INTO traffic_log (request_id, host, upstream_name, bytes_up, bytes_down, created_at)"
    " VALUES (:request_id, :host, :upstream_name, :bytes_up, :bytes_down, :created_at)"
)


def traffic_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "request_id": "req-1",
        "host": "example.com",
        "upstream_name": "direct",
        "bytes_up": 10,
        "bytes_down": 20,
        "created_at": BASE_TS,
    }
    row.update(overrides)
    return row


def seed_traffic(path: Path, rows: Sequence[dict[str, object]]) -> None:
    conn = open_write(path, Database.LOGS)
    try:
        for row in rows:
            conn.execute(_TRAFFIC_INSERT, row)
    finally:
        conn.close()


@pytest.fixture
def traffic_pool(tmp_path: Path) -> ReadOnlyPool:
    seed_traffic(
        tmp_path / "logs.db",
        [
            traffic_row(request_id="a", host="big.example", bytes_up=100, bytes_down=900),
            traffic_row(request_id="b", host="big.example", bytes_up=50, bytes_down=100),
            traffic_row(request_id="c", host="small.example", bytes_up=1, bytes_down=1),
            traffic_row(request_id="d", host="yesterday.example", created_at=BASE_TS - 90000),
        ],
    )
    return ReadOnlyPool(tmp_path / "logs.db")


class TestHostTraffic:
    def test_hosts_are_ranked_by_total_bytes_descending(self, traffic_pool: ReadOnlyPool) -> None:
        rows = queries.query_host_traffic(traffic_pool, BASE_TS - 1, BASE_TS + 1, 20)
        assert [row["host"] for row in rows] == ["big.example", "small.example"]

    def test_bytes_and_requests_are_summed_per_host(self, traffic_pool: ReadOnlyPool) -> None:
        rows = queries.query_host_traffic(traffic_pool, BASE_TS - 1, BASE_TS + 1, 20)
        big = rows[0]
        assert (big["bytes_up"], big["bytes_down"], big["requests"]) == (150, 1000, 2)

    def test_the_time_window_excludes_rows_outside_it(self, traffic_pool: ReadOnlyPool) -> None:
        """`yesterday.example` 落在窗口之外，不该出现在榜单里。"""
        rows = queries.query_host_traffic(traffic_pool, BASE_TS - 1, BASE_TS + 1, 20)
        assert "yesterday.example" not in {row["host"] for row in rows}

    def test_limit_caps_the_result_count(self, traffic_pool: ReadOnlyPool) -> None:
        rows = queries.query_host_traffic(traffic_pool, BASE_TS - 1, BASE_TS + 1, 1)
        assert len(rows) == 1
        assert rows[0]["host"] == "big.example"


@pytest.fixture
async def logs_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = await running(
        tmp_path,
        [
            log_row(request_id="a", host="a.example", created_at=BASE_TS + 1),
            log_row(
                request_id="b",
                host="b.example",
                created_at=BASE_TS + 2,
                http_status=502,
                failure_kind="upstream_error",
                upstream_name="proxy-a",
                upstream_priority=10,
            ),
            log_row(request_id="b", host="b.example", created_at=BASE_TS + 3, attempt_index=1),
        ],
    )
    async with client_for(app) as client:
        yield client
    await app.stop()


class TestLogsEndpoint:
    async def test_logs_need_a_token_when_one_is_configured(self, tmp_path: Path) -> None:
        """M4-05 的原始端点：``/api/logs`` 会返回访问过的全部 URL。"""
        logs = tmp_path / "logs.db"
        path = tmp_path / "config.toml"
        path.write_text(
            CONFIG.format(state=tmp_path / "state.db", logs=logs).replace(
                "enabled = false", 'enabled = false\nauth_token = "tok-abcdefghijklmnop"'
            ),
            encoding="utf-8",
        )
        app = Application(config_path=path)
        await app.start()
        try:
            async with client_for(app) as client:
                assert (await client.get("/api/logs")).status_code == 401
                authed = await client.get(
                    "/api/logs", headers={"X-Auth-Token": "tok-abcdefghijklmnop"}
                )
                assert authed.status_code == 200
        finally:
            await app.stop()

    async def test_logs_are_returned_newest_first(self, logs_client: httpx.AsyncClient) -> None:
        body = (await logs_client.get("/api/logs")).json()
        assert [item["created_at"] for item in body["items"]] == [
            BASE_TS + 3,
            BASE_TS + 2,
            BASE_TS + 1,
        ]
        assert body["has_more"] is False

    async def test_page_size_caps_the_items_and_sets_has_more(
        self, logs_client: httpx.AsyncClient
    ) -> None:
        body = (await logs_client.get("/api/logs?page_size=2")).json()
        assert len(body["items"]) == 2
        assert body["has_more"] is True

    async def test_filters_are_forwarded(self, logs_client: httpx.AsyncClient) -> None:
        body = (await logs_client.get("/api/logs?host=a.example&status=200")).json()
        assert [item["request_id"] for item in body["items"]] == ["a"]

    async def test_an_oversized_page_size_is_rejected(self, logs_client: httpx.AsyncClient) -> None:
        """M4-07：无上限时一次请求百万行会占满线程池，DNS 解析随之排队。"""
        response = await logs_client.get("/api/logs?page_size=100000")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_a_deep_page_is_rejected(self, logs_client: httpx.AsyncClient) -> None:
        assert (await logs_client.get("/api/logs?page=999999999")).status_code == 422

    async def test_a_nonsense_status_is_rejected(self, logs_client: httpx.AsyncClient) -> None:
        assert (await logs_client.get("/api/logs?status=99")).status_code == 422

    async def test_switches_carry_the_whole_attempt_chain(
        self, logs_client: httpx.AsyncClient
    ) -> None:
        body = (await logs_client.get("/api/logs/switches")).json()
        assert [item["request_id"] for item in body["items"]] == ["b"]
        attempts = body["items"][0]["attempts"]
        assert [a["attempt_index"] for a in attempts] == [0, 1]
        assert attempts[0]["upstream_name"] == "proxy-a"
        assert body["has_more"] is False


class TestHealthEndpoint:
    async def test_health_lists_upstreams_by_priority(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                body = (await client.get("/api/health")).json()
        finally:
            await app.stop()
        assert [u["name"] for u in body["upstreams"]] == ["proxy-a", "direct"]
        assert body["upstreams"][0]["circuit_state"] == "closed"
        assert body["upstreams"][0]["available"] is True
        assert body["upstreams"][0]["last_success_age_seconds"] is None

    async def test_health_reports_accumulated_traffic(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            app.state.health.add_traffic("proxy-a", bytes_up=100, bytes_down=200)
            async with client_for(app) as client:
                body = (await client.get("/api/health")).json()
        finally:
            await app.stop()
        entry = next(u for u in body["upstreams"] if u["name"] == "proxy-a")
        assert (entry["bytes_up_total"], entry["bytes_down_total"]) == (100, 200)

    async def test_health_reports_the_open_circuit(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            _trip(app)
            async with client_for(app) as client:
                body = (await client.get("/api/health")).json()
        finally:
            await app.stop()
        entry = body["upstreams"][0]
        assert entry["circuit_state"] == "open"
        assert entry["available"] is False
        assert entry["consecutive_failures"] == 5
        assert entry["success_rate"] == 0.0

    async def test_an_elapsed_cooldown_shows_as_half_open(self, tmp_path: Path) -> None:
        """冷却期已过时必须显示 ``half_open``。

        状态迁移是查询时惰性完成的，Web 若直接读字段会显示一个早已不成立的
        ``open``——运维会以为出口还被拦着。
        """
        app = await running(tmp_path)
        try:
            _trip(app, now=time.monotonic() - 3600)
            async with client_for(app) as client:
                body = (await client.get("/api/health")).json()
        finally:
            await app.stop()
        assert body["upstreams"][0]["circuit_state"] == "half_open"
        assert body["upstreams"][0]["available"] is True

    async def test_resetting_closes_the_circuit_and_keeps_the_counters(
        self, tmp_path: Path
    ) -> None:
        app = await running(tmp_path)
        try:
            _trip(app)
            async with client_for(app) as client:
                body = (await client.post("/api/health/proxy-a/reset")).json()
            assert app.state.health.state_of("proxy-a", now=time.monotonic()) is HealthState.CLOSED
        finally:
            await app.stop()
        assert body["circuit_state"] == "closed"
        assert body["consecutive_failures"] == 0
        # 累计计数是看板上历史成功率的唯一来源，重置熔断不该把它清零。
        assert body["total_failure"] == 5

    async def test_resetting_an_unknown_upstream_is_404(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                response = await client.post("/api/health/ghost/reset")
        finally:
            await app.stop()
        assert response.status_code == 404

    async def test_the_reset_is_audited(self, tmp_path: Path) -> None:
        """「这个出口怎么突然恢复了」必须能追溯到人和时间。"""
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                assert (await client.post("/api/health/proxy-a/reset")).status_code == 200
            row = await _wait_for_audit(app.storage.logs_reader)
        finally:
            await app.stop()
        assert row["action"] == "reset_health"
        assert row["target"] == "proxy-a"
        assert row["actor"] == "127.0.0.1"


class TestHostTrafficEndpoint:
    async def test_ranks_hosts_within_the_given_window(self, tmp_path: Path) -> None:
        seed_traffic(
            tmp_path / "logs.db",
            [
                traffic_row(request_id="a", host="big.example", bytes_up=100, bytes_down=900),
                traffic_row(request_id="b", host="small.example", bytes_up=1, bytes_down=1),
            ],
        )
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                body = (
                    await client.get(
                        "/api/traffic/hosts",
                        params={"since": BASE_TS - 1, "until": BASE_TS + 1},
                    )
                ).json()
        finally:
            await app.stop()
        assert [item["host"] for item in body["items"]] == ["big.example", "small.example"]
        assert body["since"] == BASE_TS - 1
        assert body["until"] == BASE_TS + 1

    async def test_defaults_to_today_when_no_range_is_given(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                body = (await client.get("/api/traffic/hosts")).json()
        finally:
            await app.stop()
        assert body["items"] == []
        assert body["since"] <= body["until"]

    async def test_limit_is_applied(self, tmp_path: Path) -> None:
        seed_traffic(
            tmp_path / "logs.db",
            [
                traffic_row(request_id="a", host="a.example", bytes_up=1, bytes_down=1),
                traffic_row(request_id="b", host="b.example", bytes_up=1, bytes_down=1),
            ],
        )
        app = await running(tmp_path)
        try:
            async with client_for(app) as client:
                body = (
                    await client.get(
                        "/api/traffic/hosts",
                        params={"since": BASE_TS - 1, "until": BASE_TS + 1, "limit": 1},
                    )
                ).json()
        finally:
            await app.stop()
        assert len(body["items"]) == 1


def _trip(app: Application, *, now: float | None = None) -> None:
    """把 proxy-a 打到熔断。direct 不行：它的失败一律降级，永不熔断。"""
    at = time.monotonic() if now is None else now
    for _ in range(5):
        app.state.health.record_result(
            "proxy-a", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=at, error="连不上"
        )


async def _wait_for_audit(pool: ReadOnlyPool, *, timeout: float = 5.0) -> sqlite3.Row:
    """等写者线程把审计落盘。批量落盘有时间窗，不能立刻查。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await asyncio.to_thread(
            pool.query, "SELECT actor, action, target FROM config_audit ORDER BY id DESC LIMIT 1"
        )
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError("审计记录未落盘")
