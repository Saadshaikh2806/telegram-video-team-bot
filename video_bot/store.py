import json
import sqlite3
import time
import uuid
from pathlib import Path


SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS editors (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 1,
 fair_load INTEGER NOT NULL DEFAULT 0, last_assigned REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_chat INTEGER NOT NULL,
 source_message INTEGER NOT NULL, file_key TEXT, brief TEXT NOT NULL,
 effort INTEGER NOT NULL CHECK(effort BETWEEN 1 AND 3), created REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', editor_id INTEGER REFERENCES editors(id),
 assigned REAL, due REAL, original_due REAL, card_id INTEGER,
 first_submitted REAL, submitted REAL, approved REAL, revisions INTEGER NOT NULL DEFAULT 0,
 submission_chat INTEGER, submission_message INTEGER, submission_link TEXT,
 UNIQUE(source_chat, source_message)
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_video ON jobs(file_key) WHERE file_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, job_id INTEGER, actor INTEGER, kind TEXT NOT NULL,
 details TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_updates (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE, method TEXT NOT NULL, payload TEXT NOT NULL,
 job_id INTEGER, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0, last_error TEXT
);
'''


class Store:
    def __init__(self, path, database_url=''):
        self.owner = uuid.uuid4().hex
        self.remote = bool(database_url)
        if self.remote:
            from .postgres import PostgresConnection
            self.db = PostgresConnection(database_url, SCHEMA)
            return
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def enqueue(self, method, payload, dedupe=None, job=None):
        self.db.execute('INSERT INTO outbox(dedupe,method,payload,job_id) VALUES (?,?,?,?) ON CONFLICT(dedupe) DO NOTHING',
                        (dedupe, method, json.dumps(payload), job))

    def event(self, job, actor, kind, details, now):
        self.db.execute('INSERT INTO events(job_id,actor,kind,details,at) VALUES (?,?,?,?,?)',
                        (job, actor, kind, details, now))

    def begin_write(self):
        if not self.remote:
            self.db.execute('BEGIN IMMEDIATE')

    def lease(self):
        """Only one Render instance polls/sends during overlapping deployments."""
        if not self.remote:
            return True
        with self.db:
            row = self.db.execute('''INSERT INTO worker_lease(id,owner,expires) VALUES (1,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
                WHERE worker_lease.expires < ? OR worker_lease.owner=excluded.owner RETURNING owner''',
                (self.owner, time.time() + 300, time.time())).fetchone()
        return bool(row)

    def release_lease(self):
        if self.remote:
            with self.db:
                self.db.execute('DELETE FROM worker_lease WHERE owner=?', (self.owner,))
