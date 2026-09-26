"""多租户算力信用额度的事务用例。

覆盖额度来源（预付/后付/专项赠送）、适用资源与有效期、预约确认时的
额度选择与资源预留原子落账、跨月切分、取消/失败仅返还未执行部分、
有期限超额复核、职责分离、已结算账期不可回写，以及租户隔离的查询。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .domain import (
    CreditAllocation,
    Segment,
    canonical_json,
    decimal_text,
    digest,
    period_key,
    quantize_hours,
    quantize_money,
    select_credits,
    split_by_month,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import SOURCE_TYPES, CreditGrant, ReservationRequest
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "tenant": {
        "credit.read_self",
        "reservation.write",
        "reservation.read_self",
        "reservation.lifecycle",
        "ledger.read_self",
    },
    "finance": {
        "tenant.write",
        "user.write",
        "credit.write",
        "rule.write",
        "pool.write",
        "period.close",
        "reservation.lifecycle",
        "credit.read_all",
        "ledger.read_all",
        "report.read",
    },
    "reviewer": {
        "review.read_pending",
        "review.read_all",
        "review.decide",
        "credit.read_all",
        "ledger.read_all",
    },
    "auditor": {
        "credit.read_all",
        "ledger.read_all",
        "review.read_all",
        "report.read",
        "audit.read",
    },
}

DEFAULT_PRIORITY = ["prepaid", "grant", "postpaid"]
DEFAULT_REVIEW_TTL_HOURS = 48
ZERO = Decimal("0")


class CreditService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础
    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _now_dt(self):
        return self.clock.now()

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

    def _tenant(self, tenant_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_tenants WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise NotFound("租户不存在")
        if not row["active"]:
            raise InvalidState("租户已停用")
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

    def _ledger(
        self,
        *,
        tenant_id: str,
        entry_type: str,
        amount: Decimal,
        created_by: str,
        reason: str,
        reservation_id: str | None = None,
        segment_id: int | None = None,
        credit_id: str | None = None,
        source_type: str | None = None,
        period: str | None = None,
        settled: bool = False,
        rule_revision: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO credit_ledger(tenant_id,reservation_id,segment_id,credit_id,entry_type,"
            "source_type,amount,period_key,settled,rule_revision,reason,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant_id,
                reservation_id,
                segment_id,
                credit_id,
                entry_type,
                source_type,
                decimal_text(quantize_money(amount)),
                period,
                1 if settled else 0,
                rule_revision,
                reason,
                created_by,
                self._now(),
            ),
        )

    def _require_period_open(self, tenant_id: str, period: str) -> None:
        row = self.connection.execute(
            "SELECT state FROM accounting_periods WHERE tenant_id=? AND period_key=?",
            (tenant_id, period),
        ).fetchone()
        if row is not None and row["state"] == "closed":
            raise InvalidState(f"账期 {period} 已结算，禁止回写")

    # ------------------------------------------------------------- 目录管理
    def _is_bootstrap(self) -> bool:
        """系统中尚无任何用户时允许首个内部账号自举（与供应服务一致）。"""
        return self.connection.execute("SELECT 1 FROM credit_users LIMIT 1").fetchone() is None

    def create_tenant(self, actor_id: str, tenant_id: str, name: str) -> dict[str, Any]:
        if not self._is_bootstrap():
            self._require(actor_id, "tenant.write")
        if not tenant_id.strip() or not name.strip():
            raise ValidationFailed("租户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_tenants(tenant_id,name,created_at) VALUES(?,?,?)",
                    (tenant_id.strip(), name.strip(), self._now()),
                )
                self._audit("tenant", tenant_id.strip(), "tenant.created", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("租户已经存在") from exc
        return {"tenant_id": tenant_id.strip(), "name": name.strip()}

    def create_user(
        self, actor_id: str, user_id: str, display_name: str, role: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        bootstrap = self._is_bootstrap()
        if not bootstrap:
            self._require(actor_id, "user.write")
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if bootstrap and role == "tenant":
            raise ValidationFailed("首个自举账号必须是内部角色（财务/复核/审计）")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "tenant":
            if not tenant_id:
                raise ValidationFailed("租户角色必须归属某个租户")
            self._tenant(tenant_id)
        elif tenant_id:
            raise ValidationFailed("只有租户角色可以归属租户")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_users(user_id,display_name,role,tenant_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, tenant_id, self._now()),
                )
                self._audit("user", user_id.strip(), "user.created", actor_id, {"role": role, "tenant_id": tenant_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在或租户不存在") from exc
        return {"user_id": user_id.strip(), "role": role, "tenant_id": tenant_id}

    def create_resource_pool(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "pool.write")
        pool_id = raw.get("pool_id")
        if not isinstance(pool_id, str) or not pool_id.strip():
            raise ValidationFailed("pool_id 不能为空")
        facility_id = raw.get("facility_id", "")
        product = str(raw.get("product", ""))
        if product not in {"gpu-h100", "gpu-a100", "gpu-l40s", "accelerator-npu", "cpu-highmem", "storage-io"}:
            raise ValidationFailed("product 不是受支持的资源类型")
        available = Decimal(str(raw.get("available_hours", "0")))
        price = Decimal(str(raw.get("unit_price_cny", "0")))
        if available < 0 or price < 0:
            raise ValidationFailed("可用工时与单价不能为负数")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resource_pools(pool_id,facility_id,product,available_hours,"
                    "unit_price_cny,created_at) VALUES(?,?,?,?,?,?)",
                    (pool_id.strip(), str(facility_id), product, decimal_text(quantize_hours(available)),
                     decimal_text(quantize_money(price)), self._now()),
                )
                self._audit("pool", pool_id.strip(), "pool.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源池编号已经存在") from exc
        return self.resource_pool(pool_id.strip())

    def resource_pool(self, pool_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM resource_pools WHERE pool_id=?", (pool_id,)).fetchone()
        if row is None:
            raise NotFound("资源池不存在")
        return dict(row)

    # ------------------------------------------------------------- 额度来源
    def grant_credit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "credit.write")
        grant = CreditGrant.from_dict(raw)
        self._tenant(grant.tenant_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO credit_accounts(credit_id,tenant_id,source_type,amount_total,"
                    "frozen_amount,consumed_amount,products_json,valid_from,valid_to,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.credit_id,
                        grant.tenant_id,
                        grant.source_type,
                        decimal_text(quantize_money(grant.amount)),
                        decimal_text(quantize_money(ZERO)),
                        decimal_text(quantize_money(ZERO)),
                        canonical_json(grant.products),
                        utc_text(grant.valid_from),
                        None if grant.valid_to is None else utc_text(grant.valid_to),
                        actor_id,
                        self._now(),
                    ),
                )
                self._ledger(
                    tenant_id=grant.tenant_id,
                    entry_type="grant",
                    amount=grant.amount,
                    created_by=actor_id,
                    reason=f"{grant.source_type} 额度入账 {grant.credit_id}",
                    credit_id=grant.credit_id,
                    source_type=grant.source_type,
                )
                self._audit("credit", grant.credit_id, "credit.granted", actor_id, {
                    "tenant_id": grant.tenant_id,
                    "source_type": grant.source_type,
                    "amount": decimal_text(grant.amount),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("额度编号冲突或租户不存在") from exc
        return self.credit_account(grant.credit_id)

    def credit_account(self, credit_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM credit_accounts WHERE credit_id=?", (credit_id,)
        ).fetchone()
        if row is None:
            raise NotFound("额度账户不存在")
        result = dict(row)
        result["products"] = json.loads(result.pop("products_json"))
        return result

    def _account_views(self, tenant_id: str, product: str):
        rows = self.connection.execute(
            "SELECT * FROM credit_accounts WHERE tenant_id=? AND state='active' ORDER BY credit_id",
            (tenant_id,),
        ).fetchall()
        views = []
        for row in rows:
            frozen = Decimal(row["frozen_amount"])
            consumed = Decimal(row["consumed_amount"])
            views.append({
                "credit_id": row["credit_id"],
                "source_type": row["source_type"],
                "products": json.loads(row["products_json"]),
                "available": Decimal(row["amount_total"]) - frozen - consumed,
                "valid_from": parse_utc(row["valid_from"]),
                "valid_to": None if row["valid_to"] is None else parse_utc(row["valid_to"]),
            })
        return views

    # ------------------------------------------------------------- 选择规则
    def put_rule(
        self,
        actor_id: str,
        tenant_id: str | None,
        source_priority: Sequence[str] | None,
        allow_overage: bool,
    ) -> dict[str, Any]:
        """发布新版本规则；旧版本标记 superseded，历史落账保留其 revision 不回写。"""
        self._require(actor_id, "rule.write")
        if tenant_id is not None:
            self._tenant(tenant_id)
        priority = list(source_priority) if source_priority else list(DEFAULT_PRIORITY)
        if sorted(priority) != sorted(SOURCE_TYPES) or len(priority) != len(SOURCE_TYPES):
            raise ValidationFailed("source_priority 必须恰好包含 prepaid、postpaid、grant")
        scope = tenant_id if tenant_id is not None else "*"
        with transaction(self.connection, immediate=True):
            last = self.connection.execute(
                "SELECT max(revision) AS revision FROM credit_rules WHERE tenant_id IS ?",
                (tenant_id,),
            ).fetchone()
            revision = int(last["revision"] or 0) + 1
            self.connection.execute(
                "UPDATE credit_rules SET state='superseded' WHERE tenant_id IS ? AND state='active'",
                (tenant_id,),
            )
            self.connection.execute(
                "INSERT INTO credit_rules(tenant_id,revision,source_priority_json,allow_overage,"
                "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    tenant_id,
                    revision,
                    canonical_json(priority),
                    1 if allow_overage else 0,
                    self._now(),
                    actor_id,
                    self._now(),
                ),
            )
            self._audit("rule", scope, "rule.published", actor_id, {
                "tenant_id": tenant_id,
                "revision": revision,
                "source_priority": priority,
                "allow_overage": allow_overage,
            })
        return {"tenant_id": tenant_id, "revision": revision, "source_priority": priority,
                "allow_overage": allow_overage, "state": "active"}

    def _active_rule(self, tenant_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM credit_rules WHERE tenant_id=? AND state='active'",
            (tenant_id,),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT * FROM credit_rules WHERE tenant_id IS NULL AND state='active'"
            ).fetchone()
        if row is None:
            raise InvalidState("尚未发布额度选择规则")
        return row

    # ------------------------------------------------------------- 预约落账
    def _check_idempotent(self, scope: str, key: str, digest_value: str):
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM credit_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != digest_value:
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _save_idempotent(self, scope: str, key: str, digest_value: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO credit_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest_value, canonical_json(response), self._now()),
        )

    def submit_reservation(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "reservation.write")
        request = ReservationRequest.from_dict(raw)
        # 租户用户只能为自己机构提交。
        if user["role"] == "tenant" and user["tenant_id"] != request.tenant_id:
            raise Forbidden("不能为其他机构提交预约")
        self._tenant(request.tenant_id)
        pool = self.resource_pool(request.pool_id)
        if not pool["active"]:
            raise InvalidState("资源池已停用")
        request_digest = digest(raw)
        cached = self._check_idempotent("reservation", request.idempotency_key, request_digest)
        if cached is not None:
            return cached

        unit_price = Decimal(pool["unit_price_cny"])
        segments = split_by_month(
            request.starts_at, request.ends_at, request.requested_hours, unit_price
        )
        # 预约触及的任何账期已结算，则整体拒绝（规则更新不得回写已结算账期）。
        for seg in segments:
            self._require_period_open(request.tenant_id, seg.period_key)
        est_total = quantize_money(sum((seg.amount for seg in segments), ZERO))
        rule = self._active_rule(request.tenant_id)
        priority = json.loads(rule["source_priority_json"])
        accounts = self._account_views(request.tenant_id, pool["product"])
        allocations = select_credits(
            accounts, est_total, pool["product"], priority, self._now_dt()
        )
        covered = quantize_money(sum((item.amount for item in allocations), ZERO))
        shortfall = quantize_money(est_total - covered)
        allow_overage = bool(rule["allow_overage"])

        response: dict[str, Any]
        with transaction(self.connection, immediate=True):
            if shortfall > ZERO and not allow_overage:
                # 先落待复核预约（复核单号稍后回填），再进入有期限复核队列；
                # 不冻结额度、不预留资源。
                self._insert_reservation(
                    request, pool, est_total, covered, shortfall,
                    state="pending_review", review_id=None, rule_revision=int(rule["revision"]),
                    actor_id=actor_id,
                )
                review_id = self._open_review(
                    request, est_total, covered, shortfall, rule, actor_id, pool
                )
                self.connection.execute(
                    "UPDATE reservations SET review_id=? WHERE reservation_id=?",
                    (review_id, request.reservation_id),
                )
                self._audit("reservation", request.reservation_id, "reservation.review_queued", actor_id, {
                    "shortfall": decimal_text(shortfall), "review_id": review_id,
                })
                response = {
                    "reservation_id": request.reservation_id,
                    "state": "pending_review",
                    "review_id": review_id,
                    "est_total": decimal_text(est_total),
                    "covered_amount": decimal_text(covered),
                    "shortfall_amount": decimal_text(shortfall),
                }
            else:
                response = self._confirm(
                    request, pool, unit_price, segments, allocations,
                    est_total, covered, shortfall, int(rule["revision"]), actor_id,
                )
            self._save_idempotent("reservation", request.idempotency_key, request_digest, response)
        return response

    def _open_review(
        self, request, est_total, covered, shortfall, rule, actor_id, pool
    ) -> int:
        expires = self.clock.now() + timedelta(hours=DEFAULT_REVIEW_TTL_HOURS)
        cursor = self.connection.execute(
            "INSERT INTO overage_reviews(tenant_id,reservation_id,requested_amount,covered_amount,"
            "shortfall_amount,reason,submitted_by,submitted_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                request.tenant_id,
                request.reservation_id,
                decimal_text(est_total),
                decimal_text(covered),
                decimal_text(shortfall),
                request.reason,
                actor_id,
                self._now(),
                utc_text(expires),
            ),
        )
        review_id = int(cursor.lastrowid)
        self._ledger(
            tenant_id=request.tenant_id,
            entry_type="overage",
            amount=shortfall,
            created_by=actor_id,
            reason=f"超额申请进入复核 {review_id}",
            reservation_id=request.reservation_id,
            rule_revision=int(rule["revision"]),
        )
        return review_id

    def _insert_reservation(
        self, request, pool, est_total, covered, shortfall, *, state, review_id, rule_revision, actor_id
    ) -> None:
        self.connection.execute(
            "INSERT INTO reservations(reservation_id,tenant_id,pool_id,product,requested_hours,"
            "starts_at,ends_at,est_total,covered_amount,overage_amount,state,review_id,"
            "rule_revision,idempotency_key,submitted_by,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.reservation_id,
                request.tenant_id,
                request.pool_id,
                pool["product"],
                decimal_text(request.requested_hours),
                utc_text(request.starts_at),
                utc_text(request.ends_at),
                decimal_text(est_total),
                decimal_text(covered),
                decimal_text(shortfall),
                state,
                review_id,
                rule_revision,
                request.idempotency_key,
                actor_id,
                self._now(),
            ),
        )

    def _confirm(
        self, request, pool, unit_price, segments: list[Segment],
        allocations: list[CreditAllocation], est_total, covered, shortfall,
        rule_revision, actor_id,
    ) -> dict[str, Any]:
        """额度选择与资源预留的原子落账：任一步失败整体回滚。"""
        # 资源容量校验并占用（乐观锁）。
        available_hours = Decimal(pool["available_hours"])
        if available_hours + ZERO < request.requested_hours:
            raise Conflict("资源池可用工时不足")

        # 落账预约主表。
        self.connection.execute(
            "INSERT INTO reservations(reservation_id,tenant_id,pool_id,product,requested_hours,"
            "starts_at,ends_at,est_total,covered_amount,overage_amount,state,rule_revision,"
            "idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.reservation_id, request.tenant_id, request.pool_id, pool["product"],
                decimal_text(request.requested_hours), utc_text(request.starts_at),
                utc_text(request.ends_at), decimal_text(est_total), decimal_text(covered),
                decimal_text(shortfall), "confirmed", rule_revision, request.idempotency_key,
                actor_id, self._now(),
            ),
        )

        # 按段冻结：各段按来源顺序消耗全局选择结果，账期开放才能写。
        total_frozen = self._apply_segments(
            reservation_id=request.reservation_id,
            tenant_id=request.tenant_id,
            segments=segments,
            allocations=allocations,
            shortfall=shortfall,
            rule_revision=rule_revision,
            actor_id=actor_id,
            overage_reason="允许超额确认，缺口计入后付",
        )

        # 原子占用资源工时。
        cursor = self.connection.execute(
            "UPDATE resource_pools SET available_hours=?,revision=revision+1 "
            "WHERE pool_id=? AND revision=? AND CAST(available_hours AS REAL)>=CAST(? AS REAL)",
            (
                decimal_text(quantize_hours(available_hours - request.requested_hours)),
                request.pool_id, pool["revision"], decimal_text(request.requested_hours),
            ),
        )
        if cursor.rowcount != 1:
            raise Conflict("资源池版本已变化或工时不足")

        self._audit("reservation", request.reservation_id, "reservation.confirmed", actor_id, {
            "est_total": decimal_text(est_total),
            "covered": decimal_text(covered),
            "overage": decimal_text(shortfall),
            "segments": len(segments),
            "rule_revision": rule_revision,
        })
        return {
            "reservation_id": request.reservation_id,
            "state": "confirmed",
            "est_total": decimal_text(est_total),
            "covered_amount": decimal_text(covered),
            "overage_amount": decimal_text(shortfall),
            "frozen_amount": decimal_text(quantize_money(total_frozen)),
            "segments": [
                {"period_key": s.period_key, "amount": decimal_text(s.amount),
                 "hours": decimal_text(s.hours)}
                for s in segments
            ],
        }

    def _adjust_account(
        self, credit_id: str, *, frozen_delta=ZERO, consumed_delta=ZERO,
        require_available: Decimal | None = None, error: str = "额度账户余额不足",
    ) -> None:
        """在写事务内规范更新账户金额，保持文本为两位小数并校验可用余额。"""
        row = self.connection.execute(
            "SELECT amount_total,frozen_amount,consumed_amount,revision "
            "FROM credit_accounts WHERE credit_id=?",
            (credit_id,),
        ).fetchone()
        if row is None:
            raise Conflict(f"额度账户 {credit_id} 不存在")
        total = Decimal(row["amount_total"])
        frozen = quantize_money(Decimal(row["frozen_amount"]) + frozen_delta)
        consumed = quantize_money(Decimal(row["consumed_amount"]) + consumed_delta)
        if frozen < ZERO or consumed < ZERO:
            raise Conflict(error)
        if require_available is not None and total - frozen - consumed < ZERO:
            raise Conflict(error)
        if frozen + consumed > total:
            raise Conflict(error)
        self.connection.execute(
            "UPDATE credit_accounts SET frozen_amount=?,consumed_amount=?,revision=revision+1 "
            "WHERE credit_id=? AND revision=?",
            (decimal_text(frozen), decimal_text(consumed), credit_id, row["revision"]),
        )

    def _adjust_pool(self, pool_id: str, *, hours_delta: Decimal) -> None:
        """写事务内规范更新资源池可用工时。"""
        row = self.connection.execute(
            "SELECT available_hours,revision FROM resource_pools WHERE pool_id=?",
            (pool_id,),
        ).fetchone()
        available = quantize_hours(Decimal(row["available_hours"]) + hours_delta)
        if available < ZERO:
            raise Conflict("资源池可用工时不足")
        self.connection.execute(
            "UPDATE resource_pools SET available_hours=?,revision=revision+1 "
            "WHERE pool_id=? AND revision=?",
            (decimal_text(available), pool_id, row["revision"]),
        )

    def _place_hold(
        self, segment_id, credit_id, source_type, hold, rank, seg,
        tenant_id, actor_id, rule_revision,
    ) -> None:
        hold = quantize_money(hold)
        # 原子条件冻结：写事务内读取当前余额并写入规范化文本，仅当可用足够时成功。
        self._adjust_account(
            credit_id, frozen_delta=hold, require_available=hold,
            error=f"额度账户 {credit_id} 可用余额不足",
        )
        self.connection.execute(
            "INSERT INTO segment_holds(segment_id,credit_id,source_type,amount,frozen_amount,"
            "priority_rank) VALUES(?,?,?,?,?,?)",
            (segment_id, credit_id, source_type, decimal_text(hold), decimal_text(hold), rank),
        )
        self._ledger(
            tenant_id=tenant_id,
            entry_type="freeze",
            amount=hold,
            created_by=actor_id,
            reason=f"预约冻结 {seg.period_key}",
            segment_id=segment_id,
            credit_id=credit_id,
            source_type=source_type,
            period=seg.period_key,
            rule_revision=rule_revision,
        )

    # ------------------------------------------------------------- 超额复核
    def _expire_due_reviews(self) -> int:
        rows = self.connection.execute(
            "SELECT review_id,reservation_id,tenant_id FROM overage_reviews "
            "WHERE state='pending' AND expires_at<=?",
            (self._now(),),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE overage_reviews SET state='expired',decided_at=? WHERE review_id=? AND state='pending'",
                (self._now(), row["review_id"]),
            )
            self.connection.execute(
                "UPDATE reservations SET state='rejected',revision=revision+1 "
                "WHERE reservation_id=? AND state='pending_review'",
                (row["reservation_id"],),
            )
            self._audit("review", str(row["review_id"]), "review.expired", "system", {
                "reservation_id": row["reservation_id"],
            })
        return len(rows)

    def decide_review(self, actor_id: str, review_id: int, approve: bool, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "review.decide")
        with transaction(self.connection, immediate=True):
            self._expire_due_reviews()
            review = self.connection.execute(
                "SELECT * FROM overage_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if review is None:
                raise NotFound("复核单不存在")
            if review["state"] != "pending":
                raise InvalidState(f"复核单已 {review['state']}")
            # 职责分离：不能批准/驳回自己提交的豁免。
            if review["submitted_by"] == actor_id:
                raise Forbidden("不能审批自己提交的超额豁免")
            if approve:
                self._approve_review(review, actor_id, note)
                new_state = "confirmed"
            else:
                self.connection.execute(
                    "UPDATE overage_reviews SET state='rejected',decided_by=?,decided_at=?,"
                    "decision_note=? WHERE review_id=?",
                    (actor_id, self._now(), note, review_id),
                )
                self.connection.execute(
                    "UPDATE reservations SET state='rejected',revision=revision+1 "
                    "WHERE reservation_id=? AND state='pending_review'",
                    (review["reservation_id"],),
                )
                new_state = "rejected"
            self._audit("review", str(review_id), "review.decided", actor_id, {
                "approve": approve, "reservation_id": review["reservation_id"], "note": note,
            })
        return {"review_id": review_id, "state": new_state,
                "reservation_id": review["reservation_id"]}

    def _approve_review(self, review, actor_id, note) -> None:
        reservation = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (review["reservation_id"],)
        ).fetchone()
        if reservation is None or reservation["state"] != "pending_review":
            raise InvalidState("预约不是待复核状态")
        pool = self.resource_pool(reservation["pool_id"])
        unit_price = Decimal(pool["unit_price_cny"])
        segments = split_by_month(
            parse_utc(reservation["starts_at"]), parse_utc(reservation["ends_at"]),
            Decimal(reservation["requested_hours"]), unit_price,
        )
        est_total = Decimal(reservation["est_total"])
        accounts = self._account_views(reservation["tenant_id"], pool["product"])
        rule = self.connection.execute(
            "SELECT * FROM credit_rules WHERE tenant_id=? AND state='active'",
            (reservation["tenant_id"],),
        ).fetchone()
        rule_revision = int(reservation["rule_revision"])
        # 复核批准时按当前余额重新选择来源（余额可能已变化），不足部分仍作为豁免超额。
        allocations = select_credits(
            accounts, est_total, pool["product"],
            json.loads(rule["source_priority_json"]) if rule is not None else list(DEFAULT_PRIORITY),
            self._now_dt(),
        )
        now_covered = quantize_money(sum((item.amount for item in allocations), ZERO))
        now_shortfall = quantize_money(est_total - min(now_covered, est_total))

        self.connection.execute(
            "UPDATE overage_reviews SET state='approved',decided_by=?,decided_at=?,decision_note=? "
            "WHERE review_id=?",
            (actor_id, self._now(), note, review["review_id"]),
        )
        self.connection.execute(
            "UPDATE reservations SET state='confirmed',covered_amount=?,overage_amount=?,"
            "revision=revision+1 WHERE reservation_id=?",
            (decimal_text(est_total - now_shortfall), decimal_text(now_shortfall),
             reservation["reservation_id"]),
        )
        self._apply_segments(
            reservation_id=reservation["reservation_id"],
            tenant_id=reservation["tenant_id"],
            segments=segments,
            allocations=allocations,
            shortfall=now_shortfall,
            rule_revision=rule_revision,
            actor_id=actor_id,
            overage_reason=f"复核 {review['review_id']} 批准超额豁免",
        )
        # 占用资源工时。
        available_hours = Decimal(pool["available_hours"])
        requested_hours = Decimal(reservation["requested_hours"])
        if available_hours < requested_hours:
            raise Conflict("资源池可用工时不足，无法确认豁免")
        cursor = self.connection.execute(
            "UPDATE resource_pools SET available_hours=?,revision=revision+1 "
            "WHERE pool_id=? AND revision=? AND CAST(available_hours AS REAL)>=CAST(? AS REAL)",
            (decimal_text(quantize_hours(available_hours - requested_hours)),
             pool["pool_id"], pool["revision"], decimal_text(requested_hours)),
        )
        if cursor.rowcount != 1:
            raise Conflict("资源池版本已变化")

    def _apply_segments(
        self, *, reservation_id, tenant_id, segments, allocations, shortfall,
        rule_revision, actor_id, overage_reason,
    ) -> Decimal:
        """在来源额度间按段落冻结，并把未覆盖部分记为段超额；返回冻结总额。"""
        remaining_by_credit = {item.credit_id: item.amount for item in allocations}
        rank_by_credit = {item.credit_id: item.rank for item in allocations}
        source_by_credit = {item.credit_id: item.source_type for item in allocations}
        ordered_credits = [item.credit_id for item in allocations]
        total_frozen = ZERO
        total_overage = ZERO
        for seg in segments:
            self._require_period_open(tenant_id, seg.period_key)
            seg_cursor = self.connection.execute(
                "INSERT INTO reservation_segments(reservation_id,period_key,seg_index,seg_start,"
                "seg_end,hours,amount) VALUES(?,?,?,?,?,?,?)",
                (
                    reservation_id, seg.period_key, seg.index,
                    utc_text(seg.start), utc_text(seg.end), decimal_text(seg.hours),
                    decimal_text(seg.amount),
                ),
            )
            segment_id = int(seg_cursor.lastrowid)
            seg_remaining = seg.amount
            for credit_id in ordered_credits:
                if seg_remaining <= ZERO:
                    break
                left = remaining_by_credit[credit_id]
                if left <= ZERO:
                    continue
                hold = quantize_money(min(left, seg_remaining))
                self._place_hold(
                    segment_id, credit_id, source_by_credit[credit_id], hold,
                    rank_by_credit[credit_id], seg, tenant_id, actor_id, rule_revision,
                )
                remaining_by_credit[credit_id] = quantize_money(left - hold)
                seg_remaining = quantize_money(seg_remaining - hold)
                total_frozen += hold
            seg_overage = max(ZERO, seg_remaining)
            if seg_overage > ZERO:
                total_overage += seg_overage
                self.connection.execute(
                    "UPDATE reservation_segments SET overage_amount=? WHERE segment_id=?",
                    (decimal_text(seg_overage), segment_id),
                )
                self._ledger(
                    tenant_id=tenant_id,
                    entry_type="overage",
                    amount=seg_overage,
                    created_by=actor_id,
                    reason=overage_reason,
                    reservation_id=reservation_id,
                    segment_id=segment_id,
                    period=seg.period_key,
                    rule_revision=rule_revision,
                )
        if quantize_money(total_overage) != quantize_money(shortfall):
            # 全局选择与分段结果必须一致，否则整体回滚。
            raise Conflict("额度冻结与分段金额不一致")
        return total_frozen

    # ------------------------------------------------------- 执行/取消/失败
    def start_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "reservation.lifecycle")
        with transaction(self.connection, immediate=True):
            reservation = self._locked_reservation(reservation_id)
            self._assert_reservation_scope(user, reservation)
            if reservation["state"] != "confirmed":
                raise InvalidState("只有已确认预约可以开始")
            self.connection.execute(
                "UPDATE reservations SET state='running',revision=revision+1 WHERE reservation_id=?",
                (reservation_id,),
            )
            self._audit("reservation", reservation_id, "reservation.started", actor_id, {})
        return {"reservation_id": reservation_id, "state": "running"}

    def _assert_reservation_scope(self, user: sqlite3.Row, reservation: sqlite3.Row) -> None:
        if user["role"] == "tenant" and user["tenant_id"] != reservation["tenant_id"]:
            raise Forbidden("只能操作本机构预约")

    def _locked_reservation(self, reservation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        return row

    def _settle_or_release(
        self, reservation_id: str, executed_until, actor_id: str, *, failed: bool, completed: bool = False
    ) -> dict[str, Any]:
        """按可注入时钟切分实际执行：到 executed_until 为止转为消耗，之后返还未执行部分。"""
        if completed:
            event, new_state = "completed", "completed"
        else:
            event, new_state = ("failed", "failed") if failed else ("cancelled", "cancelled")
        with transaction(self.connection, immediate=True):
            user = self._require(actor_id, "reservation.lifecycle")
            reservation = self._locked_reservation(reservation_id)
            self._assert_reservation_scope(user, reservation)
            allowed = {"confirmed", "running"}
            if completed and reservation["state"] != "running":
                raise InvalidState("只有运行中的预约可以标记完成")
            if not completed and reservation["state"] not in allowed:
                raise InvalidState(f"预约状态 {reservation['state']} 不能{event}")
            start = parse_utc(reservation["starts_at"])
            end = parse_utc(reservation["ends_at"])
            cutoff = self.clock.now() if executed_until is None else executed_until
            if cutoff < start:
                cutoff = start
            if cutoff > end:
                cutoff = end

            segments = self.connection.execute(
                "SELECT * FROM reservation_segments WHERE reservation_id=? ORDER BY seg_index",
                (reservation_id,),
            ).fetchall()
            executed_total = ZERO
            released_total = ZERO
            for seg in segments:
                seg_start = parse_utc(seg["seg_start"])
                seg_end = parse_utc(seg["seg_end"])
                seg_amount = Decimal(seg["amount"])
                seg_seconds = Decimal(str((seg_end - seg_start).total_seconds()))
                if cutoff >= seg_end:
                    executed_fraction = Decimal("1")
                elif cutoff <= seg_start:
                    executed_fraction = ZERO
                else:
                    executed_fraction = Decimal(str((cutoff - seg_start).total_seconds())) / seg_seconds
                # 已冻结部分按比例区分消耗与返还。
                holds = self.connection.execute(
                    "SELECT * FROM segment_holds WHERE segment_id=? ORDER BY priority_rank,hold_id",
                    (seg["segment_id"],),
                ).fetchall()
                seg_held = sum((Decimal(h["frozen_amount"]) for h in holds), ZERO)
                seg_executed_held = quantize_money(seg_held * executed_fraction)
                seg_release_held = quantize_money(seg_held - seg_executed_held)
                # 段上的超额部分同理：已执行才计入后付消耗。
                seg_overage = Decimal(seg["overage_amount"])
                executed_overage = quantize_money(seg_overage * executed_fraction)
                released_overage = quantize_money(seg_overage - executed_overage)

                self._settle_holds(
                    holds, seg, seg_executed_held, seg_release_held,
                    reservation, actor_id,
                )
                if executed_overage > ZERO:
                    # 已执行的超额缺口转为后付实际消耗（账期须仍开放）。
                    self._require_period_open(reservation["tenant_id"], seg["period_key"])
                    self._ledger(
                        tenant_id=reservation["tenant_id"],
                        entry_type="consume",
                        amount=executed_overage,
                        created_by=actor_id,
                        reason=f"{seg['period_key']} 超额部分实际消耗（后付）",
                        reservation_id=reservation_id,
                        segment_id=seg["segment_id"],
                        source_type="postpaid",
                        period=seg["period_key"],
                        rule_revision=reservation["rule_revision"],
                    )
                if released_overage > ZERO:
                    self._ledger(
                        tenant_id=reservation["tenant_id"],
                        entry_type="release",
                        amount=released_overage,
                        created_by=actor_id,
                        reason=f"{event} 返还未执行超额",
                        reservation_id=reservation_id,
                        segment_id=seg["segment_id"],
                        period=seg["period_key"],
                    )
                executed_total += seg_executed_held + executed_overage
                released_total += seg_release_held + released_overage
                self.connection.execute(
                    "UPDATE reservation_segments SET consumed_amount=?,released_amount=?,"
                    "overage_amount=? WHERE segment_id=?",
                    (
                        decimal_text(seg_executed_held),
                        decimal_text(seg_release_held),
                        decimal_text(executed_overage),
                        seg["segment_id"],
                    ),
                )

            # 返还资源工时：仅未执行部分。
            total_hours = Decimal(reservation["requested_hours"])
            window = Decimal(str((end - start).total_seconds()))
            executed_hours = quantize_hours(total_hours * (Decimal(str((cutoff - start).total_seconds())) / window))
            released_hours = quantize_hours(total_hours - executed_hours)
            if released_hours > ZERO:
                self._adjust_pool(reservation["pool_id"], hours_delta=released_hours)

            new_state = "failed" if failed else "cancelled"
            self.connection.execute(
                "UPDATE reservations SET state=?,revision=revision+1 WHERE reservation_id=?",
                (new_state, reservation_id),
            )
            self._audit("reservation", reservation_id, f"reservation.{event}", actor_id, {
                "cutoff": utc_text(cutoff),
                "executed_amount": decimal_text(quantize_money(executed_total)),
                "released_amount": decimal_text(quantize_money(released_total)),
                "released_hours": decimal_text(released_hours),
            })
        return {
            "reservation_id": reservation_id,
            "state": new_state,
            "executed_amount": decimal_text(quantize_money(executed_total)),
            "released_amount": decimal_text(quantize_money(released_total)),
            "released_hours": decimal_text(released_hours),
        }

    def _settle_holds(self, holds, seg, executed_held, released_held, reservation, actor_id) -> None:
        # 在各来源间按冻结比例分摊执行/返还，末个 hold 吸收量化误差。
        total_held = sum((Decimal(h["frozen_amount"]) for h in holds), ZERO)
        if total_held <= ZERO:
            return
        allocated_exec = ZERO
        allocated_release = ZERO
        for index, hold in enumerate(holds):
            frozen = Decimal(hold["frozen_amount"])
            last = index == len(holds) - 1
            if last:
                exec_part = quantize_money(executed_held - allocated_exec)
                release_part = quantize_money(released_held - allocated_release)
            else:
                share = frozen / total_held
                exec_part = quantize_money(executed_held * share)
                release_part = quantize_money(released_held * share)
                allocated_exec += exec_part
                allocated_release += release_part
            self._apply_hold_settlement(
                hold, seg, exec_part, release_part, reservation, actor_id
            )

    def _apply_hold_settlement(self, hold, seg, exec_part, release_part, reservation, actor_id) -> None:
        period = seg["period_key"]
        credit_id = hold["credit_id"]
        if exec_part > ZERO:
            self._require_period_open(reservation["tenant_id"], period)
            self._adjust_account(
                credit_id, frozen_delta=-exec_part, consumed_delta=exec_part,
                error="额度账户消耗超过冻结额",
            )
            self._update_hold(hold["hold_id"], frozen_delta=-exec_part, consumed_delta=exec_part)
            self._ledger(
                tenant_id=reservation["tenant_id"],
                entry_type="consume",
                amount=exec_part,
                created_by=actor_id,
                reason=f"{seg['period_key']} 实际消耗",
                reservation_id=reservation["reservation_id"],
                segment_id=seg["segment_id"],
                credit_id=credit_id,
                source_type=hold["source_type"],
                period=period,
                rule_revision=reservation["rule_revision"],
            )
        if release_part > ZERO:
            self._adjust_account(
                credit_id, frozen_delta=-release_part,
                error="额度账户返还超过冻结额",
            )
            self._update_hold(hold["hold_id"], frozen_delta=-release_part, released_delta=release_part)
            self._ledger(
                tenant_id=reservation["tenant_id"],
                entry_type="release",
                amount=release_part,
                created_by=actor_id,
                reason=f"{seg['period_key']} 返还未执行冻结",
                reservation_id=reservation["reservation_id"],
                segment_id=seg["segment_id"],
                credit_id=credit_id,
                source_type=hold["source_type"],
                period=period,
            )

    def _update_hold(
        self, hold_id: int, *, frozen_delta=ZERO, consumed_delta=ZERO, released_delta=ZERO,
    ) -> None:
        row = self.connection.execute(
            "SELECT frozen_amount,consumed_amount,released_amount FROM segment_holds WHERE hold_id=?",
            (hold_id,),
        ).fetchone()
        frozen = quantize_money(Decimal(row["frozen_amount"]) + frozen_delta)
        consumed = quantize_money(Decimal(row["consumed_amount"]) + consumed_delta)
        released = quantize_money(Decimal(row["released_amount"]) + released_delta)
        if frozen < ZERO or consumed < ZERO or released < ZERO:
            raise Conflict("冻结明细结算金额超过冻结额")
        self.connection.execute(
            "UPDATE segment_holds SET frozen_amount=?,consumed_amount=?,released_amount=? WHERE hold_id=?",
            (decimal_text(frozen), decimal_text(consumed), decimal_text(released), hold_id),
        )

    def cancel_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        return self._settle_or_release(reservation_id, None, actor_id, failed=False)

    def fail_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        return self._settle_or_release(reservation_id, None, actor_id, failed=True)

    def complete_reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        return self._settle_or_release(reservation_id, parse_utc(
            self._locked_reservation(reservation_id)["ends_at"]), actor_id, failed=False, completed=True)

    # ------------------------------------------------------------- 账期结算
    def close_period(self, actor_id: str, tenant_id: str, period: str) -> dict[str, Any]:
        self._require(actor_id, "period.close")
        self._tenant(tenant_id)
        with transaction(self.connection, immediate=True):
            self._expire_due_reviews()
            self._require_period_open(tenant_id, period)
            # 该账期存在仍在运行/待确认的预约时不允许关账。
            open_segments = self.connection.execute(
                "SELECT 1 FROM reservation_segments s JOIN reservations r ON r.reservation_id=s.reservation_id "
                "WHERE s.period_key=? AND r.tenant_id=? AND r.state IN "
                "('pending_review','confirmed','running') LIMIT 1",
                (period, tenant_id),
            ).fetchone()
            if open_segments is not None:
                raise InvalidState(f"账期 {period} 仍有未结清预约")
            self.connection.execute(
                "INSERT INTO accounting_periods(tenant_id,period_key,state,closed_by,closed_at) "
                "VALUES(?,?,'closed',?,?) ON CONFLICT(tenant_id,period_key) DO UPDATE SET "
                "state='closed',closed_by=excluded.closed_by,closed_at=excluded.closed_at "
                "WHERE accounting_periods.state='open'",
                (tenant_id, period, actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE credit_ledger SET settled=1 WHERE tenant_id=? AND period_key=? AND settled=0",
                (tenant_id, period),
            )
            self._audit("period", f"{tenant_id}:{period}", "period.closed", actor_id, {})
        return {"tenant_id": tenant_id, "period_key": period, "state": "closed"}

    # ------------------------------------------------------------- 查询视图
    def _scoped_tenant(self, user: sqlite3.Row, tenant_id: str | None) -> str:
        if user["role"] == "tenant":
            if tenant_id is not None and tenant_id != user["tenant_id"]:
                raise Forbidden("只能访问本机构数据")
            return user["tenant_id"]
        if not tenant_id:
            raise ValidationFailed("内部角色查询必须指定 tenant_id")
        self._tenant(tenant_id)
        return tenant_id

    def credit_summary(self, actor_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        return self._credit_summary_impl(actor_id, tenant_id)

    def _credit_summary_impl(self, actor_id: str, tenant_id: str | None) -> dict[str, Any]:
        user = self._user(actor_id)
        permission = "credit.read_self" if user["role"] == "tenant" else "credit.read_all"
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权查询额度")
        scope = self._scoped_tenant(user, tenant_id)
        rows = self.connection.execute(
            "SELECT * FROM credit_accounts WHERE tenant_id=? ORDER BY source_type,credit_id",
            (scope,),
        ).fetchall()
        by_source = {source: {"total": ZERO, "frozen": ZERO, "consumed": ZERO, "available": ZERO}
                     for source in SOURCE_TYPES}
        accounts = []
        for row in rows:
            total = Decimal(row["amount_total"])
            frozen = Decimal(row["frozen_amount"])
            consumed = Decimal(row["consumed_amount"])
            available = total - frozen - consumed
            bucket = by_source[row["source_type"]]
            bucket["total"] += total
            bucket["frozen"] += frozen
            bucket["consumed"] += consumed
            bucket["available"] += available
            accounts.append({
                "credit_id": row["credit_id"],
                "source_type": row["source_type"],
                "state": row["state"],
                "total": decimal_text(total),
                "frozen": decimal_text(frozen),
                "consumed": decimal_text(consumed),
                "available": decimal_text(available),
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "products": json.loads(row["products_json"]),
            })
        return {
            "tenant_id": scope,
            "as_of": self._now(),
            "sources": {key: {k: decimal_text(quantize_money(v)) for k, v in value.items()}
                        for key, value in sorted(by_source.items())},
            "accounts": accounts,
        }

    def ledger(
        self, actor_id: str, tenant_id: str | None = None,
        reservation_id: str | None = None, limit: int = 100,
    ) -> dict[str, Any]:
        user = self._user(actor_id)
        permission = "ledger.read_self" if user["role"] == "tenant" else "ledger.read_all"
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权查询流水")
        if user["role"] == "tenant":
            scope = user["tenant_id"]
            if tenant_id is not None and tenant_id != scope:
                raise Forbidden("只能访问本机构流水")
        else:
            if not tenant_id:
                raise ValidationFailed("内部角色查询必须指定 tenant_id")
            scope = tenant_id
            self._tenant(scope)
        sql = ("SELECT ledger_id,tenant_id,reservation_id,segment_id,credit_id,entry_type,"
               "source_type,amount,period_key,settled,rule_revision,reason,created_by,created_at "
               "FROM credit_ledger WHERE tenant_id=?")
        params: list[Any] = [scope]
        if reservation_id:
            sql += " AND reservation_id=?"
            params.append(reservation_id)
        sql += " ORDER BY ledger_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self.connection.execute(sql, params).fetchall()
        return {"tenant_id": scope, "entries": [dict(row) for row in rows]}

    def reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        if user["role"] == "tenant":
            if "reservation.read_self" not in ROLE_PERMISSIONS[user["role"]] or user["tenant_id"] != row["tenant_id"]:
                raise Forbidden("只能访问本机构预约")
        result = dict(row)
        result["segments"] = [
            dict(s) for s in self.connection.execute(
                "SELECT segment_id,period_key,seg_index,hours,amount,frozen_amount,consumed_amount,"
                "released_amount,overage_amount FROM reservation_segments WHERE reservation_id=? "
                "ORDER BY seg_index", (reservation_id,)
            ).fetchall()
        ]
        return result

    def list_reviews(self, actor_id: str, *, include_all: bool = False) -> dict[str, Any]:
        user = self._require(actor_id, "review.read_all" if include_all else "review.read_pending")
        with transaction(self.connection, immediate=True):
            self._expire_due_reviews()
        if include_all:
            rows = self.connection.execute(
                "SELECT * FROM overage_reviews ORDER BY review_id DESC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM overage_reviews WHERE state='pending' ORDER BY expires_at,review_id"
            ).fetchall()
        return {"reviews": [dict(row) for row in rows]}

    def explain_ledger_entry(self, actor_id: str, ledger_id: int) -> dict[str, Any]:
        """财务/审计解释每次扣减、返还与超额决定的完整上下文。"""
        self._require(actor_id, "ledger.read_all")
        entry = self.connection.execute(
            "SELECT * FROM credit_ledger WHERE ledger_id=?", (ledger_id,)
        ).fetchone()
        if entry is None:
            raise NotFound("流水不存在")
        result: dict[str, Any] = {"entry": dict(entry)}
        if entry["credit_id"]:
            result["credit_account"] = self.credit_account(entry["credit_id"])
        if entry["segment_id"]:
            seg = self.connection.execute(
                "SELECT * FROM reservation_segments WHERE segment_id=?", (entry["segment_id"],)
            ).fetchone()
            result["segment"] = dict(seg) if seg else None
            result["holds"] = [
                dict(h) for h in self.connection.execute(
                    "SELECT * FROM segment_holds WHERE segment_id=? ORDER BY priority_rank,hold_id",
                    (entry["segment_id"],),
                ).fetchall()
            ]
        if entry["reservation_id"]:
            result["reservation"] = dict(self.connection.execute(
                "SELECT * FROM reservations WHERE reservation_id=?",
                (entry["reservation_id"],),
            ).fetchone())
            review = self.connection.execute(
                "SELECT * FROM overage_reviews WHERE reservation_id=?",
                (entry["reservation_id"],),
            ).fetchone()
            result["overage_review"] = dict(review) if review else None
        if entry["rule_revision"] is not None:
            rule = self.connection.execute(
                "SELECT rule_id,tenant_id,revision,source_priority_json,allow_overage,"
                "effective_from,state FROM credit_rules "
                "WHERE revision=? AND (tenant_id=? OR tenant_id IS NULL) "
                "ORDER BY tenant_id IS NULL LIMIT 1",
                (entry["rule_revision"], entry["tenant_id"]),
            ).fetchone()
            result["rule"] = dict(rule) if rule else None
        return result

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
