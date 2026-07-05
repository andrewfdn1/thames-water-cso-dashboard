"""
Shared database connection helper.

Local dev (default, and everything up to now): a plain sqlite3 connection
to a file under data/ -- no extra dependency, no setup required, exactly
how this has always worked.

Cloud (optional, per store): each caller passes an env_prefix identifying
which store it is (e.g. "TURSO" for water quality, "TURSO_DISCHARGE" for
discharge history). If that prefix's _DATABASE_URL and _AUTH_TOKEN
environment variables are both set, connect() transparently returns a
hosted Turso/libSQL database instead -- Turso is a SQLite-compatible fork
(libSQL) and its Python client mirrors the stdlib sqlite3 interface
closely enough that this codebase's plain `conn.execute(...)` /
`with conn:` / `?`-placeholder style keeps working unchanged (one
confirmed difference: libsql's cursor isn't directly iterable like
sqlite3's, so callers must use .fetchall()/.fetchone() explicitly).

Separate prefixes let each store live in its own Turso database rather
than all sharing one -- set only the prefixes you've actually created a
database for; anything without its pair of env vars set falls back to its
local SQLite file untouched.

The `libsql` package is only imported the first time it's actually
needed, so a local-only setup never needs it installed.
"""
import os
import sqlite3


def connect(local_path, env_prefix="TURSO"):
    """Return a database connection: a local SQLite file at local_path by
    default, or a Turso/libSQL database if {env_prefix}_DATABASE_URL and
    {env_prefix}_AUTH_TOKEN are both set in the environment. local_path is
    only used for the local fallback."""
    turso_url = os.environ.get(f"{env_prefix}_DATABASE_URL")
    turso_token = os.environ.get(f"{env_prefix}_AUTH_TOKEN")
    if turso_url and turso_token:
        import libsql
        return libsql.connect(turso_url, auth_token=turso_token)

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    return sqlite3.connect(local_path)
