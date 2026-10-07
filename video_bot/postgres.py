"""Postgres connection exposing the small DB interface used by the bot."""
import psycopg


class Row:
    def __init__(self, columns, values):
        self.columns, self.values = columns, values

    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.columns.index(key)]

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return self.columns


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.columns = [column.name for column in cursor.description] if cursor.description else []

    def fetchone(self):
        values = self.cursor.fetchone()
        return Row(self.columns, values) if values is not None else None

    def fetchall(self):
        return [Row(self.columns, values) for values in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PostgresConnection:
    def __init__(self, url, sqlite_schema):
        self.connection = psycopg.connect(url, autocommit=True, connect_timeout=15)
        self.transactions = []
        if sqlite_schema is None:
            return
        schema = '\n'.join(line for line in sqlite_schema.splitlines() if not line.startswith('PRAGMA'))
        schema = schema.replace('INTEGER', 'BIGINT').replace('REAL', 'DOUBLE PRECISION')
        schema = schema.replace('BIGINT PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
        for table in ('events', 'outbox'):
            schema = schema.replace(f'CREATE TABLE IF NOT EXISTS {table} (\n id BIGINT PRIMARY KEY',
                                    f'CREATE TABLE IF NOT EXISTS {table} (\n id BIGSERIAL PRIMARY KEY')
        with self.connection.transaction():
            for statement in schema.split(';'):
                if statement.strip():
                    self.connection.execute(statement)
            self.connection.execute('CREATE TABLE IF NOT EXISTS worker_lease (id BIGINT PRIMARY KEY, owner TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL)')
            self.connection.execute('ALTER TABLE outbox ADD COLUMN IF NOT EXISTS priority BIGINT NOT NULL DEFAULT 10')
            self.connection.execute('CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state,priority,available_at,id)')

    def execute(self, sql, params=()):
        # Application SQL uses positional ? parameters, never user-supplied SQL.
        params = tuple(int(value) if isinstance(value, bool) else value for value in params)
        return Cursor(self.connection.execute(sql.replace('?', '%s'), params or None))

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, *args):
        return self.transactions.pop().__exit__(*args)

    def close(self):
        self.connection.close()
