"""会话落盘 —— 把会话本体写进 SQLite，服务重启后「最近」还能打开。

为什么需要它
------------
原来的会话只活在内存里（``SessionStore`` + 30 分钟 TTL + 200 条上限）。
这造成一个很难察觉的割裂：

- 前端把「最近」列表存在 ``localStorage`` —— **刷新页面，列表还在**
- 但列表指向的会话体在服务端内存里 —— **重启服务 / 过了 TTL 就点不开了**

于是用户看到一份「看起来还在、点进去却说过期」的历史。
``logs/runs.jsonl`` 救不了这个场景：它是为**统计**设计的，
刻意不写计划正文，也没有 session_id。

所以这里补上真正的一层：会话本体落盘。

为什么是 SQLite，不是 JSONL
----------------------------
- 需要「按时间倒序取最近 N 条」「按 id 取一条」「按 id 删一条」——
  SQL 里是三个语句，JSONL 里是三次全文件扫描。
- 会话是一条**会被反复覆盖**的记录（状态一直在变），而 JSONL 只适合追加。
  追加式写法要么产生大量重复行，要么得定期压实。
- ``sqlite3`` 是标准库，项目里景点库已经在用，不引入新依赖。

与 ``agent/run_log.py`` 的分工
------------------------------
::

    logs/runs.jsonl    每次运行一行，给统计 / 评测用，不含计划正文
    logs/sessions.db   每个会话一行，给界面「最近」用，含完整事件与计划

两者互补，不重复。

写入时机
--------
**不在每个事件上写盘** —— 一次运行有 10–30 个事件，写放大很明显。
改为在**状态跃迁**时写（running / awaiting_input / done / failed），
这已经覆盖了所有「崩溃后再看要有意义」的时点。

不做的事
--------
- 不加密、不做多用户隔离。这是单机单用户的本地工具，
  数据库就在 ``logs/`` 下，和 ``runs.jsonl`` 同级。
- 不做 schema 迁移框架。表结构只有一版，字段只增不减；
  真要大改时删掉 ``sessions.db`` 重来即可（历史不是珍贵数据）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from web.session import session_ok

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注
    from web.session import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB_PATH = PROJECT_ROOT / "logs" / "sessions.db"

# 最多保留多少个会话。超出后按创建时间淘汰最旧的。
# 取 500 是个折中：一次规划一条，够翻很久，又不至于让库无限长大。
MAX_ROWS = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    task          TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    events        TEXT    NOT NULL DEFAULT '[]',
    result        TEXT,
    error         TEXT,
    clarification TEXT,
    ok            INTEGER,
    revisions     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions (created_at DESC);
"""

# 后加的列。``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表补列，
# 所以老库要靠下面的 _migrate() 显式 ALTER。
#
# 为什么不能「删库重建」：库里是用户的真实历史。加一列的成本是几毫秒，
# 丢掉用户数据的成本是他再也不会信任这个功能。
_ADDED_COLUMNS: dict[str, str] = {
    "revisions": "TEXT",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def db_path() -> Path:
    """会话库路径（可由 ``SESSION_DB_PATH`` 覆盖）。"""
    raw = _env("SESSION_DB_PATH")
    if not raw:
        return DEFAULT_DB_PATH
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def is_enabled() -> bool:
    """是否启用会话落盘。``SESSION_DB_ENABLED=0`` 可关闭。

    关掉之后行为退回改造前：会话只在内存里，重启即丢。
    保留这个开关是为了让测试和「只想临时跑一下」的场景不受影响。
    """
    return _env("SESSION_DB_ENABLED", "1") not in {"0", "false", "no"}


def _loads(raw: str | None, fallback: Any) -> Any:
    """解析 JSON 列。坏数据返回兜底值，绝不抛异常。"""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


class SessionDB:
    """会话的磁盘副本。所有方法都**不抛异常**，失败时返回空/False。

    为什么全部吞异常：持久化是旁路。磁盘满了、文件被占用、
    库被外部工具锁住 —— 这些都不该让一次正在跑的规划崩掉。
    代价是失败会静默，所以每个失败点都返回可判断的值，方便测试断言。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or db_path()
        self._ready = False
        # 保护 _ensure_schema 的懒初始化。用 threading.Lock 而不是 asyncio.Lock：
        # 这个类的调用来自多个线程（FastAPI 的线程池 + asyncio.to_thread）。
        # 没有它的话两个线程可能同时跑 DDL，`ALTER TABLE ADD COLUMN` 撞车时
        # 异常会被下面的宽 except 吞掉 —— 表现为「那一次写入静默失败」。
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # -- 连接 -------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """每次操作开一个新连接。

        不用长连接的原因：FastAPI 的异步端点会在线程池里跑同步代码，
        长连接需要 ``check_same_thread=False`` + 自己加锁，
        而 SQLite 本地开连接只要几十微秒 —— 不值得为此引入并发复杂度。
        """
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """懒建表 + 补列。只做一次，避免每次读写都重放一遍 DDL。"""
        if self._ready:
            return
        with self._lock:
            # 双检：等锁期间别的线程可能已经建好了
            if self._ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.commit()
            self._ready = True

    # 关于 `PRAGMA journal_mode=WAL`：**实测在本项目里有害，不要加。**
    #
    # 直觉上 WAL 能让读写互不阻塞，看起来正合适。但 WAL 的前提是
    # **长连接**，而本类的 `_connect()` 是「每次操作开一个连接、用完即关」。
    # 连接关闭时若它是最后一个，SQLite 会做 checkpoint 并删除 -wal/-shm，
    # 此时另一个线程正好打开连接就会拿到 SQLITE_READONLY。
    #
    # 实测（Windows，8 线程并发首次写入）：
    #   · 默认 DELETE 模式（预建表）  8/8 成功
    #   · WAL 模式                    1/8 成功 —— 7 次报
    #     "attempt to write a readonly database"
    # 更糟的是 save() 吞异常返回 False，**失败是静默的**：
    # 用户看到的是「历史记录偶尔丢一条」。
    #
    # 串行写入两种模式都正常，所以这个问题只在并发下暴露 ——
    # 而 `_persist` 走 `asyncio.to_thread` 之后并发是常态。

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给已存在的表补上后加的列。

        只做**加列**，不改类型、不删列 —— 加列是幂等且无损的，
        其余操作都有丢数据的风险，而这个库里是用户的真实历史。

        列名来自模块内的常量，不经过用户输入，因此直接拼进 DDL 是安全的
        （SQLite 也不支持用参数占位符代替列名）。
        """
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        for name, decl in _ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")

    # -- 写 ---------------------------------------------------------------

    def save(self, session: "Session") -> bool:
        """整条 upsert。返回是否成功。"""
        try:
            self._ensure_schema()
            row = _snapshot(session)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions
                        (id, task, status, created_at, updated_at,
                         events, result, error, clarification, ok, revisions)
                    VALUES
                        (:id, :task, :status, :created_at, :updated_at,
                         :events, :result, :error, :clarification, :ok, :revisions)
                    ON CONFLICT(id) DO UPDATE SET
                        task          = excluded.task,
                        status        = excluded.status,
                        updated_at    = excluded.updated_at,
                        events        = excluded.events,
                        result        = excluded.result,
                        error         = excluded.error,
                        clarification = excluded.clarification,
                        ok            = excluded.ok,
                        revisions     = excluded.revisions
                    """,
                    row,
                )
                conn.commit()
                self._prune(conn)
            return True
        except Exception:  # noqa: BLE001 - 落盘失败不该影响正在跑的规划
            return False

    def _prune(self, conn: sqlite3.Connection) -> None:
        """只保留最近的 ``MAX_ROWS`` 条。

        先 COUNT 再决定要不要删：原来无条件跑那条
        `DELETE ... WHERE id IN (SELECT ... ORDER BY ... OFFSET 500)`，
        等于**每次写入都做一次全表排序 + 子查询**，而其中 99.8% 的调用
        表根本没满、纯属白做。COUNT 走索引，代价可以忽略。
        """
        row = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        if row is None or int(row["n"]) <= MAX_ROWS:
            return
        conn.execute(
            "DELETE FROM sessions WHERE id IN ("
            "  SELECT id FROM sessions ORDER BY created_at DESC LIMIT -1 OFFSET ?"
            ")",
            (MAX_ROWS,),
        )
        conn.commit()

    def delete(self, session_id: str) -> bool:
        """硬删除。返回是否真的删掉了一行。"""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception:  # noqa: BLE001
            return False

    def mark_interrupted(self, reason: str) -> int:
        """把上次进程遗留的「非终态」会话标成中断。返回处理条数。

        为什么需要：进程被 Ctrl-C 或崩溃时，磁盘上会留下 status=running 的行。
        不处理的话，新进程启动后「最近」里会出现一条永远转圈的记录，
        点进去也永远等不到结果 —— 比直接说「被中断了」更让人困惑。
        """
        try:
            self._ensure_schema()
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE sessions SET status = 'interrupted', error = ?, updated_at = ? "
                    "WHERE status IN ('pending', 'running', 'awaiting_input')",
                    (reason, time.time()),
                )
                conn.commit()
                return cursor.rowcount
        except Exception:  # noqa: BLE001
            return 0

    def clear(self) -> int:
        """清空全部会话（供测试与「清空历史」使用）。返回删除条数。"""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM sessions")
                conn.commit()
                return cursor.rowcount
        except Exception:  # noqa: BLE001
            return 0

    # -- 读 ---------------------------------------------------------------

    def load(self, session_id: str) -> dict[str, Any] | None:
        """按 id 取一条完整记录。"""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            return _row_to_record(row) if row is not None else None
        except Exception:  # noqa: BLE001
            return None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """按创建时间倒序取最近 N 条的**摘要**。

        刻意不返回 events / result —— 列表只需要标题和状态点，
        把每条几十 KB 的计划正文一起捞出来是纯浪费。
        """
        try:
            self._ensure_schema()
            size = max(1, min(int(limit), 200))
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, task, status, created_at, updated_at, ok "
                    "FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (size,),
                ).fetchall()
            return [_row_to_summary(row) for row in rows]
        except Exception:  # noqa: BLE001
            return []

    def count(self) -> int:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
            return int(row["n"]) if row is not None else 0
        except Exception:  # noqa: BLE001
            return 0


# ---------------------------------------------------------------------------
# 行 <-> dict
# ---------------------------------------------------------------------------


def _snapshot(session: "Session") -> dict[str, Any]:
    """把 Session 压成一行。``default=str`` 兜住任何非 JSON 类型。"""
    return {
        "id": session.id,
        "task": session.task,
        "status": session.status,
        "created_at": session.created_wall,
        "updated_at": time.time(),
        "events": json.dumps(session.events, ensure_ascii=False, default=str),
        "result": (
            json.dumps(session.result, ensure_ascii=False, default=str)
            if session.result is not None
            else None
        ),
        "error": session.error,
        "clarification": (
            json.dumps(session.clarification, ensure_ascii=False, default=str)
            if session.clarification
            else None
        ),
        "ok": _ok_flag(session),
        "revisions": (
            json.dumps(session.revisions, ensure_ascii=False, default=str)
            if session.revisions
            else None
        ),
    }


def _ok_flag(session: "Session") -> int | None:
    """把「这次规划成不成功」压成一个可索引的整数列。

    为什么要冗余这一列：列表接口只想显示一个状态点，
    但 ``result.ok`` 埋在几十 KB 的 JSON 里。存一列出来，
    列表查询就不必把正文一起捞出来解析。

    ``None`` 表示**还没有结论**（还在跑），与 ``0``（跑完但没成功）不同 ——
    这个区分正是前端圆点该显示灰还是红。
    """
    value = session_ok(session)
    return None if value is None else int(value)


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": row["id"],
        "task": row["task"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "ok": _ok_from_row(row),
    }


def _ok_from_row(row: sqlite3.Row) -> bool | None:
    """从一行算出「成不成功」。

    刻意**不全靠 ``ok`` 列**：那一列只在 ``save()`` 里写，
    而 ``mark_interrupted()`` 是一条裸 SQL，绕过 ``save`` 直接改 status。
    如果只看列，被标成中断的会话会显示 ``ok=None``（即「还在跑」），
    前端就会给它画一个灰点 —— 明明已经结束了。

    所以这里以 **status 为准**，``ok`` 列只在 ``done`` 时才需要
    （区分「跑完且产出计划」与「跑完但模型输出不合规」）。
    """
    status = row["status"]
    if status in ("failed", "interrupted"):
        return False
    if status != "done":
        return None
    return bool(row["ok"])


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = _row_to_summary(row)
    events = _loads(row["events"], [])
    record["events"] = events if isinstance(events, list) else []
    record["result"] = _loads(row["result"], None)
    record["error"] = row["error"]
    record["clarification"] = _loads(row["clarification"], None)
    revisions = _loads(row["revisions"], [])
    record["revisions"] = revisions if isinstance(revisions, list) else []
    return record


__all__ = [
    "DEFAULT_DB_PATH",
    "MAX_ROWS",
    "SCHEMA",
    "SessionDB",
    "db_path",
    "is_enabled",
]
