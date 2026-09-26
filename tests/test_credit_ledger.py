from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from credit_ledger.api import JsonApplication
from credit_ledger.clock import FrozenClock
from credit_ledger.domain import select_credits, split_by_month
from credit_ledger.errors import Conflict, Forbidden, InvalidState
from credit_ledger.service import CreditService


UTC = timezone.utc


class DomainTests(unittest.TestCase):
    def test_split_single_month_keeps_total(self) -> None:
        segments = split_by_month(
            datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC),
            Decimal("24"), Decimal("10"),
        )
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].period_key, "2026-09")
        self.assertEqual(sum((s.amount for s in segments), Decimal("0")), Decimal("240.00"))

    def test_split_cross_month_partitions_by_time(self) -> None:
        segments = split_by_month(
            datetime(2026, 9, 30, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC),
            Decimal("48"), Decimal("10"),
        )
        self.assertEqual([s.period_key for s in segments], ["2026-09", "2026-10"])
        self.assertEqual(sum((s.hours for s in segments), Decimal("0")), Decimal("48.000"))
        self.assertEqual(sum((s.amount for s in segments), Decimal("0")), Decimal("480.00"))

    def test_select_credits_follows_priority_and_expiry(self) -> None:
        accounts = [
            {"credit_id": "post1", "source_type": "postpaid", "products": [],
             "available": Decimal("1000"), "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
             "valid_to": None},
            {"credit_id": "grant-late", "source_type": "grant", "products": [],
             "available": Decimal("100"), "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
             "valid_to": datetime(2026, 12, 1, tzinfo=UTC)},
            {"credit_id": "grant-soon", "source_type": "grant", "products": [],
             "available": Decimal("100"), "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
             "valid_to": datetime(2026, 10, 1, tzinfo=UTC)},
        ]
        allocations = select_credits(
            accounts, Decimal("250"), "gpu-h100",
            ["prepaid", "grant", "postpaid"], datetime(2026, 9, 20, tzinfo=UTC),
        )
        # grant 先到期者先用，再用 postpaid。
        self.assertEqual([a.credit_id for a in allocations], ["grant-soon", "grant-late", "post1"])
        self.assertEqual(allocations[-1].amount, Decimal("50"))

    def test_select_credits_filters_product_and_validity(self) -> None:
        accounts = [
            {"credit_id": "g1", "source_type": "grant", "products": ["gpu-a100"],
             "available": Decimal("100"), "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
             "valid_to": None},
            {"credit_id": "expired", "source_type": "prepaid", "products": [],
             "available": Decimal("100"), "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
             "valid_to": datetime(2026, 8, 1, tzinfo=UTC)},
        ]
        allocations = select_credits(
            accounts, Decimal("80"), "gpu-h100",
            ["prepaid", "grant", "postpaid"], datetime(2026, 9, 1, tzinfo=UTC),
        )
        self.assertEqual(allocations, [])


class CreditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, tzinfo=UTC))
        self.service = CreditService(self.connection, self.clock)
        self.connection.executescript(
            """
            INSERT INTO credit_tenants VALUES('lab','研究院',1,'t'),('acme','企业',1,'t');
            INSERT INTO credit_users(user_id,display_name,role,tenant_id,active,created_at) VALUES
              ('fin','财务','finance',NULL,1,'t'),
              ('rev1','复核甲','reviewer',NULL,1,'t'),
              ('rev2','复核乙','reviewer',NULL,1,'t'),
              ('aud','审计','auditor',NULL,1,'t'),
              ('labu','研究院账号','tenant','lab',1,'t'),
              ('acmeu','企业账号','tenant','acme',1,'t');
            """
        )
        self.service.create_resource_pool("fin", {
            "pool_id": "h100", "facility_id": "c-a", "product": "gpu-h100",
            "available_hours": "1000", "unit_price_cny": "10"})
        self.service.put_rule("fin", None, ["prepaid", "grant", "postpaid"], False)

    def tearDown(self) -> None:
        self.connection.close()

    def _grant(self, credit_id: str, tenant: str, source: str, amount: str, **kw) -> None:
        payload = {
            "credit_id": credit_id, "tenant_id": tenant, "source_type": source,
            "amount": amount, "products": kw.get("products", ["gpu-h100"]),
            "valid_from": kw.get("valid_from", "2026-09-01T00:00:00Z"),
            "valid_to": kw.get("valid_to", "2026-12-31T23:59:59Z"),
        }
        if payload["valid_to"] is None:
            del payload["valid_to"]
        self.service.grant_credit("fin", payload)

    def _reservation(self, reservation_id: str, tenant="lab", hours="10", *,
                     start="2026-09-25T00:00:00Z", end="2026-09-25T10:00:00Z",
                     key=None, pool="h100", reason="x"):
        return self.service.submit_reservation("labu" if tenant == "lab" else "acmeu", {
            "reservation_id": reservation_id, "tenant_id": tenant, "pool_id": pool,
            "requested_hours": hours, "starts_at": start, "ends_at": end,
            "idempotency_key": key or reservation_id, "reason": reason})

    def test_sources_are_separate_and_summary_groups_them(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        self._grant("g", "lab", "grant", "50")
        summary = self.service.credit_summary("labu")
        self.assertEqual(summary["sources"]["prepaid"]["available"], "100.00")
        self.assertEqual(summary["sources"]["grant"]["available"], "50.00")
        self.assertEqual(summary["sources"]["postpaid"]["available"], "0.00")

    def test_confirm_freezes_credit_and_reserves_resource_atomically(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        result = self._reservation("job-1", hours="5")
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["frozen_amount"], "50.00")
        # 资源工时同步扣减。
        self.assertEqual(self.service.resource_pool("h100")["available_hours"], "995.000")
        account = self.service.credit_account("p")
        self.assertEqual(account["frozen_amount"], "50.00")

    def test_resource_shortfall_rolls_back_credit_freeze(self) -> None:
        self._grant("p", "lab", "prepaid", "100000")
        with self.assertRaises(Conflict):
            self._reservation("job-big", hours="2000")
        # 回滚后额度未被冻结。
        self.assertEqual(self.service.credit_account("p")["frozen_amount"], "0.00")
        self.assertEqual(self.service.resource_pool("h100")["available_hours"], "1000.000")

    def test_cancel_releases_only_unexecuted_part(self) -> None:
        self._grant("p", "lab", "prepaid", "200")
        self._reservation("job-c", hours="10", start="2026-09-25T00:00:00Z",
                          end="2026-09-25T10:00:00Z")
        self.service.start_reservation("labu", "job-c")
        # 执行 4 小时后取消（40% 已执行）。
        self.clock.current = datetime(2026, 9, 25, 4, tzinfo=UTC)
        result = self.service.cancel_reservation("labu", "job-c")
        self.assertEqual(result["executed_amount"], "40.00")
        self.assertEqual(result["released_amount"], "60.00")
        self.assertEqual(result["released_hours"], "6.000")
        account = self.service.credit_account("p")
        self.assertEqual(account["consumed_amount"], "40.00")
        self.assertEqual(account["frozen_amount"], "0.00")
        # 资源只返还未执行部分。
        self.assertEqual(self.service.resource_pool("h100")["available_hours"], "996.000")

    def test_failure_only_refunds_unexecuted(self) -> None:
        self._grant("p", "lab", "prepaid", "240")
        self._reservation("job-f", hours="24", start="2026-10-10T00:00:00Z",
                          end="2026-10-11T00:00:00Z")
        self.service.start_reservation("labu", "job-f")
        self.clock.current = datetime(2026, 10, 10, 6, tzinfo=UTC)
        result = self.service.fail_reservation("labu", "job-f")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["released_amount"], "180.00")
        self.assertEqual(result["executed_amount"], "60.00")

    def test_overage_enters_review_and_segregation_of_duties(self) -> None:
        result = self._reservation("job-o", tenant="acme", hours="50")
        self.assertEqual(result["state"], "pending_review")
        self.assertEqual(result["shortfall_amount"], "500.00")
        # 未确认前不占用资源。
        self.assertEqual(self.service.resource_pool("h100")["available_hours"], "1000.000")
        review_id = result["review_id"]
        # 提交人是 acmeu；让 rev1 成为提交人后禁止自批。
        self.connection.execute(
            "UPDATE overage_reviews SET submitted_by='rev1' WHERE review_id=?", (review_id,))
        with self.assertRaises(Forbidden):
            self.service.decide_review("rev1", review_id, True)
        self.connection.execute(
            "UPDATE overage_reviews SET submitted_by='acmeu' WHERE review_id=?", (review_id,))
        decision = self.service.decide_review("rev2", review_id, True)
        self.assertEqual(decision["state"], "confirmed")

    def test_review_rejection_releases_nothing_and_marks_rejected(self) -> None:
        result = self._reservation("job-r", tenant="acme", hours="10")
        decision = self.service.decide_review("rev1", result["review_id"], False, "拒绝")
        self.assertEqual(decision["state"], "rejected")
        self.assertEqual(self.service.resource_pool("h100")["available_hours"], "1000.000")

    def test_review_expires_after_ttl(self) -> None:
        result = self._reservation("job-e", tenant="acme", hours="10",
                                   start="2026-10-20T00:00:00Z", end="2026-10-20T10:00:00Z")
        review_id = result["review_id"]
        self.clock.current += timedelta(hours=49)
        self.service._expire_due_reviews()
        reviews = self.service.list_reviews("rev1", include_all=True)["reviews"]
        row = next(item for item in reviews if item["review_id"] == review_id)
        self.assertEqual(row["state"], "expired")
        self.assertEqual(self.service.reservation("aud", "job-e")["state"], "rejected")

    def test_closed_period_rejects_new_writes_but_keeps_history(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        self._reservation("job-sep", hours="2", start="2026-09-25T00:00:00Z",
                          end="2026-09-25T02:00:00Z")
        self.service.start_reservation("labu", "job-sep")
        self.clock.current = datetime(2026, 9, 25, 2, tzinfo=UTC)
        self.service.complete_reservation("labu", "job-sep")
        self.service.close_period("fin", "lab", "2026-09")
        with self.assertRaises(InvalidState):
            self._reservation("job-tamper", hours="1", start="2026-09-28T00:00:00Z",
                              end="2026-09-28T01:00:00Z")
        # 已结算流水保留并被标记 settled。
        settled = self.connection.execute(
            "SELECT DISTINCT settled FROM credit_ledger WHERE period_key='2026-09'").fetchall()
        self.assertEqual({row["settled"] for row in settled}, {1})

    def test_rule_update_does_not_rewrite_settled_reservations(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        self._reservation("job-rule", hours="1")
        original = self.service.reservation("aud", "job-rule")["rule_revision"]
        self.service.put_rule("fin", None, ["grant", "prepaid", "postpaid"], True)
        self.assertEqual(
            self.service.reservation("aud", "job-rule")["rule_revision"], original)

    def test_allow_overage_confirms_with_shortfall(self) -> None:
        self.service.put_rule("fin", None, ["prepaid", "grant", "postpaid"], True)
        self._grant("p", "lab", "prepaid", "60")
        result = self._reservation("job-auto", hours="10")
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["overage_amount"], "40.00")
        self.assertEqual(result["covered_amount"], "60.00")

    def test_failure_executes_overage_as_postpaid_and_releases_rest(self) -> None:
        self.service.put_rule("fin", None, ["prepaid", "grant", "postpaid"], True)
        self._grant("p", "lab", "prepaid", "100")
        # 200 元作业：100 预付 + 100 超额。
        self._reservation("job-ov", hours="20",
                          start="2026-09-25T00:00:00Z", end="2026-09-25T20:00:00Z")
        self.service.start_reservation("labu", "job-ov")
        # 执行四分之一（5 小时）后失败。
        self.clock.current = datetime(2026, 9, 25, 5, tzinfo=UTC)
        result = self.service.fail_reservation("labu", "job-ov")
        self.assertEqual(result["executed_amount"], "50.00")
        self.assertEqual(result["released_amount"], "150.00")
        rows = self.connection.execute(
            "SELECT entry_type,source_type,amount FROM credit_ledger "
            "WHERE reservation_id='job-ov' ORDER BY ledger_id").fetchall()
        consume = {row["source_type"]: Decimal(row["amount"])
                   for row in rows if row["entry_type"] == "consume"}
        self.assertEqual(consume, {"prepaid": Decimal("25.00"), "postpaid": Decimal("25.00")})
        # 预付账户只消耗已执行的 25，冻结全部清零。
        account = self.service.credit_account("p")
        self.assertEqual(account["consumed_amount"], "25.00")
        self.assertEqual(account["frozen_amount"], "0.00")

    def test_cross_month_spans_two_periods_and_closes_independently(self) -> None:
        self._grant("p", "lab", "prepaid", "1000")
        result = self._reservation(
            "job-x", hours="48",
            start="2026-09-30T00:00:00Z", end="2026-10-02T00:00:00Z")
        self.assertEqual(
            [seg["period_key"] for seg in result["segments"]], ["2026-09", "2026-10"])
        segments = self.service.reservation("aud", "job-x")["segments"]
        self.assertEqual(len(segments), 2)
        # 作业仍在进行时不能关闭 9 月账期。
        self.service.start_reservation("labu", "job-x")
        with self.assertRaises(InvalidState):
            self.service.close_period("fin", "lab", "2026-09")

    def test_tenant_isolation_and_internal_scoping(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        self._grant("q", "acme", "prepaid", "77")
        self._reservation("job-iso", hours="2")
        with self.assertRaises(Forbidden):
            self.service.credit_summary("labu", "acme")
        with self.assertRaises(Forbidden):
            self.service.ledger("labu", tenant_id="acme")
        # 租户默认只见本机构；审计可指定任意机构。
        self.assertEqual(self.service.credit_summary("labu")["tenant_id"], "lab")
        self.assertEqual(self.service.credit_summary("aud", "acme")["tenant_id"], "acme")
        # 审计可解释流水，租户不能调用解释接口。
        ledger_id = self.connection.execute(
            "SELECT ledger_id FROM credit_ledger WHERE tenant_id='lab' "
            "AND entry_type='freeze' LIMIT 1").fetchone()["ledger_id"]
        explanation = self.service.explain_ledger_entry("aud", ledger_id)
        self.assertIn("holds", explanation)
        with self.assertRaises(Forbidden):
            self.service.explain_ledger_entry("labu", ledger_id)

    def test_idempotent_replay_conflicts_on_changed_payload(self) -> None:
        self._grant("p", "lab", "prepaid", "1000")
        payload = {
            "reservation_id": "job-idem", "tenant_id": "lab", "pool_id": "h100",
            "requested_hours": "5", "starts_at": "2026-09-25T00:00:00Z",
            "ends_at": "2026-09-25T05:00:00Z", "idempotency_key": "same-key", "reason": "x"}
        first = self.service.submit_reservation("labu", payload)
        second = self.service.submit_reservation("labu", dict(payload))
        self.assertEqual(first, second)
        changed = dict(payload, requested_hours="6")
        with self.assertRaises(Conflict):
            self.service.submit_reservation("labu", changed)

    def test_audit_chain_detects_tampering(self) -> None:
        self._grant("p", "lab", "prepaid", "100")
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE credit_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])


class CreditApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, tzinfo=UTC))
        self.service = CreditService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        self.connection.executescript(
            """
            INSERT INTO credit_tenants VALUES('lab','研究院',1,'t');
            INSERT INTO credit_users VALUES('fin','财务','finance',NULL,1,'t'),
              ('labu','租户','tenant','lab',1,'t'),('aud','审计','auditor',NULL,1,'t');
            """
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_unknown_route(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "fin"})
        self.assertEqual(response.status, 404)

    def test_actor_required(self) -> None:
        response = self.app.handle("GET", "/credits/summary")
        self.assertEqual(response.status, 422)

    def test_end_to_end_reservation_and_tenant_view(self) -> None:
        self.app.handle("POST", "/pools", {"X-Actor-Id": "fin"}, b'{"pool_id":"h100",'
                        b'"facility_id":"c","product":"gpu-h100","available_hours":"100",'
                        b'"unit_price_cny":"10"}')
        self.app.handle("POST", "/rules", {"X-Actor-Id": "fin"},
                        b'{"source_priority":["prepaid","grant","postpaid"]}')
        self.app.handle("POST", "/credits", {"X-Actor-Id": "fin"},
                        b'{"credit_id":"p1","tenant_id":"lab","source_type":"prepaid",'
                        b'"amount":"100","products":["gpu-h100"],'
                        b'"valid_from":"2026-09-01T00:00:00Z",'
                        b'"valid_to":"2026-12-31T23:59:59Z"}')
        response = self.app.handle("POST", "/reservations", {"X-Actor-Id": "labu"},
                                   b'{"reservation_id":"j1","tenant_id":"lab","pool_id":"h100",'
                                   b'"requested_hours":"5","starts_at":"2026-09-25T00:00:00Z",'
                                   b'"ends_at":"2026-09-25T05:00:00Z",'
                                   b'"idempotency_key":"k1","reason":"t"}')
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["state"], "confirmed")
        summary = self.app.handle("GET", "/credits/summary", {"X-Actor-Id": "labu"})
        self.assertEqual(summary.body["sources"]["prepaid"]["frozen"], "50.00")
        # 租户无权查看审计链。
        forbidden = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "labu"})
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"}).status, 200)


if __name__ == "__main__":
    unittest.main()
