"""Offline tests for the integrity gate + restore-from-backup -- no API/network.

Corrupts a real SQLite file and asserts the preflight detects it and restores
the newest good backup. Run:  python -m scripts.test_integrity
"""
import sys, os, glob, shutil, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, backup


def _make_db(path, season=2026):
    conn = db.init_db(path)
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                     num_teams, status) VALUES(1,?,1,0,8,'regular')""", (season,))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def _corrupt(path):
    for sfx in ("-wal", "-shm"):        # drop sidecars so the bad header wins
        try:
            os.remove(path + sfx)
        except OSError:
            pass
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(b"\xde\xad\xbe\xef" * 64)


def test_ok_and_fresh():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        _make_db(dbp)
        assert db.integrity_ok(dbp) is True
        assert db.preflight(dbp) == "ok"
        assert db.preflight(os.path.join(tmp, "nope.db")) == "fresh"
    print("ok: integrity_ok/preflight report ok for a good DB, fresh for none")


def test_corrupt_detected_and_restored():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        _make_db(dbp, season=2026)
        backup.backup_db(db_path=dbp, backup_dir=bdir, retain_days=0, quiet=True)

        _corrupt(dbp)
        assert db.integrity_ok(dbp) is False, "corruption not detected"

        status = db.preflight(dbp, backup_dir=bdir)
        assert status.startswith("restored:"), status
        assert db.integrity_ok(dbp) is True, "DB still bad after restore"
        # Data recovered from the snapshot.
        conn = db.connect(dbp)
        assert conn.execute("SELECT season FROM league").fetchone()[0] == 2026
        conn.close()
        # The corrupt file is preserved aside, not deleted.
        assert glob.glob(dbp + ".corrupt-*"), "corrupt file not kept for forensics"
    print("ok: corrupt DB detected, restored from backup, data recovered, "
          "bad file kept aside")


def test_corrupt_no_backup_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "empty-backups")
        os.makedirs(bdir)
        _make_db(dbp)
        _corrupt(dbp)
        try:
            db.preflight(dbp, backup_dir=bdir)
        except RuntimeError:
            print("ok: corrupt DB with no backup refuses to run (raises)")
            return
        assert False, "preflight should have raised with no backup to restore"


def test_restore_skips_a_corrupt_backup():
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        os.makedirs(bdir)
        _make_db(dbp, season=2025)
        # An older GOOD snapshot and a newer CORRUPT one (dated filenames sort).
        good = os.path.join(bdir, "league-20200101-000000.db")
        shutil.copyfile(dbp, good)
        bad = os.path.join(bdir, "league-20990101-000000.db")
        with open(bad, "wb") as f:
            f.write(b"garbage not a database")

        assert backup.latest_good_backup(bdir) == good, "did not skip corrupt backup"
        _corrupt(dbp)
        status = db.preflight(dbp, backup_dir=bdir)
        assert status == f"restored:{good}", status
        conn = db.connect(dbp)
        assert conn.execute("SELECT season FROM league").fetchone()[0] == 2025
        conn.close()
    print("ok: restore skips a newer corrupt backup, uses the newest good one")


def main():
    test_ok_and_fresh()
    test_corrupt_detected_and_restored()
    test_corrupt_no_backup_refuses()
    test_restore_skips_a_corrupt_backup()
    print("\nALL OFFLINE INTEGRITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
