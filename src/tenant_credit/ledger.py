"""确定性的账期切分、额度选择与扣减计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Sequence


ZERO = Decimal("0")
QUANTUM = Decimal("0.001")


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def period_of(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def period_bounds(period: str) -> tuple[datetime, datetime]:
    from datetime import timezone

    year = int(period[:4])
    month = int(period[5:7])
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


@dataclass(frozen=True, slots=True)
class SlicePlan:
    period: str
    starts_at: datetime
    ends_at: datetime
    amount: Decimal


def split_by_month(start: datetime, end: datetime, total: Decimal) -> list[SlicePlan]:
    """把 [start, end) 的预计消耗按 UTC 自然月切分。

    每片金额按片内时长占比分配，非末片向下取整到 0.001，
    差额全部归入末片，保证各片之和恒等于总额。
    """
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    if total <= ZERO:
        raise ValueError("预计消耗必须大于零")
    spans: list[tuple[str, datetime, datetime]] = []
    cursor = start
    while cursor < end:
        period = period_of(cursor)
        _, period_end = period_bounds(period)
        slice_end = min(period_end, end)
        spans.append((period, cursor, slice_end))
        cursor = slice_end
    total_seconds = Decimal((end - start).total_seconds())
    result: list[SlicePlan] = []
    allocated = ZERO
    for index, (period, slice_start, slice_end) in enumerate(spans):
        if index == len(spans) - 1:
            amount = quantize_amount(total) - allocated
        else:
            share = total * Decimal((slice_end - slice_start).total_seconds()) / total_seconds
            amount = share.quantize(QUANTUM, rounding=ROUND_DOWN)
            allocated += amount
        result.append(SlicePlan(period, slice_start, slice_end, amount))
    return result


def attribute_executed(slice_amounts: Sequence[Decimal], executed: Decimal) -> list[Decimal]:
    """把已执行消耗按时间顺序归属到各个月度切片。"""
    if executed < ZERO:
        raise ValueError("已执行消耗不能为负数")
    remaining = executed
    result: list[Decimal] = []
    for amount in slice_amounts:
        take = min(amount, remaining)
        result.append(take)
        remaining -= take
    if remaining > ZERO:
        raise ValueError("已执行消耗超过预计总量")
    return result


@dataclass(frozen=True, slots=True)
class LineCandidate:
    line_id: str
    source: str
    available: Decimal
    valid_to: datetime


def select_lines(
    candidates: Sequence[LineCandidate],
    amount: Decimal,
    source_priority: Sequence[str],
) -> tuple[list[tuple[str, Decimal]], Decimal]:
    """按 (有效期截止, 来源优先级, 编号) 顺序贪心选择额度。

    返回 (扣减计划, 缺口)；缺口为零表示额度充足。
    """
    rank = {source: index for index, source in enumerate(source_priority)}
    ordered = sorted(candidates, key=lambda item: (item.valid_to, rank[item.source], item.line_id))
    plan: list[tuple[str, Decimal]] = []
    remaining = amount
    for candidate in ordered:
        if remaining <= ZERO:
            break
        take = min(candidate.available, remaining)
        if take > ZERO:
            plan.append((candidate.line_id, take))
            remaining -= take
    return plan, remaining
