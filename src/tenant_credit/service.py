"""多租户算力信用额度、预约冻结、超额复核与账期结算的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InsufficientCredit, InvalidState, NotFound, ValidationFailed
from .ledger import (
    ZERO,
    LineCandidate,
    attribute_executed,
    canonical_json,
    decimal_text,
    digest,
    period_bounds,
    period_of,
    quantize_amount,
    select_lines,
    split_by_month,
)
from .models import (
    ORG_KINDS,
    CreditLineInput,
    ExemptionInput,
    ReservationInput,
    RuleInput,
    decimal_value,
    identifier,
    period_text,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "tenant_admin": {"reservation.write", "exemption.write", "org.read"},
    "scheduler": {"reservation.close"},
    "finance": {"credit.write", "rule.write", "period.settle", "line.expire", "report.read"},
    "reviewer": {"exemption.write", "exemption.decide"},
    "auditor": {"audit.read", "report.read"},
}

OUTCOME_STATES = {"complete": "completed", "cancel": "cancelled", "fail": "failed"}
OUTCOME_REASONS = {
    "complete": "作业完成，按实际执行结算",
    "cancel": "预约取消，仅结算已执行部分",
    "fail": "作业失败，仅结算已执行部分",
}


class CreditService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _require_org_member(self, user: sqlite3.Row, org_id: str) -> None:
        if user["role"] == "tenant_admin" and user["org_id"] != org_id:
            raise Forbidden("租户只能操作本机构数据")

    def _org_scope(self, user: sqlite3.Row, org_id: str) -> None:
        if user["role"] == "tenant_admin":
            if user["org_id"] != org_id:
                raise Forbidden("租户只能查看本机构数据")
            return
        if "report.read" not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权查看机构数据")

    def _org(self, org_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_orgs WHERE org_id=?", (org_id,)
        ).fetchone()
        if row is None:
            raise NotFound("机构不存在")
        return row

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM credit_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO credit_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM credit_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != digest(raw):
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _store_idempotency(
        self, scope: str, key: str, raw: Mapping[str, Any], response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO credit_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(raw), canonical_json(response), self._now()),
        )

    def _current_rule(self) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_rules ORDER BY rule_version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise InvalidState("尚未发布额度规则")
        return row

    def _ensure_period_open(self, org_id: str, period: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM billing_periods WHERE org_id=? AND period=?", (org_id, period)
        ).fetchone()
        if row is not None:
            raise InvalidState(f"账期 {period} 已结算，禁止回写")

    @staticmethod
    def _available(line: sqlite3.Row) -> Decimal:
        return (
            Decimal(line["total_amount"])
            - Decimal(line["consumed_amount"])
            - Decimal(line["frozen_amount"])
            - Decimal(line["expired_amount"])
        )

    def create_org(self, org_id: str, name: str, kind: str) -> dict[str, Any]:
        org_id = identifier(org_id, "org_id")
        name = required_text(name, "name")
        if kind not in ORG_KINDS:
            raise ValidationFailed("机构类型必须是 research 或 enterprise")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_orgs(org_id,name,kind,created_at) VALUES(?,?,?,?)",
                    (org_id, name, kind, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("机构已经存在") from exc
        return {"org_id": org_id, "kind": kind}

    def create_user(
        self, user_id: str, display_name: str, role: str, org_id: str = "platform"
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        org_id = org_id.strip() or "platform"
        if role == "tenant_admin":
            self._org(org_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_users(user_id,display_name,role,org_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, org_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "org_id": org_id}

    def publish_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = RuleInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO credit_rules(source_priority,review_ttl_hours,note,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (canonical_json(list(rule.source_priority)), rule.review_ttl_hours, rule.note, actor_id, self._now()),
            )
            version = int(cursor.lastrowid)
            self._audit(
                "rule",
                str(version),
                "rule.published",
                actor_id,
                {"source_priority": list(rule.source_priority), "review_ttl_hours": rule.review_ttl_hours},
            )
        return {"rule_version": version, "source_priority": list(rule.source_priority)}

    def open_credit_line(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "credit.write")
        line = CreditLineInput.from_dict(raw)
        stored = self._idempotent("credit_line", line.idempotency_key, raw)
        if stored is not None:
            return stored
        self._org(line.org_id)
        response = {
            "line_id": line.line_id,
            "org_id": line.org_id,
            "source": line.source,
            "resource_kind": line.resource_kind,
            "total_amount": decimal_text(quantize_amount(line.total_amount)),
            "available_amount": decimal_text(quantize_amount(line.total_amount)),
            "state": "active",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_lines(line_id,org_id,source,resource_kind,total_amount,valid_from,valid_to,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        line.line_id,
                        line.org_id,
                        line.source,
                        line.resource_kind,
                        decimal_text(quantize_amount(line.total_amount)),
                        line.valid_from,
                        line.valid_to,
                        line.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotency("credit_line", line.idempotency_key, raw, response)
                self._audit(
                    "credit_line",
                    line.line_id,
                    "credit_line.opened",
                    actor_id,
                    {
                        "org_id": line.org_id,
                        "source": line.source,
                        "resource_kind": line.resource_kind,
                        "total_amount": decimal_text(quantize_amount(line.total_amount)),
                        "valid_from": line.valid_from,
                        "valid_to": line.valid_to,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("额度行编号或幂等键冲突") from exc
        return response

    def _line_row(self, line_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_lines WHERE line_id=?", (line_id,)
        ).fetchone()
        if row is None:
            raise NotFound("额度行不存在")
        return row

    def credit_line(self, actor_id: str, line_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self._line_row(line_id)
        self._org_scope(user, row["org_id"])
        return {**dict(row), "available_amount": decimal_text(self._available(row))}

    def _plan_freezes(
        self,
        org_id: str,
        resource_kind: str,
        slices: Sequence,
        source_priority: Sequence[str],
    ) -> tuple[list[tuple[Any, list[tuple[str, Decimal]]]], dict[str, Decimal]]:
        """为每个切片选择额度行；返回 (逐片扣减计划, 逐账期缺口)。"""
        rows = self.connection.execute(
            "SELECT * FROM credit_lines WHERE org_id=? AND state='active' AND (resource_kind=? OR resource_kind='ANY')",
            (org_id, resource_kind),
        ).fetchall()
        planned: dict[str, Decimal] = {}
        allocations: list[tuple[Any, list[tuple[str, Decimal]]]] = []
        shortfall: dict[str, Decimal] = {}
        for slice_plan in slices:
            candidates = []
            for row in rows:
                if parse_utc(row["valid_from"]) <= slice_plan.starts_at and parse_utc(row["valid_to"]) >= slice_plan.ends_at:
                    available = self._available(row) - planned.get(row["line_id"], ZERO)
                    if available > ZERO:
                        candidates.append(
                            LineCandidate(row["line_id"], row["source"], available, parse_utc(row["valid_to"]))
                        )
            plan, missing = select_lines(candidates, slice_plan.amount, source_priority)
            for line_id, amount in plan:
                planned[line_id] = planned.get(line_id, ZERO) + amount
            allocations.append((slice_plan, plan))
            if missing > ZERO:
                shortfall[slice_plan.period] = missing
        return allocations, shortfall

    def _write_ledger(
        self,
        *,
        org_id: str,
        line_id: str,
        reservation_id: str | None,
        period: str,
        entry_type: str,
        amount: Decimal,
        reason: str,
        rule_version: int | None,
        actor_id: str,
    ) -> None:
        if amount <= ZERO:
            raise InvalidState("流水金额必须大于零")
        self._ensure_period_open(org_id, period)
        line = self._line_row(line_id)
        consumed = Decimal(line["consumed_amount"])
        frozen = Decimal(line["frozen_amount"])
        expired = Decimal(line["expired_amount"])
        if entry_type == "freeze":
            if self._available(line) < amount:
                raise InvalidState("额度行可用余额不足")
            frozen += amount
        elif entry_type == "consume":
            consumed += amount
            frozen -= amount
        elif entry_type == "refund":
            frozen -= amount
        elif entry_type == "expire":
            expired += amount
        else:
            raise InvalidState("未知流水类型")
        available = Decimal(line["total_amount"]) - consumed - frozen - expired
        if available < ZERO or frozen < ZERO or consumed < ZERO or expired < ZERO:
            raise InvalidState("额度行余额异常")
        if entry_type == "expire":
            state = "expired"
        elif frozen == ZERO and consumed + expired >= Decimal(line["total_amount"]):
            state = "exhausted"
        else:
            state = line["state"]
        cursor = self.connection.execute(
            "UPDATE credit_lines SET consumed_amount=?,frozen_amount=?,expired_amount=?,state=?,"
            "revision=revision+1 WHERE line_id=? AND revision=?",
            (
                decimal_text(consumed),
                decimal_text(frozen),
                decimal_text(expired),
                state,
                line_id,
                line["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise Conflict("额度行已被并发修改")
        self.connection.execute(
            "INSERT INTO credit_ledger(org_id,line_id,reservation_id,period,entry_type,amount,available_after,"
            "frozen_after,consumed_after,reason,rule_version,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                org_id,
                line_id,
                reservation_id,
                period,
                entry_type,
                decimal_text(amount),
                decimal_text(available),
                decimal_text(frozen),
                decimal_text(consumed),
                reason,
                rule_version,
                actor_id,
                self._now(),
            ),
        )

    def confirm_reservation(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "reservation.write")
        reservation = ReservationInput.from_dict(raw)
        self._require_org_member(user, reservation.org_id)
        stored = self._idempotent("reservation", reservation.idempotency_key, raw)
        if stored is not None:
            return stored
        self._org(reservation.org_id)
        now = self.clock.now()
        if parse_utc(reservation.starts_at) < now:
            raise ValidationFailed("starts_at 不能早于当前时间")
        rule = self._current_rule()
        priority = json.loads(rule["source_priority"])
        slices = split_by_month(
            parse_utc(reservation.starts_at), parse_utc(reservation.ends_at), reservation.estimated_amount
        )
        allocations, shortfall = self._plan_freezes(
            reservation.org_id, reservation.resource_kind, slices, priority
        )
        exempted = ZERO
        exemption: sqlite3.Row | None = None
        if shortfall:
            exempted = sum(shortfall.values(), ZERO)
            detail = "、".join(f"{period} 缺 {decimal_text(amount)}" for period, amount in sorted(shortfall.items()))
            if reservation.exemption_id is None:
                raise InsufficientCredit(f"额度不足：{detail}；可提交豁免复核")
            exemption = self.connection.execute(
                "SELECT * FROM exemption_requests WHERE request_id=?", (reservation.exemption_id,)
            ).fetchone()
            if exemption is None:
                raise NotFound("豁免复核不存在")
            if exemption["org_id"] != reservation.org_id:
                raise Forbidden("豁免与预约不属于同一机构")
            if exemption["state"] == "pending":
                raise InvalidState("豁免复核尚未批准")
            if exemption["state"] in ("rejected", "expired"):
                raise InvalidState("豁免复核未通过或已过期")
            if exemption["state"] == "used":
                raise InvalidState("豁免已被其他预约使用")
            if (
                exemption["resource_kind"] != reservation.resource_kind
                or exemption["starts_at"] != reservation.starts_at
                or exemption["ends_at"] != reservation.ends_at
                or Decimal(exemption["requested_amount"]) != reservation.estimated_amount
            ):
                raise ValidationFailed("豁免与预约内容不匹配")
            if exempted > Decimal(exemption["shortfall_amount"]):
                raise InsufficientCredit("缺口超过已批准的豁免额度")
        response = {
            "reservation_id": reservation.reservation_id,
            "org_id": reservation.org_id,
            "state": "confirmed",
            "rule_version": rule["rule_version"],
            "slices": [
                {
                    "period": item.period,
                    "starts_at": utc_text(item.starts_at),
                    "ends_at": utc_text(item.ends_at),
                    "estimated_amount": decimal_text(item.amount),
                }
                for item in slices
            ],
            "allocations": [
                {"line_id": line_id, "period": item.period, "amount": decimal_text(amount)}
                for item, plan in allocations
                for line_id, amount in plan
            ],
            "exempted_amount": decimal_text(quantize_amount(exempted)),
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,org_id,resource_kind,starts_at,ends_at,"
                    "estimated_amount,exempted_amount,rule_version,exemption_id,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation.reservation_id,
                        reservation.org_id,
                        reservation.resource_kind,
                        reservation.starts_at,
                        reservation.ends_at,
                        decimal_text(quantize_amount(reservation.estimated_amount)),
                        decimal_text(quantize_amount(exempted)),
                        rule["rule_version"],
                        reservation.exemption_id if exemption is not None else None,
                        reservation.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                for item in slices:
                    self.connection.execute(
                        "INSERT INTO reservation_slices(reservation_id,period,starts_at,ends_at,estimated_amount) "
                        "VALUES(?,?,?,?,?)",
                        (
                            reservation.reservation_id,
                            item.period,
                            utc_text(item.starts_at),
                            utc_text(item.ends_at),
                            decimal_text(item.amount),
                        ),
                    )
                for item, plan in allocations:
                    for line_id, amount in plan:
                        self._write_ledger(
                            org_id=reservation.org_id,
                            line_id=line_id,
                            reservation_id=reservation.reservation_id,
                            period=item.period,
                            entry_type="freeze",
                            amount=amount,
                            reason=f"预约 {reservation.reservation_id} 确认冻结",
                            rule_version=rule["rule_version"],
                            actor_id=actor_id,
                        )
                if exemption is not None:
                    cursor = self.connection.execute(
                        "UPDATE exemption_requests SET state='used',reservation_id=? "
                        "WHERE request_id=? AND state='approved'",
                        (reservation.reservation_id, exemption["request_id"]),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("豁免已被并发使用")
                self._store_idempotency("reservation", reservation.idempotency_key, raw, response)
                self._audit(
                    "reservation",
                    reservation.reservation_id,
                    "reservation.confirmed",
                    actor_id,
                    {
                        "org_id": reservation.org_id,
                        "resource_kind": reservation.resource_kind,
                        "estimated_amount": decimal_text(quantize_amount(reservation.estimated_amount)),
                        "exempted_amount": decimal_text(quantize_amount(exempted)),
                        "rule_version": rule["rule_version"],
                        "slices": response["slices"],
                        "allocations": response["allocations"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("预约编号或幂等键冲突") from exc
        return response

    def reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        self._org_scope(user, row["org_id"])
        return dict(row)

    def _close_reservation(
        self, actor_id: str, reservation_id: str, outcome: str, executed_raw: object
    ) -> dict[str, Any]:
        self._require(actor_id, "reservation.close")
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        if row["state"] != "confirmed":
            raise InvalidState("预约已关闭")
        estimated = Decimal(row["estimated_amount"])
        executed = quantize_amount(decimal_value(executed_raw, "executed_amount", minimum=ZERO))
        if executed > estimated:
            raise ValidationFailed("已执行消耗不能超过预计总量")
        slices = self.connection.execute(
            "SELECT * FROM reservation_slices WHERE reservation_id=? ORDER BY starts_at,slice_id",
            (reservation_id,),
        ).fetchall()
        per_slice = attribute_executed([Decimal(item["estimated_amount"]) for item in slices], executed)
        rule = self.connection.execute(
            "SELECT * FROM credit_rules WHERE rule_version=?", (row["rule_version"],)
        ).fetchone()
        priority = json.loads(rule["source_priority"])
        rank = {source: index for index, source in enumerate(priority)}
        reason = OUTCOME_REASONS[outcome]
        state = OUTCOME_STATES[outcome]
        entries: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for slice_row, slice_executed in zip(slices, per_slice):
                freeze_rows = self.connection.execute(
                    "SELECT line_id,amount FROM credit_ledger WHERE reservation_id=? AND period=? "
                    "AND entry_type='freeze' ORDER BY entry_id",
                    (reservation_id, slice_row["period"]),
                ).fetchall()
                frozen_by_line: dict[str, Decimal] = {}
                for freeze in freeze_rows:
                    frozen_by_line[freeze["line_id"]] = frozen_by_line.get(freeze["line_id"], ZERO) + Decimal(
                        freeze["amount"]
                    )
                line_rows = {line_id: self._line_row(line_id) for line_id in frozen_by_line}
                ordered = sorted(
                    frozen_by_line,
                    key=lambda line_id: (
                        parse_utc(line_rows[line_id]["valid_to"]),
                        rank[line_rows[line_id]["source"]],
                        line_id,
                    ),
                )
                remaining = slice_executed
                for line_id in ordered:
                    consume = min(frozen_by_line[line_id], remaining)
                    refund = frozen_by_line[line_id] - consume
                    remaining -= consume
                    if consume > ZERO:
                        self._write_ledger(
                            org_id=row["org_id"],
                            line_id=line_id,
                            reservation_id=reservation_id,
                            period=slice_row["period"],
                            entry_type="consume",
                            amount=consume,
                            reason=reason,
                            rule_version=row["rule_version"],
                            actor_id=actor_id,
                        )
                        entries.append(
                            {"line_id": line_id, "period": slice_row["period"], "entry_type": "consume", "amount": decimal_text(consume)}
                        )
                    if refund > ZERO:
                        self._write_ledger(
                            org_id=row["org_id"],
                            line_id=line_id,
                            reservation_id=reservation_id,
                            period=slice_row["period"],
                            entry_type="refund",
                            amount=refund,
                            reason="未执行部分返还",
                            rule_version=row["rule_version"],
                            actor_id=actor_id,
                        )
                        entries.append(
                            {"line_id": line_id, "period": slice_row["period"], "entry_type": "refund", "amount": decimal_text(refund)}
                        )
                self.connection.execute(
                    "UPDATE reservation_slices SET executed_amount=?,state='closed' WHERE slice_id=?",
                    (decimal_text(slice_executed), slice_row["slice_id"]),
                )
            cursor = self.connection.execute(
                "UPDATE reservations SET state=?,executed_amount=?,closed_at=? "
                "WHERE reservation_id=? AND state='confirmed'",
                (state, decimal_text(executed), self._now(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("预约已被并发关闭")
            self._audit(
                "reservation",
                reservation_id,
                "reservation.closed",
                actor_id,
                {
                    "outcome": state,
                    "executed_amount": decimal_text(executed),
                    "refunded_amount": decimal_text(estimated - executed),
                    "entries": entries,
                },
            )
        return {
            "reservation_id": reservation_id,
            "state": state,
            "executed_amount": decimal_text(executed),
            "refunded_amount": decimal_text(estimated - executed),
            "entries": entries,
        }

    def complete_reservation(
        self, actor_id: str, reservation_id: str, executed_amount: object
    ) -> dict[str, Any]:
        return self._close_reservation(actor_id, reservation_id, "complete", executed_amount)

    def cancel_reservation(
        self, actor_id: str, reservation_id: str, executed_amount: object = 0
    ) -> dict[str, Any]:
        return self._close_reservation(actor_id, reservation_id, "cancel", executed_amount)

    def fail_reservation(
        self, actor_id: str, reservation_id: str, executed_amount: object = 0
    ) -> dict[str, Any]:
        return self._close_reservation(actor_id, reservation_id, "fail", executed_amount)

    def request_exemption(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "exemption.write")
        exemption = ExemptionInput.from_dict(raw)
        self._require_org_member(user, exemption.org_id)
        stored = self._idempotent("exemption", exemption.idempotency_key, raw)
        if stored is not None:
            return stored
        self._org(exemption.org_id)
        now = self.clock.now()
        if parse_utc(exemption.starts_at) < now:
            raise ValidationFailed("starts_at 不能早于当前时间")
        rule = self._current_rule()
        priority = json.loads(rule["source_priority"])
        slices = split_by_month(
            parse_utc(exemption.starts_at), parse_utc(exemption.ends_at), exemption.requested_amount
        )
        _, shortfall = self._plan_freezes(exemption.org_id, exemption.resource_kind, slices, priority)
        total_shortfall = sum(shortfall.values(), ZERO)
        if total_shortfall <= ZERO:
            raise ValidationFailed("当前额度充足，无需进入豁免复核")
        expires_at = utc_text(now + timedelta(hours=int(rule["review_ttl_hours"])))
        response = {
            "request_id": exemption.request_id,
            "org_id": exemption.org_id,
            "state": "pending",
            "shortfall_amount": decimal_text(total_shortfall),
            "shortfall_by_period": {period: decimal_text(amount) for period, amount in sorted(shortfall.items())},
            "expires_at": expires_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO exemption_requests(request_id,org_id,resource_kind,starts_at,ends_at,"
                    "requested_amount,shortfall_amount,reason,expires_at,idempotency_key,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        exemption.request_id,
                        exemption.org_id,
                        exemption.resource_kind,
                        exemption.starts_at,
                        exemption.ends_at,
                        decimal_text(quantize_amount(exemption.requested_amount)),
                        decimal_text(total_shortfall),
                        exemption.reason,
                        expires_at,
                        exemption.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotency("exemption", exemption.idempotency_key, raw, response)
                self._audit(
                    "exemption",
                    exemption.request_id,
                    "exemption.requested",
                    actor_id,
                    {
                        "org_id": exemption.org_id,
                        "resource_kind": exemption.resource_kind,
                        "requested_amount": decimal_text(quantize_amount(exemption.requested_amount)),
                        "shortfall_amount": decimal_text(total_shortfall),
                        "expires_at": expires_at,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("豁免编号或幂等键冲突") from exc
        return response

    def _expire_request_if_due(self, request: sqlite3.Row) -> sqlite3.Row:
        if request["state"] == "pending" and parse_utc(request["expires_at"]) <= self.clock.now():
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE exemption_requests SET state='expired' WHERE request_id=? AND state='pending'",
                    (request["request_id"],),
                )
                if cursor.rowcount == 1:
                    self._audit(
                        "exemption",
                        request["request_id"],
                        "exemption.expired",
                        "system",
                        {"expires_at": request["expires_at"]},
                    )
            return self.connection.execute(
                "SELECT * FROM exemption_requests WHERE request_id=?", (request["request_id"],)
            ).fetchone()
        return request

    def decide_exemption(
        self, actor_id: str, request_id: str, approve: object, note: object
    ) -> dict[str, Any]:
        self._require(actor_id, "exemption.decide")
        if not isinstance(approve, bool):
            raise ValidationFailed("approve 必须是布尔值")
        note_text = required_text(note, "note")
        row = self.connection.execute(
            "SELECT * FROM exemption_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFound("豁免复核不存在")
        if row["submitted_by"] == actor_id:
            raise Forbidden("复核人不能批准自己提交的豁免")
        row = self._expire_request_if_due(row)
        if row["state"] != "pending":
            raise InvalidState("豁免复核已处理或已过期")
        state = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exemption_requests SET state=?,decided_by=?,decided_at=?,decision_note=? "
                "WHERE request_id=? AND state='pending'",
                (state, actor_id, self._now(), note_text, request_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("豁免复核已被并发处理")
            self._audit(
                "exemption",
                request_id,
                "exemption.decided",
                actor_id,
                {"approved": approve, "note": note_text, "shortfall_amount": row["shortfall_amount"]},
            )
        return {"request_id": request_id, "state": state, "decided_by": actor_id}

    def list_exemptions(
        self, actor_id: str, state: str | None = None, org_id: str | None = None
    ) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] == "tenant_admin":
            org_id = user["org_id"]
        elif not (
            {"exemption.decide", "report.read"} & ROLE_PERMISSIONS[user["role"]]
        ):
            raise Forbidden(f"角色 {user['role']} 无权查看复核队列")
        pending = self.connection.execute(
            "SELECT * FROM exemption_requests WHERE state='pending' AND expires_at<=?", (self._now(),)
        ).fetchall()
        for row in pending:
            self._expire_request_if_due(row)
        clauses = []
        params: list[object] = []
        if org_id is not None:
            clauses.append("org_id=?")
            params.append(org_id)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM exemption_requests {where} ORDER BY submitted_at,request_id", params
        ).fetchall()
        return {"exemptions": [dict(row) for row in rows]}

    def settle_period(self, actor_id: str, org_id: str, period: object) -> dict[str, Any]:
        self._require(actor_id, "period.settle")
        self._org(org_id)
        period_text_value = period_text(period)
        _, period_end = period_bounds(period_text_value)
        if period_end > self.clock.now():
            raise InvalidState("账期尚未结束，不能结算")
        existing = self.connection.execute(
            "SELECT 1 FROM billing_periods WHERE org_id=? AND period=?", (org_id, period_text_value)
        ).fetchone()
        if existing is not None:
            raise Conflict("账期已结算")
        open_slices = self.connection.execute(
            "SELECT count(*) AS count FROM reservation_slices s JOIN reservations r "
            "ON r.reservation_id=s.reservation_id WHERE r.org_id=? AND s.period=? AND s.state='frozen'",
            (org_id, period_text_value),
        ).fetchone()
        if open_slices["count"]:
            raise InvalidState("账期内仍有未完成的冻结，不能结算")
        entries = self.connection.execute(
            "SELECT entry_type,amount FROM credit_ledger WHERE org_id=? AND period=?",
            (org_id, period_text_value),
        ).fetchall()
        totals = {"freeze": ZERO, "consume": ZERO, "refund": ZERO, "expire": ZERO}
        for entry in entries:
            totals[entry["entry_type"]] += Decimal(entry["amount"])
        rule = self._current_rule()
        response = {
            "org_id": org_id,
            "period": period_text_value,
            "state": "settled",
            "total_frozen": decimal_text(totals["freeze"]),
            "total_consumed": decimal_text(totals["consume"]),
            "total_refunded": decimal_text(totals["refund"]),
            "total_expired": decimal_text(totals["expire"]),
            "entry_count": len(entries),
            "rule_version": rule["rule_version"],
        }
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO billing_periods(org_id,period,total_frozen,total_consumed,total_refunded,"
                "total_expired,entry_count,rule_version,settled_by,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    period_text_value,
                    decimal_text(totals["freeze"]),
                    decimal_text(totals["consume"]),
                    decimal_text(totals["refund"]),
                    decimal_text(totals["expire"]),
                    len(entries),
                    rule["rule_version"],
                    actor_id,
                    self._now(),
                ),
            )
            self._audit("billing_period", f"{org_id}:{period_text_value}", "period.settled", actor_id, response)
        return response

    def billing_period(self, actor_id: str, org_id: str, period: object) -> dict[str, Any]:
        user = self._user(actor_id)
        self._org_scope(user, org_id)
        row = self.connection.execute(
            "SELECT * FROM billing_periods WHERE org_id=? AND period=?", (org_id, period_text(period))
        ).fetchone()
        if row is None:
            raise NotFound("账期尚未结算")
        return dict(row)

    def expire_credit_lines(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "line.expire")
        now = self.clock.now()
        rows = self.connection.execute("SELECT * FROM credit_lines WHERE state='active'").fetchall()
        due = [row for row in rows if parse_utc(row["valid_to"]) <= now]
        expired_lines: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for row in due:
                available = self._available(row)
                if available > ZERO:
                    self._write_ledger(
                        org_id=row["org_id"],
                        line_id=row["line_id"],
                        reservation_id=None,
                        period=period_of(now),
                        entry_type="expire",
                        amount=available,
                        reason="额度有效期届满核销",
                        rule_version=None,
                        actor_id=actor_id,
                    )
                else:
                    self.connection.execute(
                        "UPDATE credit_lines SET state='expired',revision=revision+1 WHERE line_id=?",
                        (row["line_id"],),
                    )
                expired_lines.append(
                    {"line_id": row["line_id"], "written_off": decimal_text(max(available, ZERO))}
                )
                self._audit(
                    "credit_line",
                    row["line_id"],
                    "credit_line.expired",
                    actor_id,
                    {"org_id": row["org_id"], "written_off": decimal_text(max(available, ZERO))},
                )
        return {"expired": expired_lines, "count": len(expired_lines)}

    def org_summary(self, actor_id: str, org_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        self._org_scope(user, org_id)
        self._org(org_id)
        rows = self.connection.execute(
            "SELECT * FROM credit_lines WHERE org_id=? ORDER BY created_at,line_id", (org_id,)
        ).fetchall()
        sources: dict[str, dict[str, Decimal]] = {}
        lines: list[dict[str, Any]] = []
        for row in rows:
            available = self._available(row)
            bucket = sources.setdefault(
                row["source"],
                {"total": ZERO, "consumed": ZERO, "frozen": ZERO, "expired": ZERO, "available": ZERO},
            )
            bucket["total"] += Decimal(row["total_amount"])
            bucket["consumed"] += Decimal(row["consumed_amount"])
            bucket["frozen"] += Decimal(row["frozen_amount"])
            bucket["expired"] += Decimal(row["expired_amount"])
            bucket["available"] += available
            lines.append({**dict(row), "available_amount": decimal_text(available)})
        return {
            "org_id": org_id,
            "generated_at": self._now(),
            "sources": {
                source: {key: decimal_text(value) for key, value in totals.items()}
                for source, totals in sorted(sources.items())
            },
            "lines": lines,
        }

    def org_ledger(self, actor_id: str, org_id: str, limit: int = 200) -> dict[str, Any]:
        user = self._user(actor_id)
        self._org_scope(user, org_id)
        self._org(org_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationFailed("limit 必须是 1 到 1000 的整数")
        rows = self.connection.execute(
            "SELECT * FROM credit_ledger WHERE org_id=? ORDER BY entry_id DESC LIMIT ?", (org_id, limit)
        ).fetchall()
        return {"org_id": org_id, "entries": [dict(row) for row in rows]}

    def explain_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        self._org_scope(user, row["org_id"])
        slices = self.connection.execute(
            "SELECT * FROM reservation_slices WHERE reservation_id=? ORDER BY slice_id", (reservation_id,)
        ).fetchall()
        entries = self.connection.execute(
            "SELECT * FROM credit_ledger WHERE reservation_id=? ORDER BY entry_id", (reservation_id,)
        ).fetchall()
        exemption = None
        if row["exemption_id"]:
            found = self.connection.execute(
                "SELECT * FROM exemption_requests WHERE request_id=?", (row["exemption_id"],)
            ).fetchone()
            exemption = None if found is None else dict(found)
        rule = self.connection.execute(
            "SELECT * FROM credit_rules WHERE rule_version=?", (row["rule_version"],)
        ).fetchone()
        return {
            "reservation": dict(row),
            "slices": [dict(item) for item in slices],
            "ledger": [dict(item) for item in entries],
            "exemption": exemption,
            "rule": None if rule is None else dict(rule),
        }

    def explain_line(self, actor_id: str, line_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self._line_row(line_id)
        self._org_scope(user, row["org_id"])
        entries = self.connection.execute(
            "SELECT * FROM credit_ledger WHERE line_id=? ORDER BY entry_id", (line_id,)
        ).fetchall()
        return {
            "line": {**dict(row), "available_amount": decimal_text(self._available(row))},
            "ledger": [dict(item) for item in entries],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM credit_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
