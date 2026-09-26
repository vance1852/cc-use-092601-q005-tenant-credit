from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from tenant_credit.api import JsonApplication
from tenant_credit.clock import FrozenClock
from tenant_credit.errors import Conflict, Forbidden, InsufficientCredit, InvalidState, ValidationFailed
from tenant_credit.ledger import attribute_executed, split_by_month
from tenant_credit.service import CreditService


class SplitTests(unittest.TestCase):
    def test_split_by_month_keeps_exact_total(self) -> None:
        slices = split_by_month(
            datetime(2026, 9, 28, tzinfo=timezone.utc),
            datetime(2026, 10, 2, tzinfo=timezone.utc),
            Decimal("1200"),
        )
        self.assertEqual([item.period for item in slices], ["2026-09", "2026-10"])
        self.assertEqual([item.amount for item in slices], [Decimal("900.000"), Decimal("300.000")])
        self.assertEqual(sum(item.amount for item in slices), Decimal("1200.000"))

    def test_split_remainder_lands_in_last_slice(self) -> None:
        slices = split_by_month(
            datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
            Decimal("0.002"),
        )
        self.assertEqual(len(slices), 3)
        self.assertEqual(sum(item.amount for item in slices), Decimal("0.002"))

    def test_attribute_executed_fills_chronologically(self) -> None:
        result = attribute_executed([Decimal("900"), Decimal("300")], Decimal("950"))
        self.assertEqual(result, [Decimal("900"), Decimal("50")])
        with self.assertRaises(ValueError):
            attribute_executed([Decimal("10")], Decimal("11"))


class CreditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CreditService(self.connection, self.clock)
        self.service.create_org("org-cas", "科研机构", "research")
        self.service.create_org("org-abc", "企业客户", "enterprise")
        self.service.create_user("tenant-cas", "租户甲", "tenant_admin", "org-cas")
        self.service.create_user("tenant-abc", "租户乙", "tenant_admin", "org-abc")
        self.service.create_user("sched", "调度", "scheduler")
        self.service.create_user("fin", "财务", "finance")
        self.service.create_user("rev-1", "复核甲", "reviewer")
        self.service.create_user("rev-2", "复核乙", "reviewer")
        self.service.create_user("audit", "审计", "auditor")
        self.service.publish_rule("fin", {"source_priority": ["GRANT", "PREPAID", "POSTPAID"], "review_ttl_hours": 48, "note": "默认规则"})
        self.service.open_credit_line("fin", {"line_id": "line-grant", "org_id": "org-cas", "source": "GRANT", "resource_kind": "gpu-h100", "total_amount": "1000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "idempotency_key": "lk-1"})
        self.service.open_credit_line("fin", {"line_id": "line-prepaid", "org_id": "org-cas", "source": "PREPAID", "resource_kind": "ANY", "total_amount": "5000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-11-01T00:00:00Z", "idempotency_key": "lk-2"})
        self.service.open_credit_line("fin", {"line_id": "line-postpaid", "org_id": "org-cas", "source": "POSTPAID", "resource_kind": "gpu-h100", "total_amount": "2000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "idempotency_key": "lk-3"})

    def tearDown(self) -> None:
        self.connection.close()

    def reservation_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "reservation_id": "res-1",
            "org_id": "org-cas",
            "resource_kind": "gpu-h100",
            "starts_at": "2026-09-28T00:00:00Z",
            "ends_at": "2026-10-02T00:00:00Z",
            "estimated_amount": "1200",
            "idempotency_key": "rk-1",
        }
        payload.update(overrides)
        return payload

    def test_cross_month_confirm_freezes_by_rule_atomically(self) -> None:
        result = self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual([item["estimated_amount"] for item in result["slices"]], ["900.000", "300.000"])
        self.assertEqual(
            result["allocations"],
            [
                {"line_id": "line-grant", "period": "2026-09", "amount": "900.000"},
                {"line_id": "line-prepaid", "period": "2026-10", "amount": "300.000"},
            ],
        )
        grant = self.service.credit_line("fin", "line-grant")
        self.assertEqual(grant["frozen_amount"], "900.000")
        self.assertEqual(grant["available_amount"], "100.000")
        entries = self.connection.execute(
            "SELECT entry_type,period,amount FROM credit_ledger WHERE reservation_id='res-1' ORDER BY entry_id"
        ).fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in entries], [("freeze", "2026-09", "900.000"), ("freeze", "2026-10", "300.000")])

    def test_confirm_rolls_back_when_any_slice_unfunded(self) -> None:
        payload = self.reservation_payload(estimated_amount="9600")
        with self.assertRaises(InsufficientCredit):
            self.service.confirm_reservation("tenant-cas", payload)
        count = self.connection.execute("SELECT count(*) FROM reservations").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
        self.assertEqual(self.service.credit_line("fin", "line-prepaid")["frozen_amount"], "0")

    def test_complete_consumes_executed_and_refunds_remainder(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        result = self.service.complete_reservation("sched", "res-1", "800")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["refunded_amount"], "400.000")
        grant = self.service.credit_line("fin", "line-grant")
        self.assertEqual(grant["consumed_amount"], "800.000")
        self.assertEqual(grant["frozen_amount"], "0.000")
        prepaid = self.service.credit_line("fin", "line-prepaid")
        self.assertEqual(prepaid["available_amount"], "5000.000")
        types = [row[0] for row in self.connection.execute(
            "SELECT entry_type FROM credit_ledger WHERE reservation_id='res-1' ORDER BY entry_id"
        ).fetchall()]
        self.assertEqual(types, ["freeze", "freeze", "consume", "refund", "refund"])

    def test_cancel_and_fail_refund_only_unexecuted(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        cancelled = self.service.cancel_reservation("sched", "res-1", "500")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["executed_amount"], "500.000")
        self.assertEqual(cancelled["refunded_amount"], "700.000")
        self.service.confirm_reservation("tenant-cas", self.reservation_payload(reservation_id="res-2", idempotency_key="rk-2"))
        failed = self.service.fail_reservation("sched", "res-2", "0")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["refunded_amount"], "1200.000")
        with self.assertRaises(InvalidState):
            self.service.complete_reservation("sched", "res-2", "1")

    def test_executed_cannot_exceed_estimate(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        with self.assertRaises(ValidationFailed):
            self.service.complete_reservation("sched", "res-1", "1201")

    def test_over_quota_enters_time_limited_review_queue(self) -> None:
        payload = self.reservation_payload(estimated_amount="9600")
        with self.assertRaises(InsufficientCredit):
            self.service.confirm_reservation("tenant-cas", payload)
        request = self.service.request_exemption("tenant-cas", {
            "request_id": "ex-1", "org_id": "org-cas", "resource_kind": "gpu-h100",
            "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z",
            "requested_amount": "9600", "reason": "关键窗口", "idempotency_key": "ek-1",
        })
        self.assertEqual(request["state"], "pending")
        self.assertEqual(request["expires_at"], "2026-09-26T08:00:00Z")
        self.clock.advance(hours=49)
        with self.assertRaises(InvalidState):
            self.service.decide_exemption("rev-1", "ex-1", True, "已超期")
        listed = self.service.list_exemptions("rev-1")
        self.assertEqual(listed["exemptions"][0]["state"], "expired")

    def test_reviewer_cannot_approve_own_request(self) -> None:
        self.service.request_exemption("rev-1", {
            "request_id": "ex-self", "org_id": "org-cas", "resource_kind": "gpu-h100",
            "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z",
            "requested_amount": "9600", "reason": "平台代提", "idempotency_key": "ek-self",
        })
        with self.assertRaises(Forbidden):
            self.service.decide_exemption("rev-1", "ex-self", True, "自己批自己")
        decided = self.service.decide_exemption("rev-2", "ex-self", True, "他人复核通过")
        self.assertEqual(decided["state"], "approved")

    def test_approved_exemption_allows_over_quota_confirm_once(self) -> None:
        self.service.request_exemption("tenant-cas", {
            "request_id": "ex-2", "org_id": "org-cas", "resource_kind": "gpu-h100",
            "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z",
            "requested_amount": "9600", "reason": "关键窗口", "idempotency_key": "ek-2",
        })
        self.service.decide_exemption("rev-1", "ex-2", True, "同意")
        confirmed = self.service.confirm_reservation("tenant-cas", self.reservation_payload(estimated_amount="9600", exemption_id="ex-2"))
        self.assertEqual(confirmed["exempted_amount"], "1600.000")
        with self.assertRaises(InvalidState):
            self.service.confirm_reservation("tenant-cas", self.reservation_payload(reservation_id="res-3", idempotency_key="rk-3", estimated_amount="9600", exemption_id="ex-2"))

    def test_sufficient_credit_needs_no_exemption(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.request_exemption("tenant-cas", {
                "request_id": "ex-3", "org_id": "org-cas", "resource_kind": "gpu-h100",
                "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z",
                "requested_amount": "100", "reason": "额度充足", "idempotency_key": "ek-3",
            })

    def test_rule_update_does_not_rewrite_settled_period(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        self.service.complete_reservation("sched", "res-1", "800")
        self.clock.advance(days=40)
        settled = self.service.settle_period("fin", "org-cas", "2026-09")
        self.assertEqual(settled["total_consumed"], "800.000")
        self.assertEqual(settled["rule_version"], 1)
        self.service.publish_rule("fin", {"source_priority": ["POSTPAID", "PREPAID", "GRANT"], "review_ttl_hours": 24, "note": "新规则"})
        after = self.service.billing_period("fin", "org-cas", "2026-09")
        self.assertEqual(after["rule_version"], 1)
        self.assertEqual(after["total_consumed"], "800.000")
        with self.assertRaises(Conflict):
            self.service.settle_period("fin", "org-cas", "2026-09")
        with self.assertRaises(InvalidState):
            self.service._ensure_period_open("org-cas", "2026-09")

    def test_settle_requires_ended_period_and_no_open_freeze(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.settle_period("fin", "org-cas", "2026-09")
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        self.clock.advance(days=40)
        with self.assertRaises(InvalidState):
            self.service.settle_period("fin", "org-cas", "2026-09")

    def test_tenant_sees_only_own_org(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        summary = self.service.org_summary("tenant-cas", "org-cas")
        self.assertEqual(summary["sources"]["GRANT"]["frozen"], "900.000")
        self.assertEqual(len(self.service.org_ledger("tenant-cas", "org-cas")["entries"]), 2)
        with self.assertRaises(Forbidden):
            self.service.org_summary("tenant-cas", "org-abc")
        with self.assertRaises(Forbidden):
            self.service.org_ledger("tenant-cas", "org-abc")
        with self.assertRaises(Forbidden):
            self.service.confirm_reservation("tenant-cas", self.reservation_payload(reservation_id="res-9", idempotency_key="rk-9", org_id="org-abc"))
        finance_view = self.service.org_summary("fin", "org-abc")
        self.assertEqual(finance_view["org_id"], "org-abc")

    def test_explain_trail_covers_deduction_refund_and_exemption(self) -> None:
        self.service.request_exemption("tenant-cas", {
            "request_id": "ex-4", "org_id": "org-cas", "resource_kind": "gpu-h100",
            "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z",
            "requested_amount": "9600", "reason": "关键窗口", "idempotency_key": "ek-4",
        })
        self.service.decide_exemption("rev-1", "ex-4", True, "同意")
        confirmed = self.service.confirm_reservation("tenant-cas", self.reservation_payload(estimated_amount="9600", exemption_id="ex-4"))
        self.assertEqual(confirmed["exempted_amount"], "1600.000")
        self.service.complete_reservation("sched", "res-1", "7500")
        explained = self.service.explain_reservation("audit", "res-1")
        self.assertEqual(explained["exemption"]["decision_note"], "同意")
        self.assertEqual(explained["exemption"]["decided_by"], "rev-1")
        types = [entry["entry_type"] for entry in explained["ledger"]]
        self.assertEqual(types, ["freeze", "freeze", "freeze", "freeze", "consume", "consume", "consume", "consume", "refund"])
        self.assertTrue(all(entry["reason"] for entry in explained["ledger"]))
        self.assertEqual(explained["rule"]["rule_version"], 1)
        line_trail = self.service.explain_line("fin", "line-prepaid")
        self.assertEqual([entry["entry_type"] for entry in line_trail["ledger"]], ["freeze", "freeze", "consume", "consume", "refund"])

    def test_idempotent_replay_and_conflict(self) -> None:
        payload = self.reservation_payload()
        first = self.service.confirm_reservation("tenant-cas", payload)
        second = self.service.confirm_reservation("tenant-cas", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.confirm_reservation("tenant-cas", self.reservation_payload(estimated_amount="1300"))
        line_payload = {"line_id": "line-x", "org_id": "org-cas", "source": "GRANT", "resource_kind": "gpu-a100", "total_amount": "10", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "idempotency_key": "lk-x"}
        self.assertEqual(self.service.open_credit_line("fin", line_payload), self.service.open_credit_line("fin", line_payload))
        with self.assertRaises(Conflict):
            self.service.open_credit_line("fin", {**line_payload, "total_amount": "11"})

    def test_expiry_writes_off_available_but_keeps_freeze(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        self.clock.advance(days=8)
        result = self.service.expire_credit_lines("fin")
        written = {item["line_id"]: item["written_off"] for item in result["expired"]}
        self.assertEqual(written["line-grant"], "100.000")
        self.assertEqual(written["line-postpaid"], "2000.000")
        self.assertNotIn("line-prepaid", written)
        grant = self.service.credit_line("fin", "line-grant")
        self.assertEqual(grant["state"], "expired")
        self.assertEqual(grant["frozen_amount"], "900.000")
        closed = self.service.complete_reservation("sched", "res-1", "800")
        self.assertEqual(closed["refunded_amount"], "400.000")

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.confirm_reservation("tenant-cas", self.reservation_payload())
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE credit_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/orgs/org-cas/summary")
        self.assertEqual(missing_actor.status, 422)
        response = app.handle(
            "GET", "/orgs/org-abc/summary", {"X-Actor-Id": "tenant-cas"}
        )
        self.assertEqual(response.status, 403)
        body = json.dumps({"org_id": "org-cas", "resource_kind": "gpu-h100", "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z", "estimated_amount": "9600", "reservation_id": "res-api", "idempotency_key": "rk-api"}).encode()
        created = app.handle("POST", "/reservations", {"X-Actor-Id": "tenant-cas"}, body)
        self.assertEqual(created.status, 409)
        self.assertEqual(created.body["error"]["code"], "insufficient_credit")


if __name__ == "__main__":
    unittest.main()
