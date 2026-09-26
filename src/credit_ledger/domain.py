"""确定性的额度切分、来源选择与金额量化计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")
HOURS_Q = Decimal("0.001")
MONEY_Q = Decimal("0.01")


def quantize_hours(value: Decimal) -> Decimal:
    return value.quantize(HOURS_Q, rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def period_key(value: datetime) -> str:
    """UTC 账期键，按月切分。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m")


def _month_start(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_month(value: datetime) -> datetime:
    month = value.month + 1
    year = value.year
    if month == 13:
        month = 1
        year += 1
    return value.replace(year=year, month=month)


@dataclass(frozen=True, slots=True)
class Segment:
    index: int
    period_key: str
    start: datetime
    end: datetime
    hours: Decimal
    amount: Decimal


def split_by_month(
    starts_at: datetime,
    ends_at: datetime,
    requested_hours: Decimal,
    unit_price: Decimal,
) -> list[Segment]:
    """把作业时间窗按 UTC 月界切分，并按时间占比分摊工时与预计金额。

    末段吸收量化误差，保证各段金额之和恰好等于预计总额。
    """
    start = starts_at.astimezone(timezone.utc)
    end = ends_at.astimezone(timezone.utc)
    total_seconds = Decimal(str((end - start).total_seconds()))
    est_total = quantize_money(requested_hours * unit_price)
    boundaries: list[datetime] = [start]
    cursor = _add_month(_month_start(start))
    while cursor < end:
        boundaries.append(cursor)
        cursor = _add_month(cursor)
    boundaries.append(end)

    segments: list[Segment] = []
    accounted_hours = ZERO
    accounted_amount = ZERO
    ranges = list(zip(boundaries[:-1], boundaries[1:]))
    for index, (seg_start, seg_end) in enumerate(ranges):
        last = index == len(ranges) - 1
        if last:
            hours = quantize_hours(requested_hours - accounted_hours)
            amount = quantize_money(est_total - accounted_amount)
        else:
            seconds = Decimal(str((seg_end - seg_start).total_seconds()))
            hours = quantize_hours(requested_hours * seconds / total_seconds)
            amount = quantize_money(hours * unit_price)
            accounted_hours += hours
            accounted_amount += amount
        segments.append(
            Segment(
                index=index,
                period_key=period_key(seg_start),
                start=seg_start,
                end=seg_end,
                hours=hours,
                amount=max(ZERO, amount),
            )
        )
    return segments


@dataclass(frozen=True, slots=True)
class AvailableCredit:
    credit_id: str
    source_type: str
    available: Decimal
    valid_to: datetime | None


@dataclass(frozen=True, slots=True)
class CreditAllocation:
    credit_id: str
    source_type: str
    amount: Decimal
    rank: int


def select_credits(
    accounts: Sequence[Mapping[str, object]],
    amount: Decimal,
    product: str,
    source_priority: Sequence[str],
    now: datetime,
) -> list[CreditAllocation]:
    """按规则的来源优先级和到期先后在可用额度上分摊冻结金额。"""
    rank = {source: index for index, source in enumerate(source_priority)}
    moment = now.astimezone(timezone.utc)
    eligible: list[AvailableCredit] = []
    for row in accounts:
        products = row["products"]
        if products and product not in products:
            continue
        valid_from = row["valid_from"]
        valid_to = row["valid_to"]
        if valid_from > moment or (valid_to is not None and valid_to < moment):
            continue
        available = Decimal(row["available"])
        if available <= ZERO:
            continue
        eligible.append(
            AvailableCredit(
                credit_id=str(row["credit_id"]),
                source_type=str(row["source_type"]),
                available=available,
                valid_to=None if valid_to is None else valid_to.astimezone(timezone.utc),
            )
        )

    def ordering(item: AvailableCredit) -> tuple[int, datetime, str]:
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        return (
            rank.get(item.source_type, len(rank)),
            item.valid_to or far_future,
            item.credit_id,
        )

    ordered = sorted(eligible, key=ordering)
    allocations: list[CreditAllocation] = []
    remaining = amount
    for index, item in enumerate(ordered):
        if remaining <= ZERO:
            break
        take = min(item.available, remaining)
        allocations.append(
            CreditAllocation(
                credit_id=item.credit_id,
                source_type=item.source_type,
                amount=take,
                rank=rank.get(item.source_type, len(rank)),
            )
        )
        remaining -= take
    return allocations
