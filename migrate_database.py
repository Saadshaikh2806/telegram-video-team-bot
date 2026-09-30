"""Copy the local SQLite database into an EMPTY Postgres database.

Stop the local bot first. Set DATABASE_URL in .env; credentials are never printed.
"""
import sqlite3

from video_bot.config import Config
from video_bot.store import Store


TABLES = ('settings', 'editors', 'jobs', 'events', 'processed_updates', 'outbox')


def main():
    c = Config.from_env()
    if not c.database_url:
        raise SystemExit('Set DATABASE_URL in .env to your external Postgres connection string first.')
    source = sqlite3.connect(f'file:{c.database}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = Store(c.database, c.database_url)
    try:
        with target.db:
            for table in TABLES:
                if target.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:
                    raise SystemExit('Destination is not empty. Migration stopped without changing existing records.')
            for table in TABLES:
                for row in source.execute(f'SELECT * FROM {table}'):
                    columns = ','.join(row.keys())
                    placeholders = ','.join('?' for _ in row)
                    target.db.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(row))
            for table in ('jobs', 'events', 'outbox'):
                target.db.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {table}")
        print('Migration complete. Editors, jobs, deadlines, and group connections have been preserved.')
    finally:
        source.close()
        target.db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Migration failed. Check database access and settings. Credentials have not been printed; the destination transaction was rolled back.') from None
