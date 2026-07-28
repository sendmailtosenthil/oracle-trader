"""One-time migration: a trade leg's P&L splits into banked + live.

Usage (from the project root):

    python -m migrations.add_settled_pnl [path-to-db] [--dry-run]

Defaults to ``oracle.db``. Safe to re-run.

Why: a leg used to carry one ``frozen_pnl``, pinned the first time the position
stopped being open. That loses money in two ways. Close a contract and open the
same one again and the banked figure was ignored while the new position ran, then
never updated when it closed — the second cycle vanished. And ``frozen_pnl``
could not tell an automatic capture from a hand-typed correction.

So a leg now keeps:

  settled_pnl       the running total of completed cycles (automatic)
  settled_override  the user's correction of that total, when they made one
  settled_base      what settled_pnl stood at when that correction was typed, so
                    later cycles add to it rather than being swallowed by it
  cycle_open        whether a position is currently running against the leg
  last_mark_pnl     the newest live mark, banked as a fallback if Kite drops the
                    row before it is ever seen at quantity 0
  cycles            how many cycles have closed — 0 means nothing has ever
                    settled, which is not the same as settling to zero

What this does:
  1. ALTER TABLE ztrade_group_legs ADD COLUMN settled_pnl / settled_override /
     cycle_open / last_mark_pnl (if missing).
  2. Copies each leg's ``frozen_pnl`` into ``settled_pnl`` — the same rupees,
     now in the accumulator, so existing groups keep marking to the same total.
  3. Leaves ``cycle_open`` alone: the poller sets it the first time it sees a
     leg actually open. Guessing it here would invent a completed cycle for legs
     whose position was already gone, and they would then report a settled ₹0.00
     for a close that never happened.

``frozen_pnl`` is left in place, read by nothing, so this is reversible by
checking out the previous release.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = 'ztrade_group_legs'
COLUMNS = [('settled_pnl', 'FLOAT DEFAULT 0.0'),
           ('settled_override', 'FLOAT'),
           ('settled_base', 'FLOAT'),
           ('cycle_open', 'BOOLEAN DEFAULT 0'),
           ('last_mark_pnl', 'FLOAT'),
           ('cycles', 'INTEGER DEFAULT 0')]


def _column_exists(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def ensure_columns(conn, dry_run=False):
    added = []
    for name, decl in COLUMNS:
        if _column_exists(conn, TABLE, name):
            continue
        added.append(name)
        if not dry_run:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {decl}")
    if not dry_run:
        conn.commit()
    for name in added:
        print(f"  Added column {TABLE}.{name}")
    if not added:
        print("  Columns already present.")
    return added


def backfill(conn, dry_run=False):
    if not _column_exists(conn, TABLE, 'settled_pnl'):
        print("  would copy frozen_pnl into settled_pnl (once the columns exist)")
        return

    rows = list(conn.execute(
        f"SELECT id, tradingsymbol, frozen_pnl, settled_pnl, settled_override "
        f"FROM {TABLE}"))
    if not rows:
        print("  No legs yet — nothing to backfill.")
        return

    # Only touch legs that haven't been migrated: a non-zero settled_pnl or a
    # set override means this already ran (or the app has been marking since).
    pending = [r for r in rows
               if (r[2] is not None) and not (r[3] or 0.0) and r[4] is None]
    print(f"  {len(rows)} leg(s); {len(pending)} with a frozen figure to move.")
    for leg_id, symbol, frozen, _, _ in pending:
        print(f"    {symbol}: frozen_pnl {frozen:,.2f} -> settled_pnl")
        if not dry_run:
            conn.execute(
                f"UPDATE {TABLE} SET settled_pnl = ?, cycle_open = 0, cycles = 1 "
                f"WHERE id = ?",
                (float(frozen), leg_id))

    # cycle_open is deliberately left alone. Marking every unfrozen leg as
    # "running" would invent a completed cycle for legs whose position was
    # already gone, and they would then show a settled ₹0.00 — asserting a close
    # that never happened. The poller (and a page load) sets the flag the first
    # time it actually sees a leg open, which is the only honest source for it.
    print("  cycle_open left for the poller to set from the live book.")
    if not dry_run:
        conn.commit()


def report(conn):
    if not _column_exists(conn, TABLE, 'settled_pnl'):
        print("\n(Nothing to report yet — run without --dry-run to apply.)")
        return
    rows = list(conn.execute(
        f"SELECT g.name, l.tradingsymbol, l.quantity, l.settled_pnl, "
        f"l.settled_override, l.cycle_open FROM {TABLE} l "
        f"JOIN ztrade_groups g ON g.id = l.group_id ORDER BY g.name, l.tradingsymbol"))
    print(f"\nLegs ({len(rows)}):")
    for name, symbol, qty, settled, override, cycle in rows:
        shown = override if override is not None else (settled or 0.0)
        flag = " (edited)" if override is not None else ""
        state = "cycle running" if cycle else "no open cycle"
        print(f"  {name[:20]:<20} {symbol:<22} qty={qty:>6} "
              f"settled={shown:>10,.2f}{flag} · {state}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Settled-P&L migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        if not _table_exists(conn, TABLE):
            print(f"No `{TABLE}` table — start the app once first.")
            return
        print("Schema:")
        ensure_columns(conn, args.dry_run)
        print("\nBackfill:")
        backfill(conn, args.dry_run)
        report(conn)
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
