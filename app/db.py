import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, name TEXT, category TEXT, price REAL, stock INTEGER, description TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, email TEXT, status TEXT, carrier TEXT, tracking TEXT,
    eta TEXT, total REAL, placed_at TEXT, items TEXT
);
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, role TEXT, content TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT, chat_id INTEGER, messages INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0,
    PRIMARY KEY (day, chat_id)
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, name TEXT, summary TEXT,
    status TEXT DEFAULT 'open', created_at TEXT
);
CREATE TABLE IF NOT EXISTS handoff_msgs (
    admin_message_id INTEGER, handoff_id INTEGER
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, body TEXT, hash TEXT, embedding TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, helpful INTEGER, created_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


class Db:
    """A small SQLite wrapper. One connection guarded by a lock is plenty for a bot."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid or 0

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    # ----- conversation memory -------------------------------------------------
    def add_message(self, chat_id: int, role: str, content: str) -> None:
        self.run(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, now()),
        )

    def history(self, chat_id: int, turns: int) -> list[dict]:
        rows = self.all(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, turns * 3),
        )
        return list(reversed(rows))

    def forget(self, chat_id: int) -> None:
        self.run("DELETE FROM messages WHERE chat_id=?", (chat_id,))

    # ----- spending ------------------------------------------------------------
    def record_usage(self, chat_id: int, prompt: int, completion: int, cost: float, message: bool = True) -> None:
        self.run(
            "INSERT INTO usage (day, chat_id, messages, prompt_tokens, completion_tokens, cost) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(day, chat_id) DO UPDATE SET messages=messages+excluded.messages, "
            "prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
            "completion_tokens=completion_tokens+excluded.completion_tokens, cost=cost+excluded.cost",
            (today(), chat_id, 1 if message else 0, prompt, completion, cost),
        )

    def user_messages_today(self, chat_id: int) -> int:
        row = self.one("SELECT messages FROM usage WHERE day=? AND chat_id=?", (today(), chat_id))
        return row["messages"] if row else 0

    def spend_today(self) -> float:
        row = self.one("SELECT COALESCE(SUM(cost),0) AS c FROM usage WHERE day=?", (today(),))
        return float(row["c"])

    # ----- orders --------------------------------------------------------------
    def order(self, order_id: str) -> dict | None:
        row = self.one("SELECT * FROM orders WHERE id=?", (order_id.strip().upper(),))
        if row:
            row["items"] = json.loads(row["items"])
        return row
