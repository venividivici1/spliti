"""Clear expenses from the Spliti database.

Hard-deletes expense rows (their `expense_shares` go too, via ON DELETE CASCADE).
Destructive and irreversible — it prints what it will remove and asks before doing
it, unless you pass --yes. Settlements are left alone unless --include-settlements.

    python scripts/clear_expenses.py                  # all expenses, with a prompt
    python scripts/clear_expenses.py --group Spiti     # only that group's expenses
    python scripts/clear_expenses.py --deleted-only    # purge only soft-deleted ones
    python scripts/clear_expenses.py --include-settlements --yes
    python scripts/clear_expenses.py --dry-run         # show counts, change nothing

Targets spliti/split.db by default; override with --db or the usual DB_PATH the app
uses. Back the file up first if you might want the data back.
"""

import argparse
import sys
from pathlib import Path

# Prefer the repo's spliti package over any (possibly stale) pip-installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spliti import db  # noqa: E402


def _resolve_group(conn, group: str | None) -> int | None:
    """Map a --group value (numeric id or name) to a group id, or None for all."""
    if group is None:
        return None
    row = None
    if group.isdigit():
        row = conn.execute("SELECT id FROM groups WHERE id = ?", (int(group),)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM groups WHERE name = ? COLLATE NOCASE", (group,)
        ).fetchone()
    if row is None:
        sys.exit(f"no group matching {group!r}")
    return row["id"]


def _where(gid: int | None, deleted_only: bool) -> tuple[str, list]:
    """Build a shared WHERE clause + params for the expense selection."""
    clauses, params = [], []
    if gid is not None:
        clauses.append("group_id = ?")
        params.append(gid)
    if deleted_only:
        clauses.append("deleted_at IS NOT NULL")
    sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def main() -> None:
    ap = argparse.ArgumentParser(description="Clear expenses from the Spliti DB.")
    ap.add_argument("--group", help="restrict to one group (id or name); default all")
    ap.add_argument("--db", help="path to the SQLite file (default: app's split.db)")
    ap.add_argument(
        "--deleted-only", action="store_true",
        help="only purge soft-deleted expenses (clear the trash)",
    )
    ap.add_argument(
        "--include-settlements", action="store_true",
        help="also delete settlements for the same scope",
    )
    ap.add_argument("--dry-run", action="store_true", help="show counts, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    conn = db.connect(args.db)  # foreign_keys = ON, so shares cascade
    try:
        gid = _resolve_group(conn, args.group)
        where, params = _where(gid, args.deleted_only)

        n_exp = conn.execute(
            f"SELECT COUNT(*) c FROM expenses{where}", params
        ).fetchone()["c"]
        n_shares = conn.execute(
            f"SELECT COUNT(*) c FROM expense_shares "
            f"WHERE expense_id IN (SELECT id FROM expenses{where})",
            params,
        ).fetchone()["c"]

        scope = f"group {args.group!r}" if args.group else "all groups"
        if args.deleted_only:
            scope += " (soft-deleted only)"
        print(f"Target: {scope}")
        print(f"  expenses:       {n_exp}")
        print(f"  expense_shares: {n_shares} (cascade)")

        n_settle = 0
        if args.include_settlements:
            swhere, sparams = (" WHERE group_id = ?", [gid]) if gid is not None else ("", [])
            n_settle = conn.execute(
                f"SELECT COUNT(*) c FROM settlements{swhere}", sparams
            ).fetchone()["c"]
            print(f"  settlements:    {n_settle}")

        if n_exp == 0 and n_settle == 0:
            print("Nothing to clear.")
            return
        if args.dry_run:
            print("Dry run — nothing changed.")
            return
        if not args.yes:
            reply = input("Permanently delete the above? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted.")
                return

        # Deleting the expense rows cascades to expense_shares (foreign_keys = ON).
        conn.execute(f"DELETE FROM expenses{where}", params)
        if args.include_settlements:
            swhere, sparams = (" WHERE group_id = ?", [gid]) if gid is not None else ("", [])
            conn.execute(f"DELETE FROM settlements{swhere}", sparams)
        conn.commit()
        print(f"Deleted {n_exp} expense(s), {n_shares} share(s)"
              + (f", {n_settle} settlement(s)" if args.include_settlements else "")
              + ".")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
