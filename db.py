"""
Shared database connection helper.

Local dev (default, and everything up to now): a plain sqlite3 connection
to a file under data/ -- no extra dependency, no setup required, exactly
how this has always worked.

Cloud (optional): set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN as
environment variables and every caller of connect() transparently gets a
hosted Turso/libSQL database instead, with no other code changes -- Turso
is a SQLite-compatible fork (libSQL) and its Python client mirrors the
stdlib sqlite3 interface closely enough that the rest of this codebase's
plain `conn.execute(...)` / `with conn:` / `?`-placeholder style should
keep working unchanged.

The `libsql` package is only imported if those two environment variables
are actually set, so a local-only setup (like running this on a plain dev
machine) never needs it installed.

Not yet verified against a real Turso database -- there's no account/
credentials configured yet. The local sqlite3 path is what's actually
running today and is unaffected by any of this.
"""
import os
import sqlite3


def connect(local_path):
    """Return a database connection: a local SQLite file at local_path by
    default, or a Turso/libSQL database if TURSO_DATABASE_URL and
    TURSO_AUTH_TOKEN are both set in the environment. local_path is only
    used for the local fallback."""
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        import libsql
        return libsql.connect(turso_url, auth_token=turso_token)

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    return sqlite3.connect(local_path)
