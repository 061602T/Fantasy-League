"""Offline tests for the DB backup helper -- no API calls, temp files only.

Run:  python -m scripts.test_backup
"""
import sys, os, time, glob, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, backup


def test_backup_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        conn = db.init_db(dbp)          # full schema, WAL, at a real path
        conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                          num_teams, status) VALUES(1, 2026, 1, 0, 8, 'draft')""")
        conn.commit()

        dest = backup.backup_db(db_path=dbp, backup_dir=bdir, retain_days=14,
                                quiet=True)
        assert dest and os.path.exists(dest), "no backup file written"
        # The snapshot is a valid DB carrying our row.
        c = sqlite3.connect(dest)
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert c.execute("SELECT season FROM league").fetchone()[0] == 2026
        c.close()
        print("ok: backup_roundtrip (snapshot written, quick_check ok, data intact)")


def test_prune_respects_retention():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        db.init_db(dbp).close()
        os.makedirs(bdir, exist_ok=True)

        # A backup from 20 days ago should be pruned at retain_days=14.
        old = os.path.join(bdir, "league-20000101-000000.db")
        open(old, "w").close()
        old_time = time.time() - 20 * 86400
        os.utime(old, (old_time, old_time))

        backup.backup_db(db_path=dbp, backup_dir=bdir, retain_days=14, quiet=True)
        assert not os.path.exists(old), "old backup was not pruned"
        assert glob.glob(os.path.join(bdir, "league-*.db")), "fresh backup missing"
        print("ok: prune removes backups older than the retention window")


def test_prune_disabled_keeps_all():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        db.init_db(dbp).close()
        os.makedirs(bdir, exist_ok=True)
        old = os.path.join(bdir, "league-20000101-000000.db")
        open(old, "w").close()
        old_time = time.time() - 999 * 86400
        os.utime(old, (old_time, old_time))

        backup.backup_db(db_path=dbp, backup_dir=bdir, retain_days=0, quiet=True)
        assert os.path.exists(old), "retain_days=0 should keep everything"
        print("ok: retain_days=0 disables pruning")


def test_missing_db_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        res = backup.backup_db(db_path=os.path.join(tmp, "nope.db"),
                               backup_dir=os.path.join(tmp, "b"), quiet=True)
        assert res is None, "missing DB should return None, not raise"
        print("ok: missing DB returns None (nothing to back up)")


def main():
    test_backup_roundtrip()
    test_prune_respects_retention()
    test_prune_disabled_keeps_all()
    test_missing_db_returns_none()
    print("\nALL OFFLINE BACKUP TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
