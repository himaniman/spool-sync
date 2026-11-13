"""SQLite state: which prints are already deducted (the journal) and which alerts were sent."""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists meta (k text primary key, v text);
create table if not exists handled (
    task_id integer primary key,
    at text not null,          -- when spool-sync handled it (UTC ISO)
    status integer,            -- Bambu task status: 2 finished, 3 failed/aborted
    title text,
    spool_id integer,
    grams real,                -- deducted (0 when nothing was deducted)
    before integer,
    after integer,
    note text
);
create table if not exists alerts (key text primary key, sent_at real not null);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # cursor: prints that started before it are never touched (history stays as it is)
    def since(self):
        row = self.db.execute("select v from meta where k='since'").fetchone()
        return row[0] if row else None

    def set_since(self, iso):
        with self.db:
            self.db.execute("insert or replace into meta values ('since', ?)", (iso,))

    def is_handled(self, task_id):
        return self.db.execute("select 1 from handled where task_id=?", (task_id,)).fetchone() is not None

    def add_handled(self, task_id, status, title, spool_id, grams, before, after, note=""):
        with self.db:
            self.db.execute("insert into handled values (?,?,?,?,?,?,?,?,?)", (
                task_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                status, title, spool_id, grams, before, after, note))

    def journal(self, n):
        return self.db.execute(
            "select at, task_id, status, title, spool_id, grams, before, after, note "
            "from handled order by at desc limit ?", (n,)).fetchall()

    # alerts: send once, or again after `every` seconds while the problem lasts
    def alert_due(self, key, every=None):
        row = self.db.execute("select sent_at from alerts where key=?", (key,)).fetchone()
        return row is None or (every is not None and time.time() - row[0] >= every)

    def alert_sent(self, key):
        with self.db:
            self.db.execute("insert or replace into alerts values (?, ?)", (key, time.time()))

    def alert_clear(self, key):
        with self.db:
            self.db.execute("delete from alerts where key=?", (key,))

    def alert_exists(self, key):
        return self.db.execute("select 1 from alerts where key=?", (key,)).fetchone() is not None
