"""
Shared database connection helper.

Local dev (default): a plain sqlite3 connection to a file under data/ --
no extra dependency, no setup required, exactly how this has always
worked.

Cloud (optional, per store): each caller passes an env_prefix identifying
which store it is (e.g. "TURSO" for water quality, "TURSO_DISCHARGE" for
discharge history). If that prefix's _DATABASE_URL and _AUTH_TOKEN
environment variables are both set, connect() transparently returns a
hosted Turso database instead.

That Turso connection talks plain HTTPS (Turso's own Hrana-over-HTTP
`/v2/pipeline` endpoint via `requests`), not the native `libsql` client
library. That's a deliberate choice, not a style preference: the native
client wraps a Rust/Tokio async runtime that, in production, deadlocked
this app's whole gunicorn worker the moment it was used from a
long-running process (confirmed as a known, unresolved issue across
multiple libsql/Turso Python packages -- e.g. tursodatabase/libsql#1075
and tursodatabase/libsql-client-py#30, the latter reproducing the exact
same Flask-triggers-a-deadlock symptom against a local db). It worked
fine in short-lived manual scripts (open, query, exit) because those
never stick around long enough to hit the buggy teardown path -- but a
persistent Flask process does, every time. Plain HTTP requests have no
persistent native runtime to ever get into that state.

TursoHttpConnection mimics just enough of sqlite3.Connection's interface
for this codebase's needs (confirmed by grep: only .execute(sql, params),
.fetchall()/.fetchone() on the result, `with conn:`, and .close() are
ever used -- no .fetchone() row-factory dict access, no executemany,
no .lastrowid/.rowcount). Rows come back as plain tuples, matching
sqlite3's default row_factory.

Separate prefixes let each store live in its own Turso database rather
than all sharing one -- set only the prefixes you've actually created a
database for; anything without its pair of env vars set falls back to its
local SQLite file untouched.
"""
import base64
import os
import sqlite3

import requests


def _encode_value(v):
    """Python value -> Hrana's typed JSON value encoding."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):  # bool is an int subclass -- check first
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _decode_value(v):
    """Hrana's typed JSON value encoding -> plain Python value."""
    t = v.get("type")
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return v["value"]
    if t == "text":
        return v["value"]
    if t == "blob":
        return base64.b64decode(v["base64"])
    return None  # "null" and anything unrecognised


class _TursoCursor:
    """Minimal stand-in for a sqlite3 cursor: just .fetchall()/.fetchone()
    over rows already decoded into plain tuples."""

    def __init__(self, result):
        result = result or {}
        self._rows = [tuple(_decode_value(v) for v in row) for row in result.get("rows", [])]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TursoHttpConnection:
    """A sqlite3.Connection-alike backed by Turso's HTTP API. See this
    module's docstring for why this exists instead of the native libsql
    client.

    Each execute() outside a `with conn:` block is sent as its own
    immediate request (one pipeline call, statement + close). Inside
    `with conn:`, statements are queued and sent together as one pipeline
    call on clean exit -- one HTTP round-trip for a whole batch, matching
    sqlite3's implicit-transaction grouping closely enough for this
    codebase's purposes (every write here is an idempotent upsert, so
    true cross-statement atomicity was never load-bearing)."""

    def __init__(self, url, auth_token, timeout=30):
        base = url.replace("libsql://", "https://", 1).rstrip("/")
        self._pipeline_url = base + "/v2/pipeline"
        self._token = auth_token
        self._timeout = timeout
        self._session = requests.Session()
        self._batch = None  # None outside a `with` block; a list while inside one

    def _post(self, step_requests):
        resp = self._session.post(
            self._pipeline_url,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            json={"requests": step_requests},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = []
        for item in resp.json().get("results", []):
            if item.get("type") == "error":
                raise RuntimeError(f"Turso query failed: {item.get('error', {}).get('message')}")
            results.append(item.get("response"))
        return results

    def execute(self, sql, params=()):
        stmt = {"sql": sql, "args": [_encode_value(p) for p in params], "want_rows": True}
        if self._batch is not None:
            self._batch.append(stmt)
            return _TursoCursor(None)  # queued writes are never read back before commit
        results = self._post([{"type": "execute", "stmt": stmt}, {"type": "close"}])
        exec_response = results[0] if results else None
        return _TursoCursor((exec_response or {}).get("result"))

    def __enter__(self):
        self._batch = []
        return self

    def __exit__(self, exc_type, exc, tb):
        batch, self._batch = self._batch, None
        if exc_type is None and batch:
            step_requests = [{"type": "execute", "stmt": s} for s in batch]
            step_requests.append({"type": "close"})
            self._post(step_requests)
        return False  # never suppress exceptions

    def close(self):
        self._session.close()


def connect(local_path, env_prefix="TURSO"):
    """Return a database connection: a local SQLite file at local_path by
    default, or a Turso database over HTTP if {env_prefix}_DATABASE_URL
    and {env_prefix}_AUTH_TOKEN are both set in the environment.
    local_path is only used for the local fallback."""
    turso_url = os.environ.get(f"{env_prefix}_DATABASE_URL")
    turso_token = os.environ.get(f"{env_prefix}_AUTH_TOKEN")
    if turso_url and turso_token:
        return TursoHttpConnection(turso_url, turso_token)

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    return sqlite3.connect(local_path)
