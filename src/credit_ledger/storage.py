"""信用额度服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS credit_tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('tenant','finance','reviewer','auditor')),
    tenant_id TEXT REFERENCES credit_tenants(tenant_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 可预约的资源池：确认时与额度冻结在同一事务内扣减可用工时。
CREATE TABLE IF NOT EXISTS resource_pools (
    pool_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    product TEXT NOT NULL,
    available_hours TEXT NOT NULL,
    unit_price_cny TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pools_resource
ON resource_pools(facility_id, product);

-- 额度来源：预付、后付、专项赠送分别建账，记录适用资源与有效期。
CREATE TABLE IF NOT EXISTS credit_accounts (
    credit_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES credit_tenants(tenant_id),
    source_type TEXT NOT NULL CHECK(source_type IN ('prepaid','postpaid','grant')),
    amount_total TEXT NOT NULL,
    frozen_amount TEXT NOT NULL DEFAULT '0',
    consumed_amount TEXT NOT NULL DEFAULT '0',
    products_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_tenant
ON credit_accounts(tenant_id, source_type, state);

-- 版本化选择规则；每次落账记录所用 revision，规则更新不回写历史账期。
CREATE TABLE IF NOT EXISTS credit_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT REFERENCES credit_tenants(tenant_id),
    revision INTEGER NOT NULL,
    source_priority_json TEXT NOT NULL,
    allow_overage INTEGER NOT NULL DEFAULT 0 CHECK(allow_overage IN (0,1)),
    effective_from TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, revision)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES credit_tenants(tenant_id),
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    product TEXT NOT NULL,
    requested_hours TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    est_total TEXT NOT NULL,
    covered_amount TEXT NOT NULL DEFAULT '0',
    overage_amount TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL CHECK(state IN (
        'pending_review','rejected','confirmed','running','completed','cancelled','failed')),
    review_id INTEGER,
    rule_revision INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_reservations_tenant_time
ON reservations(tenant_id, starts_at, state);

-- 跨月作业按月切分的预计消耗段。
CREATE TABLE IF NOT EXISTS reservation_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    period_key TEXT NOT NULL,
    seg_index INTEGER NOT NULL,
    seg_start TEXT NOT NULL,
    seg_end TEXT NOT NULL,
    hours TEXT NOT NULL,
    amount TEXT NOT NULL,
    frozen_amount TEXT NOT NULL DEFAULT '0',
    consumed_amount TEXT NOT NULL DEFAULT '0',
    released_amount TEXT NOT NULL DEFAULT '0',
    overage_amount TEXT NOT NULL DEFAULT '0',
    UNIQUE(reservation_id, seg_index)
);

CREATE INDEX IF NOT EXISTS idx_segments_period
ON reservation_segments(reservation_id, period_key);

-- 某段在各额度来源上的冻结明细，用于按相同顺序结算与返还。
CREATE TABLE IF NOT EXISTS segment_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES reservation_segments(segment_id),
    credit_id TEXT NOT NULL REFERENCES credit_accounts(credit_id),
    source_type TEXT NOT NULL,
    amount TEXT NOT NULL,
    frozen_amount TEXT NOT NULL,
    consumed_amount TEXT NOT NULL DEFAULT '0',
    released_amount TEXT NOT NULL DEFAULT '0',
    priority_rank INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_holds_segment
ON segment_holds(segment_id, priority_rank, hold_id);

-- 仅追加流水：冻结、消耗、返还、超额、调整。
CREATE TABLE IF NOT EXISTS credit_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES credit_tenants(tenant_id),
    reservation_id TEXT,
    segment_id INTEGER,
    credit_id TEXT,
    entry_type TEXT NOT NULL CHECK(entry_type IN (
        'grant','freeze','consume','release','overage','adjust')),
    source_type TEXT,
    amount TEXT NOT NULL,
    period_key TEXT,
    settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
    rule_revision INTEGER,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_tenant_time
ON credit_ledger(tenant_id, ledger_id);

CREATE INDEX IF NOT EXISTS idx_ledger_reservation
ON credit_ledger(reservation_id, ledger_id);

-- 有期限的超额豁免复核队列。
CREATE TABLE IF NOT EXISTS overage_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES credit_tenants(tenant_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    requested_amount TEXT NOT NULL,
    covered_amount TEXT NOT NULL,
    shortfall_amount TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected','expired')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_pending
ON overage_reviews(state, expires_at);

-- 已结算账期：关闭后拒绝任何回写。
CREATE TABLE IF NOT EXISTS accounting_periods (
    tenant_id TEXT NOT NULL REFERENCES credit_tenants(tenant_id),
    period_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    closed_by TEXT,
    closed_at TEXT,
    PRIMARY KEY(tenant_id, period_key)
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
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
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
