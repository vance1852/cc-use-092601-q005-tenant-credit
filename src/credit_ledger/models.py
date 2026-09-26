"""信用额度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SOURCE_TYPES = {"prepaid", "postpaid", "grant"}
PRODUCTS = {"gpu-h100", "gpu-a100", "gpu-l40s", "accelerator-npu", "cpu-highmem", "storage-io"}


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


def positive_decimal(value: object, field: str) -> Decimal:
    return decimal_value(value, field, minimum=Decimal("0.001"))


def product_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    result: list[str] = []
    for item in value:
        product = required_text(item, f"{field} 元素", 32)
        if product not in PRODUCTS:
            raise ValidationFailed(f"{field} 包含不支持的资源类型 {product}")
        if product not in result:
            result.append(product)
    return result


def utc_field(value: object, field: str) -> datetime:
    text = required_text(value, field, 40)
    try:
        return parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class CreditGrant:
    credit_id: str
    tenant_id: str
    source_type: str
    amount: Decimal
    products: list[str]
    valid_from: datetime
    valid_to: datetime | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CreditGrant":
        source_type = required_text(raw.get("source_type"), "source_type", 16).lower()
        if source_type not in SOURCE_TYPES:
            raise ValidationFailed("source_type 必须是 prepaid、postpaid 或 grant")
        valid_from = utc_field(raw.get("valid_from"), "valid_from")
        valid_to = None
        if raw.get("valid_to"):
            valid_to = utc_field(raw.get("valid_to"), "valid_to")
            if valid_to <= valid_from:
                raise ValidationFailed("valid_to 必须晚于 valid_from")
        return cls(
            credit_id=identifier(raw.get("credit_id"), "credit_id"),
            tenant_id=identifier(raw.get("tenant_id"), "tenant_id"),
            source_type=source_type,
            amount=positive_decimal(raw.get("amount"), "amount"),
            products=product_list(raw.get("products"), "products"),
            valid_from=valid_from,
            valid_to=valid_to,
        )


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    reservation_id: str
    tenant_id: str
    pool_id: str
    requested_hours: Decimal
    starts_at: datetime
    ends_at: datetime
    idempotency_key: str
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReservationRequest":
        starts_at = utc_field(raw.get("starts_at"), "starts_at")
        ends_at = utc_field(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            reservation_id=identifier(raw.get("reservation_id"), "reservation_id"),
            tenant_id=identifier(raw.get("tenant_id"), "tenant_id"),
            pool_id=identifier(raw.get("pool_id"), "pool_id"),
            requested_hours=positive_decimal(raw.get("requested_hours"), "requested_hours"),
            starts_at=starts_at,
            ends_at=ends_at,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            reason=required_text(raw.get("reason"), "reason", 512),
        )
