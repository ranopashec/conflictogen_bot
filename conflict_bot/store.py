"""SQLite persistence. API credentials are encrypted, never stored as plaintext."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet


class Store:
    def __init__(self, path: str, encryption_key: str):
        self.cipher = Fernet(encryption_key.encode())
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, provider TEXT, secret BLOB, model TEXT,
                topic TEXT DEFAULT 'любая', mode TEXT DEFAULT 'случайный'
            );
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY, user_id INTEGER, body TEXT,
                created TEXT DEFAULT CURRENT_TIMESTAMP, rating INTEGER
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
                PRIMARY KEY(user_id, day)
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS library (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
                situation TEXT NOT NULL, source_case INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, source_case)
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                title TEXT NOT NULL, situation TEXT NOT NULL, status TEXT DEFAULT 'pending',
                reviewer INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_submission
                ON submissions(library_id) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS public_cases (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, situation TEXT NOT NULL,
                origin TEXT UNIQUE NOT NULL, active INTEGER DEFAULT 1,
                created TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.db.row_factory = sqlite3.Row

    def user(self, uid):
        self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (uid,))
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def configure(self, uid, provider, key, model):
        self.user(uid)
        self.db.execute("UPDATE users SET provider=?, secret=?, model=? WHERE id=?",
                        (provider, self.cipher.encrypt(key.encode()), model, uid))
        self.db.commit()

    def key(self, uid):
        secret = self.user(uid)["secret"]
        return self.cipher.decrypt(secret).decode() if secret else None

    def setting(self, uid, field, value):
        if field not in {"topic", "mode", "model"}:
            raise ValueError("Unknown setting")
        self.user(uid)
        self.db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
        self.db.commit()

    def forget_key(self, uid):
        self.db.execute("UPDATE users SET provider=NULL, secret=NULL, model=NULL WHERE id=?", (uid,))
        self.db.commit()

    def erase(self, uid):
        # Published snapshots remain in the shared bank; they contain no author identity.
        for table, column in (("users", "id"), ("cases", "user_id"), ("usage", "user_id"),
                              ("library", "user_id"), ("submissions", "user_id")):
            self.db.execute(f"DELETE FROM {table} WHERE {column}=?", (uid,))
        self.db.commit()

    def library_add(self, uid, title, situation, source_case=None):
        if not 3 <= len(title) <= 100 or not 30 <= len(situation) <= 2800:
            raise ValueError("invalid library entry")
        if source_case is not None and self.case(uid, source_case) is None:
            return None
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO library(user_id,title,situation,source_case) VALUES (?,?,?,?)",
                (uid, title, situation, source_case))
        if cur.rowcount:
            return cur.lastrowid
        return self.db.execute("SELECT id FROM library WHERE user_id=? AND source_case=?",
                               (uid, source_case)).fetchone()[0]

    def library_entry(self, uid, entry_id):
        row = self.db.execute("SELECT id,title,situation FROM library WHERE id=? AND user_id=?",
                              (entry_id, uid)).fetchone()
        return dict(row) if row else None

    def library_page(self, uid, offset=0):
        return [dict(r) for r in self.db.execute(
            "SELECT id,title,situation FROM library WHERE user_id=? ORDER BY id DESC LIMIT 6 OFFSET ?",
            (uid, offset))]

    def library_delete(self, uid, entry_id):
        with self.db:
            self.db.execute("DELETE FROM submissions WHERE library_id=? AND user_id=? AND status='pending'",
                            (entry_id, uid))
            cur = self.db.execute("DELETE FROM library WHERE id=? AND user_id=?", (entry_id, uid))
        return cur.rowcount == 1

    def submit(self, uid, entry_id):
        entry = self.library_entry(uid, entry_id)
        if not entry:
            return False
        # A proposal is an explicit snapshot, never a grant to browse the private bank.
        existing = self.db.execute("SELECT 1 FROM submissions WHERE library_id=? AND status IN ('pending','approved')",
                                   (entry_id,)).fetchone()
        if existing:
            return False
        with self.db:
            self.db.execute("INSERT INTO submissions(library_id,user_id,title,situation) VALUES (?,?,?,?)",
                            (entry_id, uid, entry["title"], entry["situation"]))
        return True

    def moderation_page(self, offset=0):
        return [dict(r) for r in self.db.execute(
            "SELECT id,title,situation FROM submissions WHERE status='pending' ORDER BY id LIMIT 6 OFFSET ?", (offset,))]

    def submission(self, sid):
        row = self.db.execute("SELECT id,title,situation FROM submissions WHERE id=? AND status='pending'", (sid,)).fetchone()
        return dict(row) if row else None

    def moderate(self, sid, admin, approve):
        with self.db:
            row = self.submission(sid)
            if not row:
                return False
            self.db.execute("UPDATE submissions SET status=?,reviewer=? WHERE id=? AND status='pending'",
                            ('approved' if approve else 'rejected', admin, sid))
            if approve:
                self.db.execute("INSERT OR IGNORE INTO public_cases(title,situation,origin) VALUES (?,?,?)",
                                (row["title"], row["situation"], f"submission:{uuid4().hex}"))
        return True

    def seed_public(self, examples):
        with self.db:
            for entry in examples:
                self.db.execute("INSERT OR IGNORE INTO public_cases(title,situation,origin) VALUES (?,?,?)",
                                (entry["title"], entry["situation"], f"school:{entry['id']}"))

    def public_page(self, offset=0):
        return [dict(r) for r in self.db.execute(
            "SELECT id,title,situation FROM public_cases WHERE active=1 ORDER BY id DESC LIMIT 6 OFFSET ?", (offset,))]

    def public_entry(self, cid):
        r = self.db.execute("SELECT id,title,situation FROM public_cases WHERE id=? AND active=1", (cid,)).fetchone()
        return dict(r) if r else None

    def unpublish(self, cid):
        with self.db:
            cur = self.db.execute("UPDATE public_cases SET active=0 WHERE id=? AND active=1", (cid,))
        return cur.rowcount == 1

    def reserve(self, uid, limit):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO usage(user_id,day) VALUES (?,?)", (uid, day))
            cur = self.db.execute("UPDATE usage SET count=count+1 WHERE user_id=? AND day=? AND count<?",
                                  (uid, day, limit))
        return cur.rowcount == 1

    def add_case(self, uid, case):
        cur = self.db.execute("INSERT INTO cases(user_id,body) VALUES (?,?)",
                              (uid, json.dumps(case, ensure_ascii=False)))
        self.db.commit()
        return cur.lastrowid

    def recent(self, uid, limit=30):
        return [dict(id=r["id"], **json.loads(r["body"])) for r in self.db.execute(
            "SELECT id,body FROM cases WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))]

    def case(self, uid, case_id):
        r = self.db.execute("SELECT body FROM cases WHERE id=? AND user_id=?", (case_id, uid)).fetchone()
        return json.loads(r[0]) if r else None

    def rate(self, uid, case_id, rating):
        self.db.execute("UPDATE cases SET rating=? WHERE id=? AND user_id=?", (rating, case_id, uid))
        self.db.commit()

    def cursor(self):
        r = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(r[0]) if r else 0

    def set_cursor(self, offset):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(offset),))
        self.db.commit()
