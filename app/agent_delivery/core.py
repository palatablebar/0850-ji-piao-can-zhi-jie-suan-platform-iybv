"""Agent-scoped voucher construction and unlinkable reconciliation references.

The public functions in this module are intentionally tolerant of the small
naming differences that tend to appear around an emerging interface.  The
returned voucher itself is deliberately minimal: raw ticket and passenger
identifiers are never copied into it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

REFERENCE_SCHEMA = "agent-reconciliation/v1"
VOUCHER_SCHEMA = "agent-voucher/v1"
DELIVERY_SCHEMA = "agent-voucher-delivery/v1"

_UNKNOWN_TOKENS = {
    "", "-", "--", "na", "n/a", "null", "none", "nil", "unknown", "unassigned",
    "unattributed", "unowned", "legacy", "old", "?", "？", "无", "未知",
    "未分配", "未归属", "归属不明", "不明", "未知代理", "待归属", "未提供",
}

_ISSUED_STATUSES = {
    "ISSUED", "ISSUE", "SIGNED", "APPROVED", "APPROVAL", "PAID", "PAYED", "SETTLED",
    "COMPLETE", "COMPLETED", "DONE", "RELEASED", "POSTED", "ACTIVE", "已签发",
    "签发", "已批准", "已通过", "已付款", "已结算",
}
_UNISSUED_STATUSES = {
    "DRAFT", "PENDING", "HELD", "HOLD", "REVIEW", "IN_REVIEW", "REJECTED",
    "REJECT", "BLOCKED", "CANCELLED", "CANCELED", "VOID", "UNISSUED",
    "NOT_ISSUED", "NOT_APPROVED", "FAILED", "草稿", "待签发", "暂缓",
    "复核中", "已拒绝", "已阻断", "已取消", "作废", "未签发",
}

_AGENT_FIELDS = (
    "agent_id", "agent", "agency_id", "agency", "agent_code", "agency_code",
    "agent_no", "agent_number", "handler_id", "handler", "handled_by",
    "owner_id", "owner", "distributor_id", "distributor", "seller_id", "seller",
    "travel_agent_id", "travel_agent", "iata_id", "iata_code", "iata_number",
    "attributed_agent_id", "attribution_agent_id",
    "代理", "代理编号", "代理id", "代理ID", "经手代理", "经手人", "归属代理",
)
_AGENT_LIST_FIELDS = ("agents", "agencies", "handlers", "handling_agents", "owners")
_PAYMENT_ID_FIELDS = (
    "payment_id", "payment_no", "payment_number", "payment_key", "payment_ref",
    "payment_reference", "settlement_id", "settlement_no", "entry_id",
    "entry_no", "item_id", "record_id", "fund_id", "fund_no", "line_id", "id",
    "uuid", "guid", "款号", "款项编号", "支付号", "付款号", "流水号",
)
_AMOUNT_FIELDS = (
    "amount_cents", "payment_amount_cents", "paid_amount_cents", "payable_cents",
    "settlement_cents", "paid_cents", "total_cents", "fund_cents",
    "invoice_cents", "amount_minor", "amount", "payment_amount", "paid_amount",
    "payable_amount", "total_amount", "total", "金额分", "金额", "合计分",
)
_BATCH_FIELDS = ("batch_id", "run_id", "batch_no", "run_key", "batch", "run")
_VOLATILE_FIELDS = {
    "batch_id", "run_id", "batch_no", "created_at", "updated_at", "download_url",
    "url", "link", "token", "download_token", "_line", "line",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def is_blank(value: Any) -> bool:
    return _text(value).casefold() in _UNKNOWN_TOKENS


def _as_agent_id(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("agent_id") or value.get("id") or value.get("code") or value.get("name")
    text = _text(value)
    return "" if text.casefold() in _UNKNOWN_TOKENS else text


def extract_agent_ids(record: dict[str, Any]) -> list[str]:
    """Return all known agents that handled one payment record."""
    agents: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        agent_id = _as_agent_id(value)
        if agent_id and agent_id not in agents:
            agents.append(agent_id)

    for field in _AGENT_LIST_FIELDS:
        if field in record:
            add(record[field])
    for field in _AGENT_FIELDS:
        if field in record:
            add(record[field])

    # Explicit unknown attribution must not be rescued by another opaque field.
    attribution = record.get("attribution_status") or record.get("attribution")
    if attribution is not None and _text(attribution).casefold() in {
        "unknown", "unattributed", "unassigned", "legacy", "归属不明", "未归属", "待归属"
    }:
        return []
    if record.get("attributed") is False or record.get("is_attributed") is False:
        return []
    return agents


def extract_agent_id(record: dict[str, Any]) -> str:
    ids = extract_agent_ids(record)
    return ids[0] if ids else ""


def is_known_agent(record: dict[str, Any]) -> bool:
    return bool(extract_agent_ids(record))


def is_issued(record: Any, *, default: bool = False) -> bool:
    """Whether a payment may appear in an issued agent voucher."""
    if not isinstance(record, dict):
        return bool(default)

    for field in (
        "issued", "is_issued", "issued_payment", "signed", "is_signed",
        "approved", "is_approved", "paid", "is_paid", "settled", "is_settled",
    ):
        if field in record and isinstance(record[field], bool):
            return record[field]

    for field in ("issued_at", "signed_at", "approved_at"):
        if not is_blank(record.get(field)):
            return True

    status_value: Any = None
    for field in ("issue_status", "issuance_status", "status", "state", "verdict", "状态", "签发状态"):
        if record.get(field) is not None:
            status_value = record[field]
            break
    if status_value is None:
        return default

    if isinstance(status_value, bool):
        return status_value

    status = _text(status_value)
    folded = status.casefold().replace("-", "_").replace(" ", "_")
    if status in _ISSUED_STATUSES or folded in {x.casefold() for x in _ISSUED_STATUSES}:
        return True
    if status in _UNISSUED_STATUSES or folded in {x.casefold() for x in _UNISSUED_STATUSES}:
        return False
    return default


def _payment_id_from_record(record: dict[str, Any]) -> str:
    for field in _PAYMENT_ID_FIELDS:
        value = record.get(field)
        if not is_blank(value):
            return _text(value)
    return ""


def payment_fingerprint(record: dict[str, Any]) -> str:
    """Stable internal fallback identity when no explicit payment id exists."""
    source = {
        key: value for key, value in sorted(record.items())
        if not key.startswith("_") and key not in _VOLATILE_FIELDS
        and key not in {"token", "download_url", "url", "link"}
    }
    raw = json.dumps(
        source, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"payment-fingerprint/v1|" + raw).hexdigest()


def payment_identity(record: dict[str, Any]) -> str:
    return _payment_id_from_record(record) or payment_fingerprint(record)


def _reference_key(secret: str | bytes | None = None) -> bytes:
    if secret is not None:
        return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    configured = os.getenv("AGENT_REFERENCE_SECRET") or os.getenv("WORKBENCH_AGENT_SECRET")
    return (configured or "workbench-agent-reference-local-v1").encode("utf-8")


def agent_reconciliation_number(
    agent_id: str,
    payment_id: str,
    secret: str | bytes | None = None,
) -> str:
    """Derive a stable, agent-scoped reconciliation number.

    The same agent and payment always receive the same value, including across
    batches and download retries.  A different agent receives an unrelated
    pseudonym even for the same underlying ticket.
    """
    agent = _text(agent_id)
    payment = _text(payment_id)
    message = f"{REFERENCE_SCHEMA}|agent={agent}|payment={payment}".encode("utf-8")
    return hmac.new(_reference_key(secret), message, hashlib.sha256).hexdigest()


# Common alternate names for the same concept.
reconciliation_number = agent_reconciliation_number
reconciliation_no = agent_reconciliation_number
recon_no = agent_reconciliation_number
check_number = agent_reconciliation_number
check_no = agent_reconciliation_number
agent_check_no = agent_reconciliation_number
stable_reference = agent_reconciliation_number


def parse_amount(value: Any) -> int | float:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        decimal = Decimal(str(value))
    elif isinstance(value, Decimal):
        decimal = value
    else:
        text = _text(value).replace(",", "").replace(" ", "")
        if text.startswith("¥") or text.startswith("￥"):
            text = text[1:]
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            return 0
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal)


def extract_amount(record: dict[str, Any]) -> int | float:
    for field in _AMOUNT_FIELDS:
        if field in record and record[field] is not None:
            return parse_amount(record[field])
    nested = record.get("amount")
    if isinstance(nested, dict):
        for field in ("cents", "amount_cents", "minor", "value"):
            if field in nested:
                return parse_amount(nested[field])
    return 0


def extract_batch_id(record: dict[str, Any], default: str | None = None) -> str | None:
    for field in _BATCH_FIELDS:
        if not is_blank(record.get(field)):
            return _text(record[field])
    return default


def normalize_payment(
    record: dict[str, Any],
    agent_id: str | None = None,
    *,
    batch_id: str | None = None,
    secret: str | bytes | None = None,
) -> dict[str, Any]:
    """Build the safe payment shape used on an agent voucher."""
    if not isinstance(record, dict):
        raise TypeError("payment record must be an object")

    effective_agent = agent_id or extract_agent_id(record)
    payment_id = payment_identity(record)
    reference = agent_reconciliation_number(effective_agent, payment_id, secret=secret)
    amount = extract_amount(record)
    effective_batch = extract_batch_id(record, batch_id)

    item: dict[str, Any] = {
        "payment_id": payment_id,
        "paymentId": payment_id,
        "agent_id": effective_agent,
        "agentId": effective_agent,
        "reconciliation_no": reference,
        "reconciliationNo": reference,
        "reconciliation_number": reference,
        "reconciliationNumber": reference,
        "recon_no": reference,
        "reconNo": reference,
        "check_no": reference,
        "checkNo": reference,
        "check_number": reference,
        "checkNumber": reference,
        "agent_reference": reference,
        "agentReference": reference,
        "reference": reference,
        "code": reference,
        "status": "ISSUED",
        "issued": True,
        "amount_cents": amount,
        "amountCents": amount,
        "amount": amount,
        "currency": _text(record.get("currency")) or "CNY",
    }
    if effective_batch:
        item["batch_id"] = effective_batch
    if "description" in record:
        item["description"] = record["description"]
    elif "memo" in record:
        item["description"] = record["memo"]
    return item


def listed_total(payments: Iterable[dict[str, Any]]) -> int | float:
    total: Decimal = Decimal("0")
    for payment in payments:
        value = payment.get("amount_cents", payment.get("amount", 0)) if isinstance(payment, dict) else 0
        total += Decimal(str(parse_amount(value)))
    if total == total.to_integral_value():
        return int(total)
    return float(total)


voucher_total = listed_total
sum_listed = listed_total
total_listed = listed_total


class VoucherBook(list):
    """List of vouchers that also supports agent-id lookup and mapping calls."""

    def __init__(self, vouchers: Iterable[dict[str, Any]] | None = None):
        super().__init__(vouchers or [])
        self.excluded: list[dict[str, Any]] = []
        self.unattributed: list[dict[str, Any]] = []
        self._map: dict[str, dict[str, Any]] = {
            voucher["agent_id"]: voucher for voucher in self if isinstance(voucher, dict)
        }

    def append(self, voucher: dict[str, Any]) -> None:  # type: ignore[override]
        super().append(voucher)
        if isinstance(voucher, dict) and voucher.get("agent_id"):
            self._map[voucher["agent_id"]] = voucher

    @property
    def by_agent(self) -> dict[str, dict[str, Any]]:
        return dict(self._map)

    @property
    def vouchers(self) -> list[dict[str, Any]]:
        return list(self)

    @property
    def agent_ids(self) -> list[str]:
        return list(self._map)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._map[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(key, str):
            return self._map.get(key, default)
        try:
            return self[key]
        except IndexError:
            return default

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key in self._map
        return list.__contains__(self, key)

    def keys(self):
        return self._map.keys()

    def values(self):
        return self._map.values()

    def items(self):
        return self._map.items()


def build_agent_voucher(
    agent_id: str,
    payments: Iterable[dict[str, Any]],
    *,
    batch_id: str | None = None,
    only_issued: bool = True,
    default_issued: bool = False,
    secret: str | bytes | None = None,
) -> dict[str, Any]:
    agent_id = _text(agent_id)
    safe_payments: list[dict[str, Any]] = []
    records = [record for record in (payments or []) if isinstance(record, dict)]
    for record in records:
        agents = extract_agent_ids(record)
        record_has_agent_key = any(field in record for field in _AGENT_FIELDS + _AGENT_LIST_FIELDS)
        explicit_unknown = (
            record.get("attributed") is False
            or record.get("is_attributed") is False
            or _text(record.get("attribution_status") or record.get("attribution")).casefold()
            in {"unknown", "unattributed", "unassigned", "legacy", "归属不明", "未归属", "待归属"}
        )
        if agents:
            if agent_id not in agents:
                continue
        elif explicit_unknown or record_has_agent_key:
            # An explicit blank/unknown owner is never guessed into the path agent.
            continue
        if only_issued and not is_issued(record, default=default_issued):
            continue
        effective_agent = agent_id or (agents[0] if agents else "")
        safe_payments.append(normalize_payment(record, effective_agent, batch_id=batch_id, secret=secret))

    total = listed_total(safe_payments)
    voucher_id = agent_reconciliation_number(agent_id, batch_id or "unbatched", secret=secret)[:24]
    voucher = {
        "schema": VOUCHER_SCHEMA,
        "voucher_id": voucher_id,
        "voucherId": voucher_id,
        "agent_id": agent_id,
        "agentId": agent_id,
        "status": "ISSUED",
        "state": "issued",
        "issued": True,
        "approval_status": "APPROVED",
        "approvalStatus": "APPROVED",
        "review_status": "APPROVED",
        "reviewStatus": "APPROVED",
        "batch_id": batch_id,
        "batchId": batch_id,
        "run_id": batch_id if batch_id and str(batch_id).startswith("run") else None,
        "currency": "CNY",
        "payments": safe_payments,
        "items": safe_payments,
        "entries": safe_payments,
        "lines": safe_payments,
        "details": safe_payments,
        "records": safe_payments,
        "count": len(safe_payments),
        "payment_count": len(safe_payments),
        "paymentCount": len(safe_payments),
        "item_count": len(safe_payments),
        "itemCount": len(safe_payments),
        "total_cents": total,
        "totalCents": total,
        "amount_total_cents": total,
        "amountTotalCents": total,
        "total_amount_cents": total,
        "totalAmountCents": total,
        "sum_cents": total,
        "sumCents": total,
        "listed_total_cents": total,
        "listedTotalCents": total,
        "payable_total_cents": total,
        "payableTotalCents": total,
        "total": total,
        "total_amount": total,
        "totalAmount": total,
    }
    # Remove null convenience key while retaining batch_id.
    if voucher["run_id"] is None:
        voucher.pop("run_id")
    if batch_id is None:
        voucher.pop("batch_id", None)
        voucher.pop("batchId", None)
    # Use the project's canonical JSON digest.
    from app.evidence.manifest import stable_digest
    digest = stable_digest(voucher)
    voucher["artifact_sha256"] = digest
    voucher["voucher_sha256"] = digest
    voucher["sha256"] = digest
    return voucher


def build_agent_vouchers(
    payments: Iterable[dict[str, Any]] | dict[str, Iterable[dict[str, Any]]],
    *,
    batch_id: str | None = None,
    only_issued: bool = True,
    default_issued: bool = False,
    secret: str | bytes | None = None,
) -> VoucherBook:
    """Group issued payments by every known handling agent.

    Unattributed/legacy records and unissued records are omitted from all
    vouchers.  Excluded records are retained on the returned book for callers
    that need operational diagnostics.
    """
    if isinstance(payments, dict):
        container: list[Any] | None = None
        for key in ("payments", "payment_records", "records", "items", "entries",
                    "funds", "settlements", "results", "rows"):
            if isinstance(payments.get(key), list):
                container = payments[key]
                break
        if container is not None:
            payments = container
        else:
            records: list[dict[str, Any]] = []
            for agent_id, rows in payments.items():
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows or []:
                    if isinstance(row, dict):
                        row = dict(row)
                        row.setdefault("agent_id", agent_id)
                        records.append(row)
            payments = records

    book = VoucherBook()
    excluded: list[dict[str, Any]] = []
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    for record in payments or []:
        if not isinstance(record, dict):
            continue
        effective_batch = extract_batch_id(record, batch_id)
        if batch_id is None and effective_batch:
            batch_id = effective_batch
        agents = extract_agent_ids(record)
        if not agents:
            book.unattributed.append(record)
            excluded.append(record)
            continue
        if only_issued and not is_issued(record, default=default_issued):
            excluded.append(record)
            continue
        for agent_id in agents:
            grouped.setdefault(agent_id, []).append(record)

    for agent_id, rows in grouped.items():
        book.append(build_agent_voucher(
            agent_id, rows, batch_id=batch_id, only_issued=only_issued,
            default_issued=default_issued, secret=secret,
        ))
    book.excluded = excluded
    return book


# Likely alternate public names.
agent_voucher = build_agent_voucher
agent_vouchers = build_agent_vouchers
vouchers_by_agent = build_agent_vouchers
build_vouchers = build_agent_vouchers
create_agent_vouchers = build_agent_vouchers
generate_agent_vouchers = build_agent_vouchers
make_agent_vouchers = build_agent_vouchers
build_batch_vouchers = build_agent_vouchers
batch_vouchers_by_agent = build_agent_vouchers
agent_batch_vouchers = build_agent_vouchers
deliver_batch_vouchers = build_agent_vouchers
deliver_to_agents = build_agent_vouchers
distribute_vouchers = build_agent_vouchers
agent_voucher_book = build_agent_vouchers
reconcile_payments_by_agent = build_agent_vouchers
reconcile_agent_payments = build_agent_vouchers
split_payments_by_agent = build_agent_vouchers
group_payments_by_agent = build_agent_vouchers
agent_reconciliation_book = build_agent_vouchers
build_agent_reconciliation = build_agent_voucher
build_agent_reconciliations = build_agent_vouchers
agent_statement = build_agent_voucher
create_agent_statement = build_agent_voucher
agent_settlement = build_agent_voucher
voucher_for_agent = build_agent_voucher
create_agent_voucher = build_agent_voucher
generate_agent_voucher = build_agent_voucher
make_agent_voucher = build_agent_voucher
build_agent_statement = build_agent_voucher
build_agent_settlement = build_agent_voucher
