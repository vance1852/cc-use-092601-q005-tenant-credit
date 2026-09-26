"""多租户算力信用额度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SOURCES = ("PREPAID", "POSTPAID", "GRANT")
RESOURCE_KINDS = {"gpu-h100", "gpu-a100", "gpu-l40s", "accelerator-npu", "cpu-highmem", "storage-io"}
ANY_RESOURCE = "ANY"
ORG_KINDS = {"research", "enterprise"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def resource_kind(value: object, field: str = "resource_kind", *, allow_any: bool = False) -> str:
    result = required_text(value, field, 32)
    if allow_any and result == ANY_RESOURCE:
        return result
    if result not in RESOURCE_KINDS:
        raise ValidationFailed(f"{field} 不是受支持的资源类型")
    return result


def period_text(value: object, field: str = "period") -> str:
    result = required_text(value, field, 7)
    if not PERIOD.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 YYYY-MM 账期")
    return result


def utc_moment(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RuleInput:
    source_priority: tuple[str, ...]
    review_ttl_hours: int
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleInput":
        priority = raw.get("source_priority")
        if not isinstance(priority, list) or not priority:
            raise ValidationFailed("source_priority 必须是来源数组")
        normalized: list[str] = []
        for item in priority:
            source = required_text(item, "source_priority 元素", 16).upper()
            if source not in SOURCES:
                raise ValidationFailed("source_priority 包含未知额度来源")
            normalized.append(source)
        if sorted(normalized) != sorted(SOURCES):
            raise ValidationFailed("source_priority 必须恰好覆盖 PREPAID、POSTPAID、GRANT")
        ttl = raw.get("review_ttl_hours")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 720:
            raise ValidationFailed("review_ttl_hours 必须是 1 到 720 的整数")
        return cls(
            source_priority=tuple(normalized),
            review_ttl_hours=ttl,
            note=required_text(raw.get("note"), "note"),
        )


@dataclass(frozen=True, slots=True)
class CreditLineInput:
    line_id: str
    org_id: str
    source: str
    resource_kind: str
    total_amount: Decimal
    valid_from: str
    valid_to: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CreditLineInput":
        source = required_text(raw.get("source"), "source", 16).upper()
        if source not in SOURCES:
            raise ValidationFailed("source 必须是 PREPAID、POSTPAID 或 GRANT")
        valid_from = utc_moment(raw.get("valid_from"), "valid_from")
        valid_to = utc_moment(raw.get("valid_to"), "valid_to")
        if parse_utc(valid_to) <= parse_utc(valid_from):
            raise ValidationFailed("valid_to 必须晚于 valid_from")
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            org_id=identifier(raw.get("org_id"), "org_id"),
            source=source,
            resource_kind=resource_kind(raw.get("resource_kind"), allow_any=True),
            total_amount=decimal_value(raw.get("total_amount"), "total_amount", minimum=Decimal("0.001")),
            valid_from=valid_from,
            valid_to=valid_to,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ReservationInput:
    reservation_id: str
    org_id: str
    resource_kind: str
    starts_at: str
    ends_at: str
    estimated_amount: Decimal
    idempotency_key: str
    exemption_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationInput":
        starts_at = utc_moment(raw.get("starts_at"), "starts_at")
        ends_at = utc_moment(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        exemption = raw.get("exemption_id")
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            org_id=identifier(raw.get("org_id"), "org_id"),
            resource_kind=resource_kind(raw.get("resource_kind")),
            starts_at=starts_at,
            ends_at=ends_at,
            estimated_amount=decimal_value(
                raw.get("estimated_amount"), "estimated_amount", minimum=Decimal("0.001")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            exemption_id=None if exemption is None else identifier(exemption, "exemption_id"),
        )


@dataclass(frozen=True, slots=True)
class ExemptionInput:
    request_id: str
    org_id: str
    resource_kind: str
    starts_at: str
    ends_at: str
    requested_amount: Decimal
    reason: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExemptionInput":
        starts_at = utc_moment(raw.get("starts_at"), "starts_at")
        ends_at = utc_moment(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            request_id=identifier(raw.get("request_id"), "request_id"),
            org_id=identifier(raw.get("org_id"), "org_id"),
            resource_kind=resource_kind(raw.get("resource_kind")),
            starts_at=starts_at,
            ends_at=ends_at,
            requested_amount=decimal_value(
                raw.get("requested_amount"), "requested_amount", minimum=Decimal("0.001")
            ),
            reason=required_text(raw.get("reason"), "reason"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
