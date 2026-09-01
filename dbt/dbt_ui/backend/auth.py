"""
Users, passwords, sessions and per-user dataset grants.

The store is SQLite via the standard library, at dbt_ui/.runtime/studio.db.
SQLite rather than a JSON file because this is the authorization source of
truth: it needs atomic writes, uniqueness on email, and referential integrity
between users, sessions and grants. SQLite rather than Postgres because the
whole point of this project is that it installs with no pip and no service to
run - sqlite3 ships with Python.

Security notes, so the choices here are auditable:

- Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user 16-byte random salt
  and a high iteration count. The plaintext is never written anywhere, and
  verification uses hmac.compare_digest to avoid leaking timing information.
- Only the SHA-256 *hash* of a session token is stored. A dump of the database
  therefore does not yield usable session tokens.
- The token itself is returned once, in an HttpOnly cookie, so page JavaScript
  cannot read it.

What this does and does not buy you: it authenticates and authorizes the people
using this UI. It does not change what the underlying Google credentials can
reach - BigQuery IAM still decides that, and a Manager here cannot grant
themselves access the service account does not already have.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config

DB_FILE = "studio.db"

# PBKDF2 cost. High enough to make offline cracking expensive, low enough that a
# login stays well under a tenth of a second on a laptop.
PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
TOKEN_BYTES = 32

# How long a session lasts, and how long it may sit idle. Sliding: every
# authenticated request pushes the idle deadline out.
SESSION_LIFETIME_SECONDS = 12 * 60 * 60
SESSION_IDLE_SECONDS = 4 * 60 * 60

COOKIE_NAME = "dbtstudio_session"

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_ANALYST = "analyst"
VALID_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_ANALYST)

# Seed accounts. Development/testing convenience only: they exist so the app is
# usable the first time it starts. They are inserted once, then the database is
# authoritative - changing a role here later has no effect on an existing row.
SEED_USERS: Tuple[Tuple[str, str, str], ...] = (
    ("admin@gmail.com", "admin123", ROLE_ADMIN),
    ("analyst@gmail.com", "analyst123", ROLE_ANALYST),
    ("manager@gmail.com", "manager123", ROLE_MANAGER),
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.Lock()
_initialised = False


class AuthError(Exception):
    """Authentication or authorization failure, carrying an HTTP status."""

    def __init__(self, message: str, status: int = 401, **extra: Any):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


# --------------------------------------------------------------------------
# connection handling
# --------------------------------------------------------------------------

def db_path():
    return config.ensure_runtime_dir() / DB_FILE


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL keeps a long-running read (the UI polling) from blocking a write.
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    return conn


def init(force: bool = False) -> None:
    """Create the schema and seed the default users. Idempotent."""
    global _initialised
    with _lock:
        if _initialised and not force:
            return

        conn = _connect()
        try:
            conn.executescript(
                """
                create table if not exists users (
                    id            integer primary key autoincrement,
                    email         text    not null unique collate nocase,
                    password_hash text    not null,
                    password_salt text    not null,
                    iterations    integer not null,
                    role          text    not null,
                    is_active     integer not null default 1,
                    created_at    real    not null,
                    updated_at    real    not null
                );

                create table if not exists sessions (
                    token_hash text    primary key,
                    user_id    integer not null references users(id) on delete cascade,
                    created_at real    not null,
                    expires_at real    not null,
                    last_seen  real    not null
                );

                create index if not exists idx_sessions_user on sessions(user_id);

                -- Per-user dataset grants. A row here means "this user may use
                -- this dataset". No rows for a user means "fall back to the
                -- project-wide allowlist", which keeps existing behaviour for
                -- anyone nobody has restricted.
                create table if not exists user_datasets (
                    user_id integer not null references users(id) on delete cascade,
                    dataset text    not null collate nocase,
                    primary key (user_id, dataset)
                );
                """
            )
            _seed(conn)
        finally:
            conn.close()

        _initialised = True


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the default accounts, but only ones that do not already exist."""
    existing = {
        str(row["email"]).lower()
        for row in conn.execute("select email from users")
    }
    now = time.time()
    for email, password, role in SEED_USERS:
        if email.lower() in existing:
            continue
        salt, digest = hash_password(password)
        conn.execute(
            "insert into users (email, password_hash, password_salt, iterations,"
            " role, is_active, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, 1, ?, ?)",
            (email, digest, salt, PBKDF2_ITERATIONS, role, now, now),
        )


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def hash_password(password: str,
                  salt_hex: Optional[str] = None,
                  iterations: int = PBKDF2_ITERATIONS) -> Tuple[str, str]:
    """Return (salt_hex, hash_hex). A new random salt unless one is supplied."""
    if salt_hex is None:
        salt_hex = secrets.token_hex(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return salt_hex, digest.hex()


def verify_password(password: str, salt_hex: str, expected_hex: str,
                    iterations: int) -> bool:
    _, actual = hash_password(password, salt_hex, iterations)
    # Constant time: a plain == would leak how much of the hash matched.
    return hmac.compare_digest(actual, expected_hex)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    """A user as the API exposes it. Never includes hash or salt."""
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_users() -> List[Dict[str, Any]]:
    init()
    conn = _connect()
    try:
        rows = conn.execute(
            "select * from users order by role, email collate nocase"
        ).fetchall()
        users = []
        for row in rows:
            user = _row_to_user(row)
            user["datasets"] = _datasets_for(conn, row["id"])
            users.append(user)
        return users
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    init()
    conn = _connect()
    try:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        if not row:
            return None
        user = _row_to_user(row)
        user["datasets"] = _datasets_for(conn, row["id"])
        return user
    finally:
        conn.close()


def create_user(email: str, password: str, role: str) -> Dict[str, Any]:
    init()
    email = str(email or "").strip()
    role = str(role or "").strip().lower()

    if not _EMAIL_RE.match(email):
        raise AuthError(f"'{email}' is not a valid email address.", status=400)
    if role not in VALID_ROLES:
        raise AuthError(f"Unknown role '{role}'.", status=400)
    if len(password or "") < 8:
        raise AuthError("The password must be at least 8 characters.", status=400)

    salt, digest = hash_password(password)
    now = time.time()
    conn = _connect()
    try:
        try:
            cursor = conn.execute(
                "insert into users (email, password_hash, password_salt,"
                " iterations, role, is_active, created_at, updated_at)"
                " values (?, ?, ?, ?, ?, 1, ?, ?)",
                (email, digest, salt, PBKDF2_ITERATIONS, role, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise AuthError(f"{email} already has an account.", status=409) from exc
        row = conn.execute(
            "select * from users where id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def set_role(user_id: int, role: str, *, acting_user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Change a user's role. Written straight to the database, so it is in force
    for the next request - including requests on that user's existing session.
    """
    init()
    role = str(role or "").strip().lower()
    if role not in VALID_ROLES:
        raise AuthError(f"Unknown role '{role}'.", status=400)

    conn = _connect()
    try:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        if not row:
            raise AuthError("No such user.", status=404)

        # Guard against locking everyone out of user management: the last
        # remaining Manager cannot demote themselves.
        if row["role"] == ROLE_MANAGER and role != ROLE_MANAGER:
            remaining = conn.execute(
                "select count(*) as n from users"
                " where role = ? and is_active = 1 and id != ?",
                (ROLE_MANAGER, user_id),
            ).fetchone()["n"]
            if remaining == 0:
                raise AuthError(
                    "This is the only Manager. Promote another user to Manager "
                    "first, or nobody will be able to manage roles.",
                    status=409,
                )

        conn.execute(
            "update users set role = ?, updated_at = ? where id = ?",
            (role, time.time(), user_id),
        )
        updated = conn.execute(
            "select * from users where id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(updated)
    finally:
        conn.close()


def set_active(user_id: int, is_active: bool) -> Dict[str, Any]:
    init()
    conn = _connect()
    try:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        if not row:
            raise AuthError("No such user.", status=404)
        if not is_active and row["role"] == ROLE_MANAGER:
            remaining = conn.execute(
                "select count(*) as n from users"
                " where role = ? and is_active = 1 and id != ?",
                (ROLE_MANAGER, user_id),
            ).fetchone()["n"]
            if remaining == 0:
                raise AuthError(
                    "This is the only active Manager and cannot be disabled.",
                    status=409,
                )
        conn.execute(
            "update users set is_active = ?, updated_at = ? where id = ?",
            (1 if is_active else 0, time.time(), user_id),
        )
        if not is_active:
            # Revoke live sessions immediately rather than letting a disabled
            # account keep working until its cookie expires.
            conn.execute("delete from sessions where user_id = ?", (user_id,))
        updated = conn.execute(
            "select * from users where id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(updated)
    finally:
        conn.close()


def set_password(user_id: int, password: str) -> None:
    init()
    if len(password or "") < 8:
        raise AuthError("The password must be at least 8 characters.", status=400)
    salt, digest = hash_password(password)
    conn = _connect()
    try:
        cursor = conn.execute(
            "update users set password_hash = ?, password_salt = ?,"
            " iterations = ?, updated_at = ? where id = ?",
            (digest, salt, PBKDF2_ITERATIONS, time.time(), user_id),
        )
        if cursor.rowcount == 0:
            raise AuthError("No such user.", status=404)
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    init()
    conn = _connect()
    try:
        row = conn.execute("select * from users where id = ?", (user_id,)).fetchone()
        if not row:
            raise AuthError("No such user.", status=404)
        if row["role"] == ROLE_MANAGER:
            remaining = conn.execute(
                "select count(*) as n from users"
                " where role = ? and is_active = 1 and id != ?",
                (ROLE_MANAGER, user_id),
            ).fetchone()["n"]
            if remaining == 0:
                raise AuthError(
                    "This is the only Manager and cannot be deleted.", status=409
                )
        conn.execute("delete from users where id = ?", (user_id,))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# per-user dataset grants
# --------------------------------------------------------------------------

def _datasets_for(conn: sqlite3.Connection, user_id: int) -> List[str]:
    return [
        str(row["dataset"])
        for row in conn.execute(
            "select dataset from user_datasets where user_id = ?"
            " order by dataset collate nocase",
            (user_id,),
        )
    ]


def set_user_datasets(user_id: int, datasets: List[str]) -> List[str]:
    """
    Replace a user's dataset grants.

    An empty list means "no per-user restriction", so the project-wide allowlist
    applies. That keeps the default behaviour for users nobody has scoped.
    """
    init()
    cleaned = list(dict.fromkeys(
        str(name).strip().lower() for name in datasets if str(name).strip()
    ))
    conn = _connect()
    try:
        if not conn.execute(
            "select 1 from users where id = ?", (user_id,)
        ).fetchone():
            raise AuthError("No such user.", status=404)
        conn.execute("delete from user_datasets where user_id = ?", (user_id,))
        conn.executemany(
            "insert into user_datasets (user_id, dataset) values (?, ?)",
            [(user_id, name) for name in cleaned],
        )
        conn.execute(
            "update users set updated_at = ? where id = ?", (time.time(), user_id)
        )
        return cleaned
    finally:
        conn.close()


def user_datasets(user_id: int) -> List[str]:
    init()
    conn = _connect()
    try:
        return _datasets_for(conn, user_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def login(email: str, password: str) -> Tuple[str, Dict[str, Any]]:
    """
    Verify credentials and open a session. Returns (token, user).

    The failure message is deliberately identical for "no such email" and "wrong
    password" so the endpoint cannot be used to enumerate accounts.
    """
    init()
    email = str(email or "").strip()
    conn = _connect()
    try:
        row = conn.execute(
            "select * from users where email = ? collate nocase", (email,)
        ).fetchone()

        if row is None or not verify_password(
            str(password or ""), row["password_salt"],
            row["password_hash"], int(row["iterations"]),
        ):
            raise AuthError("That email and password do not match.", status=401)

        if not bool(row["is_active"]):
            raise AuthError("This account has been disabled.", status=403)

        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        conn.execute(
            "insert into sessions (token_hash, user_id, created_at, expires_at,"
            " last_seen) values (?, ?, ?, ?, ?)",
            (_hash_token(token), row["id"], now,
             now + SESSION_LIFETIME_SECONDS, now),
        )
        _prune(conn)

        user = _row_to_user(row)
        user["datasets"] = _datasets_for(conn, row["id"])
        return token, user
    finally:
        conn.close()


def resolve_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    The user behind a session token, or None.

    Reads the role from the users table on every call rather than caching it in
    the session row. That is what makes a role change take effect immediately on
    an already-signed-in user, which the requirements ask for.
    """
    if not token:
        return None
    init()
    conn = _connect()
    try:
        row = conn.execute(
            "select s.token_hash, s.expires_at, s.last_seen, u.*"
            " from sessions s join users u on u.id = s.user_id"
            " where s.token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            return None

        now = time.time()
        if now > float(row["expires_at"]) or (now - float(row["last_seen"])) > SESSION_IDLE_SECONDS:
            conn.execute(
                "delete from sessions where token_hash = ?", (row["token_hash"],)
            )
            return None
        if not bool(row["is_active"]):
            conn.execute(
                "delete from sessions where token_hash = ?", (row["token_hash"],)
            )
            return None

        # Sliding idle window.
        conn.execute(
            "update sessions set last_seen = ? where token_hash = ?",
            (now, row["token_hash"]),
        )

        user = _row_to_user(row)
        user["datasets"] = _datasets_for(conn, row["id"])
        return user
    finally:
        conn.close()


def logout(token: Optional[str]) -> None:
    if not token:
        return
    init()
    conn = _connect()
    try:
        conn.execute(
            "delete from sessions where token_hash = ?", (_hash_token(token),)
        )
    finally:
        conn.close()


def _prune(conn: sqlite3.Connection) -> None:
    """Drop expired sessions. Cheap, and keeps the table from growing forever."""
    now = time.time()
    conn.execute(
        "delete from sessions where expires_at < ? or last_seen < ?",
        (now, now - SESSION_IDLE_SECONDS),
    )


def stats() -> Dict[str, Any]:
    init()
    conn = _connect()
    try:
        users = conn.execute("select count(*) as n from users").fetchone()["n"]
        by_role = {
            str(row["role"]): row["n"]
            for row in conn.execute(
                "select role, count(*) as n from users group by role"
            )
        }
        sessions = conn.execute("select count(*) as n from sessions").fetchone()["n"]
        return {
            "users": users,
            "by_role": by_role,
            "active_sessions": sessions,
            "database": str(db_path()),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# cookies
# --------------------------------------------------------------------------

def parse_cookies(header: Optional[str]) -> Dict[str, str]:
    """Minimal cookie header parser. Avoids pulling in http.cookies for this."""
    out: Dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        out[name.strip()] = value.strip()
    return out


def session_cookie(token: str) -> str:
    """
    The Set-Cookie value for a new session.

    HttpOnly so page scripts cannot read the token, SameSite=Strict so another
    site cannot ride the session, Path=/ so it covers the API and the app. No
    Secure flag: this is served over plain HTTP on loopback, and setting Secure
    would stop the cookie being stored at all.
    """
    return (
        f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/;"
        f" Max-Age={SESSION_LIFETIME_SECONDS}"
    )


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
