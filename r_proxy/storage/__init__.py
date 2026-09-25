"""存储层：唯一写者线程 + 有界队列 + 只读连接。

对应设计：docs/design/DD_STORAGE.md。

热路径**禁止**同步数据库 I/O：路由决策只读内存状态，所有写入经队列交给唯一
的写者线程批量落盘。实测依据见 study/sqlite-storage-benchmark.md——单行独立
提交要 0.11–0.47ms，批量后单行只要 0.0025ms。
"""

from r_proxy.storage.queue import Priority, WriteOp, WriteQueue
from r_proxy.storage.schema import SCHEMA_VERSIONS, Database, StorageError, migrate, open_write
from r_proxy.storage.writer import WriterMetrics, WriterThread

__all__ = [
    "SCHEMA_VERSIONS",
    "Database",
    "Priority",
    "StorageError",
    "WriteOp",
    "WriteQueue",
    "WriterMetrics",
    "WriterThread",
    "migrate",
    "open_write",
]
