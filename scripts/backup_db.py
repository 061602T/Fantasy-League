"""Back up the live league database (for cron, or on demand).

Safe to run while the league process is writing -- it uses SQLite's online
backup API, not a raw file copy. Produces one dated, self-contained .db snapshot
and prunes snapshots older than the retention window.

Usage:
    python -m scripts.backup_db [--db PATH] [--dir DIR] [--retain-days N]

Env (used when the flags are omitted): FFL_DB_PATH, FFL_BACKUP_DIR,
FFL_BACKUP_RETAIN_DAYS.

Example Pi crontab -- every 6 hours, 14-day retention (the defaults):
    0 */6 * * * cd /home/pi/Fantasy-League && \
      FFL_DB_PATH=/home/pi/ffl-data/league.db /usr/bin/python3 -m scripts.backup_db \
      >> /home/pi/ffl-data/backup.log 2>&1

Point FFL_BACKUP_DIR at storage that is NOT on the SD card (USB/network) so a
card failure doesn't take the database and its backups at the same time.
"""
import argparse
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="source DB (default: FFL_DB_PATH)")
    ap.add_argument("--dir", default=None, help="backup dir (default: FFL_BACKUP_DIR)")
    ap.add_argument("--retain-days", type=int, default=None,
                    help="prune backups older than N days (default: 14)")
    args = ap.parse_args()

    # A missing DB is "nothing to do", not a failure; a bad snapshot raises and
    # exits non-zero so cron surfaces it.
    backup.backup_db(db_path=args.db, backup_dir=args.dir,
                     retain_days=args.retain_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
