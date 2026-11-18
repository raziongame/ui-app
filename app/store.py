"""Chat history on the server, per browser (operator 2026-11-17). SQLite from the standard library;
the file lives in data/ (gitignored). Chats are kept until the user deletes them.

A browser is identified by a random id in an HttpOnly cookie; a chat is only ever read, listed or
deleted with the browser id that created it. Engine results are stored per chat so follow-up
questions can cite them ("result N" numbers continue across the chat).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, browser TEXT NOT NULL, title TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chats_browser ON chats(browser, updated);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT, error INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS results (
  chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, n INTEGER NOT NULL,
  tool TEXT NOT NULL, arguments TEXT NOT NULL, structured TEXT, PRIMARY KEY (chat_id, n));
"""

TITLE_MAX = 60


def title_for(message: str) -> str:
    """A short chat title: the question line of a pasted text (last line ending in "?"), else the
    first non-empty line."""
    lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
    if not lines:
        return "New chat"
    pick = next((ln for ln in reversed(lines) if ln.endswith("?")), lines[0]) if len(lines) > 1 else lines[0]
    return pick if len(pick) <= TITLE_MAX else pick[:TITLE_MAX - 1].rstrip() + "…"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # ---- chats ----
    def create_chat(self, browser: str, title: str) -> dict[str, Any]:
        now = time.time()
        cid = uuid.uuid4().hex
        with self._lock:
            self._db.execute("INSERT INTO chats VALUES (?,?,?,?,?)", (cid, browser, title, now, now))
        return {"id": cid, "title": title, "updated": now}

    def get_chat(self, browser: str, chat_id: str | None) -> dict[str, Any] | None:
        if not chat_id:
            return None
        row = self._db.execute("SELECT id, title, updated FROM chats WHERE id=? AND browser=?",
                               (chat_id, browser)).fetchone()
        return dict(row) if row else None

    def list_chats(self, browser: str) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT id, title, updated FROM chats WHERE browser=? ORDER BY updated DESC",
                                (browser,)).fetchall()
        return [dict(r) for r in rows]

    def delete_chat(self, browser: str, chat_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM chats WHERE id=? AND browser=?", (chat_id, browser))
        return cur.rowcount > 0

    # ---- messages ----
    def add_message(self, chat_id: str, role: str, content: str, meta: dict[str, Any] | None = None,
                    error: bool = False) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (chat_id, role, content, meta, error, created) VALUES (?,?,?,?,?,?)",
                (chat_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None, int(error), now))
            self._db.execute("UPDATE chats SET updated=? WHERE id=?", (now, chat_id))
        return int(cur.lastrowid)

    def messages(self, chat_id: str, with_meta: bool = False) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT id, role, content, meta, error, created FROM messages "
                                "WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
        out = []
        for r in rows:
            m = {"id": r["id"], "role": r["role"], "content": r["content"], "error": bool(r["error"]),
                 "created": r["created"]}
            if with_meta and r["meta"]:
                m["meta"] = json.loads(r["meta"])
            out.append(m)
        return out

    # ---- engine results (numbered across the chat) ----
    def results(self, chat_id: str) -> list[dict[str, Any] | None]:
        rows = self._db.execute("SELECT n, structured FROM results WHERE chat_id=? ORDER BY n", (chat_id,)).fetchall()
        return [json.loads(r["structured"]) if r["structured"] else None for r in rows]

    def add_results(self, chat_id: str, first_n: int, entries: list[tuple[str, dict[str, Any], Any]]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT INTO results VALUES (?,?,?,?,?)",
                [(chat_id, first_n + i, tool, json.dumps(args, ensure_ascii=False),
                  json.dumps(structured, ensure_ascii=False) if structured is not None else None)
                 for i, (tool, args, structured) in enumerate(entries)])
