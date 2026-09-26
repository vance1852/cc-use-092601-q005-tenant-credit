"""贯通额度来源、跨月切分、原子落账、超额复核与账期结算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CreditService
from .errors import Forbidden, InvalidState


def _seed_bootstrap(connection: sqlite3.Connection, now: str) -> None:
    """直接写入首批内部账号，避免在首个财务账号出现前的自举问题。"""
    connection.executescript(
        f"""
        INSERT INTO credit_tenants(tenant_id,name,created_at) VALUES
            ('inst-lab','北京某人工智能研究院','{now}'),
            ('acme-ai','某企业算力客户','{now}');
        INSERT INTO credit_users(user_id,display_name,role,tenant_id,created_at) VALUES
            ('fin','财务结算','finance',NULL,'{now}'),
            ('rev-a','复核员甲','reviewer',NULL,'{now}'),
            ('rev-b','复核员乙','reviewer',NULL,'{now}'),
            ('audit','审计','auditor',NULL,'{now}'),
            ('lab-user','研究院接口账号','tenant','inst-lab','{now}'),
            ('acme-user','企业接口账号','tenant','acme-ai','{now}');
        """
    )


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = CreditService(connection, clock)
    now = service._now()
    _seed_bootstrap(connection, now)

    # 资源池：H100 集群，单价 10 元/卡时。
    service.create_resource_pool("fin", {
        "pool_id": "h100-pool", "facility_id": "cluster-a", "product": "gpu-h100",
        "available_hours": "100000", "unit_price_cny": "10",
    })
    # 规则：先扣预付，再用专项赠送，后付兜底；默认不允许自动超额（需复核）。
    rule = service.put_rule("fin", None, ["prepaid", "grant", "postpaid"], False)

    # 研究院：预付 8000（9-10 月有效）、专项赠送 1000（全年、仅限 H100）。
    service.grant_credit("fin", {"credit_id": "lab-prepaid", "tenant_id": "inst-lab",
        "source_type": "prepaid", "amount": "8000", "products": ["gpu-h100"],
        "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z"})
    service.grant_credit("fin", {"credit_id": "lab-grant", "tenant_id": "inst-lab",
        "source_type": "grant", "amount": "1000", "products": ["gpu-h100"],
        "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z"})

    # 跨月作业：9/30 20:00 -> 10/2 20:00（48 小时），720 卡时 = 7200 元，跨 9、10 两个账期。
    cross_month = service.submit_reservation("lab-user", {
        "reservation_id": "job-cross-month", "tenant_id": "inst-lab", "pool_id": "h100-pool",
        "requested_hours": "720", "starts_at": "2026-09-30T20:00:00Z",
        "ends_at": "2026-10-02T20:00:00Z", "idempotency_key": "idem-cross", "reason": "大模型训练",
    })
    segment_periods = [segment["period_key"] for segment in cross_month["segments"]]

    # 作业开始后在 10/1 20:00 取消：恰好一半已执行，仅返还未执行部分。
    service.start_reservation("lab-user", "job-cross-month")
    clock.current = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    cancelled = service.cancel_reservation("lab-user", "job-cross-month")

    # 超额申请：企业客户无额度，10000 元作业进入有期限复核队列。
    overage = service.submit_reservation("acme-user", {
        "reservation_id": "job-over-cap", "tenant_id": "acme-ai", "pool_id": "h100-pool",
        "requested_hours": "1000", "starts_at": "2026-10-05T00:00:00Z",
        "ends_at": "2026-10-06T00:00:00Z", "idempotency_key": "idem-over", "reason": "紧急推理扩容",
    })
    review_id = overage["review_id"]
    # 提交人不能批准自己的豁免（用提交人账号直接尝试应被拒绝）。
    connection.execute("UPDATE overage_reviews SET submitted_by='rev-a' WHERE review_id=?", (review_id,))
    self_approval_blocked = False
    try:
        service.decide_review("rev-a", review_id, True, "自我批准")
    except Forbidden:
        self_approval_blocked = True
    connection.execute("UPDATE overage_reviews SET submitted_by='acme-user' WHERE review_id=?", (review_id,))
    # 给企业客户配置一笔后付额度，再由另一名复核员批准。
    service.grant_credit("fin", {"credit_id": "acme-postpaid", "tenant_id": "acme-ai",
        "source_type": "postpaid", "amount": "6000", "products": ["gpu-h100"],
        "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z"})
    decision = service.decide_review("rev-b", review_id, True, "同意后付兜底+豁免缺口")
    over_reservation = service.reservation("audit", "job-over-cap")

    # 失败作业：只返还尚未执行部分（开始后 6 小时失败，作业总长 24 小时）。
    failed_job = service.submit_reservation("lab-user", {
        "reservation_id": "job-fail", "tenant_id": "inst-lab", "pool_id": "h100-pool",
        "requested_hours": "240", "starts_at": "2026-10-10T00:00:00Z",
        "ends_at": "2026-10-11T00:00:00Z", "idempotency_key": "idem-fail", "reason": "稳定性试验",
    })
    service.start_reservation("lab-user", "job-fail")
    clock.current = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    failed = service.fail_reservation("lab-user", "job-fail")

    # 关闭 9 月账期后，规则更新与新作业都不得回写 9 月。
    closed_period = service.close_period("fin", "inst-lab", "2026-09")
    rule_v2 = service.put_rule("fin", None, ["grant", "prepaid", "postpaid"], True)
    period_rewrite_blocked = False
    try:
        service.submit_reservation("lab-user", {
            "reservation_id": "job-sept-tamper", "tenant_id": "inst-lab", "pool_id": "h100-pool",
            "requested_hours": "10", "starts_at": "2026-09-28T00:00:00Z",
            "ends_at": "2026-09-28T10:00:00Z", "idempotency_key": "idem-tamper", "reason": "回写尝试",
        })
    except InvalidState:
        period_rewrite_blocked = True

    # 租户视角：只看到本机构汇总与流水；跨机构访问被拒绝。
    lab_summary = service.credit_summary("lab-user")
    lab_ledger = service.ledger("lab-user", limit=50)
    cross_tenant_blocked = False
    try:
        service.ledger("lab-user", tenant_id="acme-ai")
    except Forbidden:
        cross_tenant_blocked = True

    # 财务/审计可解释每一笔扣减、返还和超额决定。
    consume_entry = connection.execute(
        "SELECT ledger_id FROM credit_ledger WHERE entry_type='consume' ORDER BY ledger_id LIMIT 1"
    ).fetchone()
    explanation = service.explain_ledger_entry("audit", int(consume_entry["ledger_id"]))

    audit_chain = service.audit_chain("audit")
    result = {
        "status": "ok",
        "rule_revision": rule["revision"],
        "cross_month_segments": segment_periods,
        "cross_month_est_total": cross_month["est_total"],
        "cancelled_executed": cancelled["executed_amount"],
        "cancelled_released": cancelled["released_amount"],
        "overage_queued": overage["state"],
        "self_approval_blocked": self_approval_blocked,
        "review_decision": decision["state"],
        "over_reservation_state": over_reservation["state"],
        "over_reservation_overage": over_reservation["overage_amount"],
        "failed_released": failed["released_amount"],
        "closed_period": closed_period["state"],
        "rule_v2_revision": rule_v2["revision"],
        "period_rewrite_blocked": period_rewrite_blocked,
        "cross_tenant_blocked": cross_tenant_blocked,
        "lab_available": lab_summary["sources"],
        "lab_ledger_entries": len(lab_ledger["entries"]),
        "explained_entry_type": explanation["entry"]["entry_type"],
        "audit": audit_chain,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行多租户算力信用额度离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
