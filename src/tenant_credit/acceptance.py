"""贯通额度行、跨月预约、超额复核、账期结算与到期核销的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, InsufficientCredit
from .service import CreditService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = CreditService(connection, clock)

    service.create_org("org-cas", "某科研机构", "research")
    service.create_org("org-abc", "某企业客户", "enterprise")
    service.create_user("tenant-cas", "科研机构调度员", "tenant_admin", "org-cas")
    service.create_user("tenant-abc", "企业调度员", "tenant_admin", "org-abc")
    service.create_user("sched", "平台调度", "scheduler")
    service.create_user("fin", "财务", "finance")
    service.create_user("rev-1", "复核人甲", "reviewer")
    service.create_user("rev-2", "复核人乙", "reviewer")
    service.create_user("audit", "审计", "auditor")

    rule = service.publish_rule("fin", {"source_priority": ["GRANT", "PREPAID", "POSTPAID"], "review_ttl_hours": 48, "note": "先用赠送，再预付，最后后付"})
    service.open_credit_line("fin", {"line_id": "line-grant-sep", "org_id": "org-cas", "source": "GRANT", "resource_kind": "gpu-h100", "total_amount": "1000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "idempotency_key": "line-key-1"})
    service.open_credit_line("fin", {"line_id": "line-prepaid-q4", "org_id": "org-cas", "source": "PREPAID", "resource_kind": "ANY", "total_amount": "5000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-11-01T00:00:00Z", "idempotency_key": "line-key-2"})
    service.open_credit_line("fin", {"line_id": "line-postpaid-sep", "org_id": "org-cas", "source": "POSTPAID", "resource_kind": "gpu-h100", "total_amount": "2000", "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "idempotency_key": "line-key-3"})
    service.open_credit_line("fin", {"line_id": "line-abc-oct", "org_id": "org-abc", "source": "PREPAID", "resource_kind": "gpu-a100", "total_amount": "800", "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-11-01T00:00:00Z", "idempotency_key": "line-key-4"})

    cross = service.confirm_reservation("tenant-cas", {"reservation_id": "res-cross", "org_id": "org-cas", "resource_kind": "gpu-h100", "starts_at": "2026-09-28T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z", "estimated_amount": "1200", "idempotency_key": "res-key-1"})
    closed_cross = service.complete_reservation("sched", "res-cross", "800")

    service.confirm_reservation("tenant-cas", {"reservation_id": "res-cancel", "org_id": "org-cas", "resource_kind": "cpu-highmem", "starts_at": "2026-10-10T00:00:00Z", "ends_at": "2026-10-11T00:00:00Z", "estimated_amount": "100", "idempotency_key": "res-key-2"})
    failed_cancel = service.fail_reservation("sched", "res-cancel", "40")

    insufficient = None
    try:
        service.confirm_reservation("tenant-cas", {"reservation_id": "res-big", "org_id": "org-cas", "resource_kind": "gpu-h100", "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-06T00:00:00Z", "estimated_amount": "6000", "idempotency_key": "res-key-3"})
    except InsufficientCredit as exc:
        insufficient = {"code": exc.code, "message": str(exc)}
    exemption = service.request_exemption("tenant-cas", {"request_id": "ex-big", "org_id": "org-cas", "resource_kind": "gpu-h100", "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-06T00:00:00Z", "requested_amount": "6000", "reason": "关键模型训练窗口，缺口申请豁免", "idempotency_key": "ex-key-1"})
    decision = service.decide_exemption("rev-1", "ex-big", True, "同意，纳入重点保障")
    big = service.confirm_reservation("tenant-cas", {"reservation_id": "res-big", "org_id": "org-cas", "resource_kind": "gpu-h100", "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-06T00:00:00Z", "estimated_amount": "6000", "idempotency_key": "res-key-3", "exemption_id": "ex-big"})

    service.confirm_reservation("tenant-abc", {"reservation_id": "res-abc-1", "org_id": "org-abc", "resource_kind": "gpu-a100", "starts_at": "2026-10-03T00:00:00Z", "ends_at": "2026-10-04T00:00:00Z", "estimated_amount": "200", "idempotency_key": "res-key-4"})

    clock.advance(days=39)
    expired = service.expire_credit_lines("fin")
    settled = service.settle_period("fin", "org-cas", "2026-09")
    service.publish_rule("fin", {"source_priority": ["POSTPAID", "PREPAID", "GRANT"], "review_ttl_hours": 24, "note": "新规：后付优先"})
    settled_after_rule_update = service.billing_period("fin", "org-cas", "2026-09")
    resettle_rejected = None
    try:
        service.settle_period("fin", "org-cas", "2026-09")
    except Conflict as exc:
        resettle_rejected = {"code": exc.code, "message": str(exc)}

    result = {
        "status": "ok",
        "rule_version": rule["rule_version"],
        "cross_month_reservation": cross,
        "cross_month_closed": closed_cross,
        "failed_reservation": failed_cancel,
        "insufficient_without_exemption": insufficient,
        "exemption": {"request": exemption, "decision": decision},
        "exempted_reservation": big,
        "expired_lines": expired,
        "settled_period": settled,
        "settled_period_after_rule_update": settled_after_rule_update,
        "resettle_rejected": resettle_rejected,
        "tenant_summary": service.org_summary("tenant-cas", "org-cas"),
        "tenant_ledger_entries": len(service.org_ledger("tenant-cas", "org-cas")["entries"]),
        "explained_reservation": service.explain_reservation("audit", "res-big"),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行多租户算力信用额度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
