"""SQLite persistence; caller holds the service lock for compound operations."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(directory / "server.lock"), thread_local=False)
        try:
            self.lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("Data directory already in use; run one server process.") from exc
        self.db = sqlite3.connect(
            directory / "state.sqlite3", check_same_thread=False, isolation_level=None
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE INDEX IF NOT EXISTS objects_owner ON objects(kind,owner);
            CREATE TABLE IF NOT EXISTS events (
                job TEXT NOT NULL, seq INTEGER NOT NULL, created REAL NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(job,seq));
            CREATE TABLE IF NOT EXISTS idempotency (
                owner TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, job TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(owner,operation,key));
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def put(self, kind, identifier, owner, data):
        self.db.execute(
            "INSERT OR REPLACE INTO objects VALUES (?,?,?,?)",
            (kind, identifier, owner, json.dumps(data, ensure_ascii=False)),
        )

    def get(self, kind, identifier, owner=None):
        row = self.db.execute(
            "SELECT owner,data FROM objects WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()
        if row is None or (owner is not None and owner != row[0]):
            return None
        return json.loads(row[1])

    def all(self, kind, owner=None):
        if owner is None:
            rows = self.db.execute("SELECT data FROM objects WHERE kind=?", (kind,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT data FROM objects WHERE kind=? AND owner=?", (kind, owner)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, kind, identifier):
        self.db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, identifier))

    def close(self):
        self.db.close()
        self.lock.release()
