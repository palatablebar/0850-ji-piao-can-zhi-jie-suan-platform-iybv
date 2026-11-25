from __future__ import annotations

import secrets
from typing import Any

from app.agent_delivery.core import (
    DELIVERY_SCHEMA,
    build_agent_voucher,
    build_agent_vouchers,
)
from app.errors import DomainIssue

PAYMENT_CONTAINER_KEYS = (
    "payments", "payment_records", "records", "items", "entries", "funds",
    "settlements", "settlement_items", "results", "rows", "allocations",
    "handlings", "agent_payments", "voucher_items", "vouchers",
)
ASSIGNMENT_KEYS = ("assignments", "attributions", "agent_assignments", "allocation", "handlers")
_ID_MATCH_FIELDS = (
    "payment_id", "payment_no", "payment_number", "payment_key", "settlement_id",
    "entry_id", "item_id", "id", "ticket_id", "ticket_no", "ticket_number",
    "check_no", "reconciliation_no",
)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return [value]


def _records_from_container(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        # Could be agent_id -> [payments], payment_id -> agent, or one record.
        if any(isinstance(item, (list, dict)) for item in value.values()):
            records: list[dict[str, Any]] = []
            for key, item in value.items():
                if isinstance(item, list):
                    for row in item:
                        if isinstance(row, dict):
                            row = dict(row)
                            row.setdefault("agent_id", key)
                            records.append(row)
                elif isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("agent_id", key)
                    records.append(row)
            if records:
                return records
        return [dict(value)]
    return []


def _apply_agent_scalar(records: list[dict[str, Any]], agent_id: Any) -> None:
    if not agent_id:
        return
    for record in records:
        record.setdefault("agent_id", agent_id)


def _match_assignment(record: dict[str, Any], assignment: dict[str, Any]) -> bool:
    for field in _ID_MATCH_FIELDS:
        expected = assignment.get(field)
        if expected is None:
            continue
        actual = record.get(field)
        if actual is not None and str(actual) == str(expected):
            return True
    # Assignment may use generic target_id.
    target = assignment.get("target_id") or assignment.get("subject_id")
    if target is not None:
        return any(str(record.get(field)) == str(target) for field in _ID_MATCH_FIELDS if record.get(field) is not None)
    return False


def _apply_assignments(records: list[dict[str, Any]], assignments: Any) -> None:
    if isinstance(assignments, dict):
        # agent_id -> ids OR subject_id -> agent_id.
        looks_like_agent_map = all(isinstance(v, (list, tuple, set)) for v in assignments.values())
        if looks_like_agent_map:
            for agent_id, ids in assignments.items():
                id_set = {str(value) for value in _as_list(ids)}
                for record in records:
                    if any(str(record.get(field)) in id_set for field in _ID_MATCH_FIELDS if record.get(field) is not None):
                        record.setdefault("agent_id", agent_id)
            return
        for subject_id, agent_id in assignments.items():
            for record in records:
                if any(str(record.get(field)) == str(subject_id) for field in _ID_MATCH_FIELDS if record.get(field) is not None):
                    record.setdefault("agent_id", agent_id)
            continue

    for assignment in _records_from_container(assignments):
        agent_id = assignment.get("agent_id") or assignment.get("agent") or assignment.get("owner_id")
        if not agent_id:
            continue
        if not any(key in assignment for key in _ID_MATCH_FIELDS + ("target_id", "subject_id")):
            continue
        for record in records:
            if _match_assignment(record, assignment):
                record.setdefault("agent_id", agent_id)


def extract_payments(data: Any = None, run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Extract payment-like records from many request shapes."""
    records: list[dict[str, Any]] = []
    source = data

    if isinstance(source, list):
        records = _records_from_container(source)
        source_dict: dict[str, Any] = {}
    elif isinstance(source, dict):
        source_dict = source
        for key in PAYMENT_CONTAINER_KEYS:
            if key in source:
                records = _records_from_container(source[key])
                if records:
                    break
        has_payment_identity = any(key in source for key in _ID_MATCH_FIELDS)
        has_amount = any(key in source for key in ("amount_cents", "settlement_cents", "invoice_cents", "amount", "total_cents"))
        is_assignment_request = any(key in source for key in ASSIGNMENT_KEYS)
        if not records and (has_payment_identity or has_amount) and not is_assignment_request:
            records = [dict(source)]
    else:
        source_dict = {}

    if not records and run:
        for key in ("payments", "results", "rows", "ledger_entries", "entries", "items"):
            if run.get(key):
                records = _records_from_container(run[key])
                if records:
                    break

    # Request-level id lists are a compact attribution form.  Explicit
    # assignment structures are applied afterwards and must not be blanket-set.
    has_assignment_structure = isinstance(source_dict, dict) and any(
        key in source_dict for key in ASSIGNMENT_KEYS
    )
    if isinstance(source_dict, dict) and source_dict.get("agent_id") and not has_assignment_structure:
        _apply_agent_scalar(records, source_dict["agent_id"])
        for id_field in ("payment_ids", "ticket_ids", "ids"):
            ids = source_dict.get(id_field)
            if ids and records:
                wanted = {str(value) for value in _as_list(ids)}
                records = [
                    record for record in records
                    if any(str(record.get(field)) in wanted for field in _ID_MATCH_FIELDS if record.get(field) is not None)
                ]

    if isinstance(source_dict, dict):
        for key in ASSIGNMENT_KEYS:
            if key in source_dict:
                _apply_assignments(records, source_dict[key])
        if source_dict.get("agents") and not any(k in source_dict for k in PAYMENT_CONTAINER_KEYS):
            # agents may be an agent->payments map.
            extra = _records_from_container(source_dict["agents"])
            if extra:
                records.extend(extra)

    return records


def _download_path(token: str) -> str:
    return f"/api/agent-vouchers/{token}/download"


def _apply_link_fields(voucher: dict[str, Any], token: str) -> str:
    path = _download_path(token)
    voucher.update({
        "token": token,
        "download_token": token,
        "downloadToken": token,
        "download_url": path,
        "downloadUrl": path,
        "download_link": path,
        "downloadLink": path,
        "url": path,
        "link": path,
        "href": path,
    })
    return path


def issue_agent_voucher(store: Any, agent_id: str, payments: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    if hasattr(store, "is_agent_active") and not store.is_agent_active(agent_id):
        raise DomainIssue("AGENT_PERMISSION_REVOKED", 403)

    run_id = options.pop("run_id", None)
    voucher = build_agent_voucher(agent_id, payments, **options)
    token = secrets.token_urlsafe(24)
    _apply_link_fields(voucher, token)
    if hasattr(store, "issue_link"):
        link = store.issue_link(
            agent_id,
            voucher,
            token=token,
            run_id=run_id,
            batch_id=options.get("batch_id"),
        )
        # Re-delivery of the same agent+batch reuses the active capability link.
        actual_token = link["token"] if link else token
        if actual_token != token:
            _apply_link_fields(voucher, actual_token)
    return voucher


def issue_agent_vouchers(store: Any, payments: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    book = build_agent_vouchers(payments, **{k: v for k, v in options.items() if k in {"batch_id", "only_issued", "default_issued", "secret"}})
    vouchers: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    tokens: dict[str, str] = {}
    urls: dict[str, str] = {}

    for agent_id, voucher in book.items():
        if hasattr(store, "is_agent_active") and not store.is_agent_active(agent_id):
            # One revoked agent must not block another agent's delivery.  Mark
            # that agent's voucher as unavailable instead of issuing a link.
            voucher["permission_revoked"] = True
            continue
        token = secrets.token_urlsafe(24)
        _apply_link_fields(voucher, token)
        actual_token = token
        if hasattr(store, "issue_link"):
            link = store.issue_link(
                agent_id, voucher, token=token,
                run_id=options.get("run_id"), batch_id=options.get("batch_id"),
            )
            actual_token = link["token"] if link else token
            if actual_token != token:
                _apply_link_fields(voucher, actual_token)
        path = voucher["download_url"]
        vouchers.append(voucher)
        links.append({"agent_id": agent_id, "token": actual_token, "download_url": path})
        tokens[agent_id] = actual_token
        urls[agent_id] = path

    by_agent = {voucher["agent_id"]: voucher for voucher in vouchers}
    return {
        "schema": DELIVERY_SCHEMA,
        "batch_id": options.get("batch_id"),
        "batchId": options.get("batch_id"),
        "run_id": options.get("run_id"),
        "runId": options.get("run_id"),
        "vouchers": vouchers,
        "agent_vouchers": vouchers,
        "agentVouchers": vouchers,
        "items": vouchers,
        "by_agent": by_agent,
        "byAgent": by_agent,
        "vouchers_by_agent": by_agent,
        "vouchersByAgent": by_agent,
        "links": links,
        "tokens": tokens,
        "download_urls": urls,
        "downloadUrls": urls,
        "agent_ids": [v["agent_id"] for v in vouchers],
        "agentIds": [v["agent_id"] for v in vouchers],
        "count": len(vouchers),
        "excluded_count": len(book.excluded),
        "excludedCount": len(book.excluded),
        "unattributed_count": len(book.unattributed),
        "unattributedCount": len(book.unattributed),
        # Also expose each voucher directly by agent id.
        **by_agent,
    }


class AgentDeliveryService:
    """Small object facade for callers that prefer service methods."""

    def __init__(self, store: Any | None = None):
        if store is None:
            from app.agent_delivery.store import AgentDeliveryStore
            from app.config import DB_PATH
            store = AgentDeliveryStore(DB_PATH)
        self.store = store

    def create_voucher(self, agent_id: str, payments: list[dict[str, Any]], **options: Any):
        return issue_agent_voucher(self.store, agent_id, payments, **options)

    create_agent_voucher = create_voucher
    deliver_to_agent = create_voucher

    def create_vouchers(self, payments: list[dict[str, Any]], **options: Any):
        return issue_agent_vouchers(self.store, payments, **options)

    create_agent_vouchers = create_vouchers
    deliver = create_vouchers
    deliver_batch = create_vouchers

    def download(self, token: str) -> dict[str, Any]:
        link, problem = self.store.authorize_link(token)
        if problem == "LINK_NOT_FOUND":
            raise DomainIssue("LINK_NOT_FOUND", 404)
        if problem:
            raise DomainIssue(problem, 403)
        self.store.record_download(token)
        return link["voucher"]

    def revoke_agent(self, agent_id: str):
        return self.store.revoke_agent(agent_id)

    def revoke_link(self, token: str):
        return self.store.revoke_link(token)
