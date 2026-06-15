"""SQLite storage for the split app. Money is stored as integer paise everywhere."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "split.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS members (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    description  TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    paid_by      INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    -- who recorded the expense (the signed-in member); kept distinct from paid_by.
    -- NULL on rows created before this was tracked, or if that member is removed.
    added_by     INTEGER REFERENCES members(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at   TEXT
);

CREATE TABLE IF NOT EXISTS expense_shares (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    share_paise INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    from_member  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    to_member    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for DBs created before the current schema."""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    # cents -> paise rename (INR subunit) on pre-existing tables
    if "amount_cents" in cols("expenses"):
        conn.execute("ALTER TABLE expenses RENAME COLUMN amount_cents TO amount_paise")
    if "share_cents" in cols("expense_shares"):
        conn.execute("ALTER TABLE expense_shares RENAME COLUMN share_cents TO share_paise")
    if "amount_cents" in cols("settlements"):
        conn.execute("ALTER TABLE settlements RENAME COLUMN amount_cents TO amount_paise")
    # soft-delete support
    if "deleted_at" not in cols("expenses"):
        conn.execute("ALTER TABLE expenses ADD COLUMN deleted_at TEXT")
    # track who recorded each expense (distinct from who paid)
    if "added_by" not in cols("expenses"):
        conn.execute(
            "ALTER TABLE expenses ADD COLUMN added_by INTEGER REFERENCES members(id)"
        )
