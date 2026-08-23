"""SQLite 写入吞吐与并发丢更新基准。

用于验证 r-proxy 存储层选型（见 ../sqlite-storage-benchmark.md）。

运行：
    /media/data/venv/bin/python study/scripts/sqlite_bench.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time

# 默认使用真实磁盘而非 tmpfs：/tmp 在多数发行版是内存文件系统，
# 测得的写入吞吐会高出一个数量级，无法反映实际部署表现。
DB_DIR = os.environ.get("BENCH_DIR", "/media/data/_r_proxy_sqlite_bench")
ROWS = 20000
BATCH = 500
WORKERS = 4
PER_WORKER = 500

LOG_ROW = (
    "www.example.com",
    "http://www.example.com/some/path?q=1",
    "home-proxy",
    10,
    0,
    200,
    None,
    84,
    1770000000,
)
INSERT_LOG = (
    "INSERT INTO request_log (host,url,upstream_name,upstream_priority,"
    "attempt_index,http_status,error,elapsed_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
)


def fresh_db(name: str) -> str:
    os.makedirs(DB_DIR, exist_ok=True)
    path = os.path.join(DB_DIR, name)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    return path


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT, url TEXT, upstream_name TEXT,
            upstream_priority INTEGER, attempt_index INTEGER,
            http_status INTEGER, error TEXT,
            elapsed_ms INTEGER, created_at INTEGER)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS host_upstream (
            host TEXT PRIMARY KEY, upstream_name TEXT, source TEXT,
            last_url TEXT, last_success_at INTEGER, last_http_status INTEGER,
            fail_count INTEGER DEFAULT 0, updated_at INTEGER)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_log_host ON request_log(host, created_at)")


def bench_autocommit() -> float:
    conn = connect(fresh_db("a.db"))
    create_schema(conn)
    start = time.perf_counter()
    for _ in range(ROWS):
        conn.execute(INSERT_LOG, LOG_ROW)
    elapsed = time.perf_counter() - start
    conn.close()
    return ROWS / elapsed


def bench_batched() -> float:
    conn = connect(fresh_db("b.db"))
    create_schema(conn)
    start = time.perf_counter()
    for _ in range(0, ROWS, BATCH):
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(INSERT_LOG, [LOG_ROW] * BATCH)
        conn.execute("COMMIT")
    elapsed = time.perf_counter() - start
    conn.close()
    return ROWS / elapsed


def bench_upsert() -> float:
    conn = connect(fresh_db("c.db"))
    create_schema(conn)
    n = 5000
    start = time.perf_counter()
    for i in range(n):
        conn.execute(
            """INSERT INTO host_upstream
                 (host,upstream_name,source,last_success_at,fail_count,updated_at)
               VALUES (?,?,'auto',?,0,?)
               ON CONFLICT(host) DO UPDATE SET
                 upstream_name=excluded.upstream_name,
                 last_success_at=excluded.last_success_at,
                 fail_count=0,
                 updated_at=excluded.updated_at
               WHERE host_upstream.source != 'manual'""",
            (f"host{i % 200}.example.com", "home-proxy", 1770000000 + i, 1770000000 + i),
        )
    elapsed = time.perf_counter() - start
    conn.close()
    return n / elapsed


def bench_read_modify_write(mode: str) -> None:
    """Python 侧读改写：deferred 事务会大量丢更新。"""
    path = fresh_db(f"rmw_{mode}.db")
    setup = connect(path)
    create_schema(setup)
    setup.execute(
        "INSERT INTO host_upstream (host,upstream_name,source,fail_count,updated_at)"
        " VALUES ('shared.example.com','home-proxy','auto',0,0)"
    )
    setup.close()

    busy = [0]
    lock = threading.Lock()
    begin = "BEGIN IMMEDIATE" if mode == "immediate" else "BEGIN"

    def worker() -> None:
        conn = connect(path)
        local = 0
        for _ in range(PER_WORKER):
            try:
                conn.execute(begin)
                row = conn.execute(
                    "SELECT fail_count FROM host_upstream WHERE host='shared.example.com'"
                ).fetchone()
                conn.execute(
                    "UPDATE host_upstream SET fail_count=? WHERE host='shared.example.com'",
                    (row[0] + 1,),
                )
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                local += 1
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
        conn.close()
        with lock:
            busy[0] += local

    _run_workers(worker)
    _report(f"Python 读改写 / {mode}", path, busy[0])


def bench_sql_increment(key: str, label: str, statement: str) -> None:
    """SQL 侧自增：单条语句原子，无需显式事务。"""
    path = fresh_db(f"inc_{key}.db")
    setup = connect(path)
    create_schema(setup)
    setup.execute(
        "INSERT INTO host_upstream (host,upstream_name,source,fail_count,updated_at)"
        " VALUES ('shared.example.com','home-proxy','auto',0,0)"
    )
    setup.close()

    busy = [0]
    lock = threading.Lock()

    def worker() -> None:
        conn = connect(path)
        local = 0
        for _ in range(PER_WORKER):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                local += 1
        conn.close()
        with lock:
            busy[0] += local

    _run_workers(worker)
    _report(label, path, busy[0])


def _run_workers(worker) -> None:
    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _report(label: str, path: str, busy: int) -> None:
    conn = connect(path)
    final = conn.execute(
        "SELECT fail_count FROM host_upstream WHERE host='shared.example.com'"
    ).fetchone()[0]
    conn.close()
    expected = WORKERS * PER_WORKER
    status = "OK" if final == expected and busy == 0 else f"丢失 {expected - final} 次更新"
    print(f"    {label:26s} final={final:5d}/{expected}  busy={busy:5d}  {status}")


def bench_manual_guard() -> None:
    """UPSERT 的 WHERE 条件必须保护 Web 界面的手动绑定不被 auto 覆盖。"""
    conn = connect(fresh_db("guard.db"))
    create_schema(conn)
    conn.execute(
        "INSERT INTO host_upstream (host,upstream_name,source,last_success_at,"
        "fail_count,updated_at) VALUES ('x.example.com','proxy-c','manual',100,0,100)"
    )
    conn.execute(
        """INSERT INTO host_upstream
             (host,upstream_name,source,last_success_at,fail_count,updated_at)
           VALUES ('x.example.com','proxy-a','auto',200,0,200)
           ON CONFLICT(host) DO UPDATE SET
             upstream_name=excluded.upstream_name,
             last_success_at=excluded.last_success_at,
             fail_count=0,
             updated_at=excluded.updated_at
           WHERE host_upstream.source != 'manual'"""
    )
    row = conn.execute(
        "SELECT upstream_name, source FROM host_upstream WHERE host='x.example.com'"
    ).fetchone()
    conn.close()
    status = "OK" if row == ("proxy-c", "manual") else "FAILED"
    print(f"    manual 绑定保护            结果={row}  {status}")


def bench_read_during_write() -> tuple[float, float]:
    path = fresh_db("e.db")
    conn = connect(path)
    create_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(INSERT_LOG, [LOG_ROW] * 100000)
    conn.execute("COMMIT")
    conn.close()

    stop = threading.Event()

    def writer() -> None:
        w = connect(path)
        while not stop.is_set():
            w.execute("BEGIN IMMEDIATE")
            w.executemany(INSERT_LOG, [LOG_ROW] * BATCH)
            w.execute("COMMIT")
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.2)

    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    latencies = []
    for _ in range(20):
        start = time.perf_counter()
        reader.execute(
            "SELECT * FROM request_log WHERE host=? ORDER BY created_at DESC LIMIT 50",
            ("www.example.com",),
        ).fetchall()
        latencies.append((time.perf_counter() - start) * 1000)
    reader.close()
    stop.set()
    t.join()
    latencies.sort()
    return sum(latencies) / len(latencies), latencies[-1]


def main() -> None:
    print(f"SQLite {sqlite3.sqlite_version}  |  临时目录 {DB_DIR}\n")

    print("[1] 写入吞吐")
    print(f"    单行自动提交              {bench_autocommit():>10,.0f} rows/s")
    print(f"    批量 {BATCH} 行/事务        {bench_batched():>10,.0f} rows/s")
    print(f"    粘性 UPSERT               {bench_upsert():>10,.0f} ops/s\n")

    print(f"[2] 并发丢更新（{WORKERS} 线程 × {PER_WORKER} 次自增）")
    bench_read_modify_write("deferred")
    bench_read_modify_write("immediate")
    bench_sql_increment(
        "update",
        "SQL 自增 / 自动提交",
        "UPDATE host_upstream SET fail_count = fail_count + 1 WHERE host='shared.example.com'",
    )
    bench_sql_increment(
        "upsert",
        "UPSERT SQL 自增",
        "INSERT INTO host_upstream (host,upstream_name,source,fail_count,updated_at)"
        " VALUES ('shared.example.com','home-proxy','auto',1,0)"
        " ON CONFLICT(host) DO UPDATE SET fail_count = host_upstream.fail_count + 1",
    )
    print()

    print("[3] manual 绑定保护")
    bench_manual_guard()
    print()

    print("[4] 写入进行中的只读查询延迟（10 万行表）")
    avg, mx = bench_read_during_write()
    print(f"    avg={avg:.2f}ms  max={mx:.2f}ms")

    shutil.rmtree(DB_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
