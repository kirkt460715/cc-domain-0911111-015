import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from ..primitives import canonical, digest, decode, error

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parties(id TEXT PRIMARY KEY,name TEXT NOT NULL,aliases TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,party TEXT NOT NULL,currency TEXT NOT NULL,
 amount INTEGER NOT NULL,issued TEXT NOT NULL,due TEXT,reference TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS postings(sequence INTEGER PRIMARY KEY AUTOINCREMENT,entry TEXT NOT NULL,document TEXT NOT NULL,
 account TEXT NOT NULL,amount INTEGER NOT NULL,effective TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY,source TEXT NOT NULL,invoice TEXT NOT NULL,amount INTEGER NOT NULL,
 effective TEXT NOT NULL,reversed TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reversals(id TEXT PRIMARY KEY,target TEXT NOT NULL,kind TEXT NOT NULL,effective TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_reversals(document TEXT PRIMARY KEY,effective TEXT NOT NULL,reversal TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_lines(id TEXT PRIMARY KEY,account TEXT NOT NULL,currency TEXT NOT NULL,amount INTEGER NOT NULL,
 booked TEXT NOT NULL,reference TEXT NOT NULL,payer TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_links(line TEXT PRIMARY KEY,payment TEXT NOT NULL UNIQUE,linked_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS statements(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,result TEXT);
CREATE TABLE IF NOT EXISTS disputes(id TEXT PRIMARY KEY,invoice TEXT NOT NULL,amount INTEGER NOT NULL,opened TEXT NOT NULL,
 closed TEXT,reason TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS periods(id TEXT PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL,state TEXT NOT NULL,digest TEXT);
CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,entity TEXT NOT NULL,
 payload TEXT NOT NULL,previous TEXT NOT NULL,digest TEXT NOT NULL);
"""


class Database:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:" and Path(path).is_symlink():
            error("path", "database cannot be symlink")
        self.connection = sqlite3.connect(
            path, isolation_level=None, timeout=5, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(DDL)
        self.lock = threading.RLock()
        self.depth = 0
        old = self.one("SELECT value FROM meta WHERE key='version'")
        if old and old["value"] != "1":
            error("schema", "unsupported ledger schema")
        self.connection.execute("INSERT OR IGNORE INTO meta VALUES('version','1')")

    @contextmanager
    def transaction(self):
        with self.lock:
            depth = self.depth
            self.connection.execute(
                "BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT tx{depth}"
            )
            self.depth += 1
            try:
                yield self.connection
            except BaseException:
                if depth == 0:
                    self.connection.execute("ROLLBACK")
                else:
                    self.connection.execute(f"ROLLBACK TO tx{depth}")
                    self.connection.execute(f"RELEASE tx{depth}")
                raise
            else:
                self.connection.execute(
                    "COMMIT" if depth == 0 else f"RELEASE tx{depth}"
                )
            finally:
                self.depth -= 1

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def audit(self, kind, entity, payload):
        old = self.one(
            "SELECT sequence,digest FROM audit ORDER BY sequence DESC LIMIT 1"
        )
        seq = old["sequence"] + 1 if old else 1
        previous = old["digest"] if old else "0" * 64
        checksum = digest(
            {
                "sequence": seq,
                "kind": kind,
                "entity": entity,
                "payload": payload,
                "previous": previous,
            }
        )
        self.connection.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?)",
            (seq, kind, entity, canonical(payload), previous, checksum),
        )

    def verify_audit(self):
        previous = "0" * 64
        rows = self.rows("SELECT * FROM audit ORDER BY sequence")
        for seq, row in enumerate(rows, 1):
            checksum = digest(
                {
                    "sequence": seq,
                    "kind": row["kind"],
                    "entity": row["entity"],
                    "payload": decode(row["payload"]),
                    "previous": previous,
                }
            )
            if (
                row["sequence"] != seq
                or row["previous"] != previous
                or row["digest"] != checksum
            ):
                error("integrity", "audit chain mismatch", sequence=seq)
            previous = row["digest"]
        return {"entries": len(rows), "head": previous}

    def close(self):
        if self.depth:
            error("state", "active transaction cannot close")
        self.connection.close()

    def backup(self, path):
        if Path(path).exists():
            error("conflict", "backup destination exists")
        with self.lock:
            if self.depth:
                error("state", "backup requires no transaction")
            target = sqlite3.connect(path)
            try:
                self.connection.backup(target)
            finally:
                target.close()
        return str(path)
