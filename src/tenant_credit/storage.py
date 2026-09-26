"""多租户算力信用额度服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS credit_orgs (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('research','enterprise')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('tenant_admin','scheduler','finance','reviewer','auditor')),
    org_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_rules (
    rule_version INTEGER PRIMARY KEY AUTOINCREMENT,
    source_priority TEXT NOT NULL,
    review_ttl_hours INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES credit_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_lines (
    line_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES credit_orgs(org_id),
    source TEXT NOT NULL CHECK(source IN ('PREPAID','POSTPAID','GRANT')),
    resource_kind TEXT NOT NULL,
    total_amount TEXT NOT NULL,
    consumed_amount TEXT NOT NULL DEFAULT '0',
    frozen_amount TEXT NOT NULL DEFAULT '0',
    expired_amount TEXT NOT NULL DEFAULT '0',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','exhausted','expired','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES credit_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_from < valid_to)
);

CREATE INDEX IF NOT EXISTS idx_credit_lines_org
ON credit_lines(org_id, state, resource_kind);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES credit_orgs(org_id),
    resource_kind TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    estimated_amount TEXT NOT NULL,
    executed_amount TEXT NOT NULL DEFAULT '0',
    exempted_amount TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'confirmed'
        CHECK(state IN ('confirmed','completed','cancelled','failed')),
    rule_version INTEGER NOT NULL REFERENCES credit_rules(rule_version),
    exemption_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES credit_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(starts_at < ends_at)
);

CREATE INDEX IF NOT EXISTS idx_reservations_org
ON reservations(org_id, state, created_at);

CREATE TABLE IF NOT EXISTS reservation_slices (
    slice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    period TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    estimated_amount TEXT NOT NULL,
    executed_amount TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'frozen' CHECK(state IN ('frozen','closed')),
    UNIQUE(reservation_id, period)
);

CREATE INDEX IF NOT EXISTS idx_slices_period_state
ON reservation_slices(period, state);

CREATE TABLE IF NOT EXISTS credit_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL REFERENCES credit_orgs(org_id),
    line_id TEXT NOT NULL REFERENCES credit_lines(line_id),
    reservation_id TEXT REFERENCES reservations(reservation_id),
    period TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('freeze','consume','refund','expire')),
    amount TEXT NOT NULL,
    available_after TEXT NOT NULL,
    frozen_after TEXT NOT NULL,
    consumed_after TEXT NOT NULL,
    reason TEXT NOT NULL,
    rule_version INTEGER,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_org
ON credit_ledger(org_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_ledger_line
ON credit_ledger(line_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_ledger_reservation
ON credit_ledger(reservation_id, entry_id);

CREATE TABLE IF NOT EXISTS exemption_requests (
    request_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES credit_orgs(org_id),
    resource_kind TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    requested_amount TEXT NOT NULL,
    shortfall_amount TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected','expired','used')),
    expires_at TEXT NOT NULL,
    reservation_id TEXT REFERENCES reservations(reservation_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES credit_users(user_id),
    submitted_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_exemptions_queue
ON exemption_requests(state, expires_at);

CREATE TABLE IF NOT EXISTS billing_periods (
    org_id TEXT NOT NULL REFERENCES credit_orgs(org_id),
    period TEXT NOT NULL,
    total_frozen TEXT NOT NULL,
    total_consumed TEXT NOT NULL,
    total_refunded TEXT NOT NULL,
    total_expired TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    settled_by TEXT NOT NULL REFERENCES credit_users(user_id),
    settled_at TEXT NOT NULL,
    PRIMARY KEY(org_id, period)
);

CREATE TABLE IF NOT EXISTS credit_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS credit_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_audit_entity
ON credit_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
