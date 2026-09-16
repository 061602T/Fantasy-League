"""Safe, self-contained backups of the runtime SQLite database.

Uses SQLite's online backup API (``sqlite3.Connection.backup``), which produces
a single consistent ``.db`` snapshot even while the league process is writing --
so there is no need to copy the ``-wal``/``-shm`` sidecars, and no dependency on
the ``sqlite3`` CLI (which isn't installed on this host or a stock Pi).

Backups are opaque runtime files under ``FFL_BACKUP_DIR`` (default
``~/ffl-data/backups``). They are NOT a league export/import mechanism: a backup
only ever restores the *same* database byte-for-byte, so there is no path by
which test data could be imported into a real league.

Env vars: ``FFL_DB_PATH`` (source, via config), ``FFL_BACKUP_DIR`` (destination),
``FFL_BACKUP_RETAIN_DAYS`` (prune age, default 14).
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import time

from . import config, db

DEFAULT_BACKUP_DIR = os.path.join("~", "ffl-data", "backups")


def _backup_dir() -> str:
    return os.path.expanduser(
        os.environ.get("FFL_BACKUP_DIR", DEFAULT_BACKUP_DIR))


def _retain_days() -> int:
    return int(os.environ.get("FFL_BACKUP_RETAIN_DAYS", "14"))


def backup_db(db_path: str = None, backup_dir: str = None,
              retain_days: int = None, *, quiet: bool = False):
    """Write a verified, dated snapshot of the DB, then prune old ones.

    Returns the backup file path, or None if the source DB doesn't exist yet.
    Raises RuntimeError if the written snapshot fails its integrity check.
    """
    db_path = db_path or config.DB_PATH
    backup_dir = os.path.expanduser(backup_dir) if backup_dir else _backup_dir()
    retain_days = _retain_days() if retain_days is None else retain_days

    if not os.path.exists(db_path):
        if not quiet:
            print(f"No database at {db_path}; nothing to back up.")
        return None

    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, time.strftime("league-%Y%m%d-%H%M%S.db"))

    src = sqlite3.connect(db_path)
    try:
        bk = sqlite3.connect(dest)
        try:
            with bk:                       # online backup: write-safe, folds WAL in
                src.backup(bk)
        finally:
            bk.close()
    finally:
        src.close()

    # Verify the snapshot before we trust it (and before pruning against it).
    check = sqlite3.connect(dest)
    try:
        status = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if status != "ok":
        raise RuntimeError(f"Backup {dest} failed quick_check: {status!r}")

    pruned = _prune(backup_dir, retain_days)
    if not quiet:
        print(f"Backed up {db_path} -> {dest} (quick_check ok; pruned {pruned}).")
    return dest


def latest_good_backup(backup_dir: str = None):
    """Newest snapshot that itself passes an integrity check, or None.

    Dated filenames sort chronologically, so we check newest-first and skip any
    backup that is itself corrupt.
    """
    backup_dir = os.path.expanduser(backup_dir) if backup_dir else _backup_dir()
    for f in sorted(glob.glob(os.path.join(backup_dir, "league-*.db")), reverse=True):
        if db.integrity_ok(f):
            return f
    return None


def restore_latest_backup(db_path: str = None, backup_dir: str = None):
    """Replace a corrupt DB with the newest good backup. Returns the backup used,
    or None if there is no valid backup to restore from.

    The corrupt file is moved aside (``.corrupt-<timestamp>``) for forensics
    rather than deleted, and the live DB's stale ``-wal``/``-shm`` sidecars are
    removed (the restored snapshot is self-contained).
    """
    db_path = db_path or config.DB_PATH
    good = latest_good_backup(backup_dir)
    if not good:
        return None
    if os.path.exists(db_path):
        aside = f"{db_path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(db_path, aside)
    shutil.copyfile(good, db_path)
    for sfx in ("-wal", "-shm"):
        try:
            os.remove(db_path + sfx)
        except OSError:
            pass
    return good


def _prune(backup_dir: str, retain_days: int) -> int:
    """Delete backups older than retain_days. retain_days<=0 keeps everything."""
    if retain_days <= 0:
        return 0
    cutoff = time.time() - retain_days * 86400
    removed = 0
    for f in glob.glob(os.path.join(backup_dir, "league-*.db")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    return removed
