import pytest

from app.agent_delivery.core import (
    agent_reconciliation_number,
    build_agent_voucher,
    build_agent_vouchers,
)


def payment(**overrides):
    row = {
        "payment_id": "P1",
        "agent_id": "A1",
        "ticket_id": "T-SHARED",
        "passenger_id": "P-SECRET",
        "amount_cents": 1000,
        "status": "ISSUED",
    }
    row.update(overrides)
    return row


def test_voucher_lists_only_handled_issued_and_attributed_payments():
    book = build_agent_vouchers([
        payment(),
        payment(payment_id="P2", agent_id="A1", amount_cents=250),
        payment(payment_id="P3", agent_id="A2", amount_cents=400),
        payment(payment_id="P4", agent_id="A1", amount_cents=800, status="DRAFT"),
        payment(payment_id="P5", agent_id=None, amount_cents=1600),
        payment(payment_id="P6", agent_id="", attribution_status="UNKNOWN", amount_cents=3200),
    ])

    assert [item["payment_id"] for item in book["A1"]["payments"]] == ["P1", "P2"]
    assert [item["payment_id"] for item in book["A2"]["payments"]] == ["P3"]
    assert book.unattributed
    assert book["A1"]["total_cents"] == 1250
    assert book["A2"]["total_cents"] == 400


def test_reconciliation_number_is_stable_for_same_agent_and_payment():
    first = agent_reconciliation_number("A1", "P1")
    second = agent_reconciliation_number("A1", "P1")
    assert first == second
    assert first == build_agent_voucher("A1", [payment()])["payments"][0]["reconciliation_no"]
    assert first == build_agent_voucher(
        "A1", [payment(batch_id="B2")], batch_id="B2"
    )["payments"][0]["reconciliation_no"]


def test_references_are_unlinkable_between_agents_and_hide_ticket_identity():
    first = build_agent_voucher("A1", [payment()])
    second = build_agent_voucher("A2", [payment(agent_id="A2")])

    first_no = first["payments"][0]["reconciliation_no"]
    second_no = second["payments"][0]["reconciliation_no"]
    assert first_no != second_no
    assert first_no != "T-SHARED"
    assert second_no != "T-SHARED"

    import json
    body = json.dumps([first, second], ensure_ascii=False)
    assert "T-SHARED" not in body
    assert "P-SECRET" not in body


def test_total_uses_only_actually_listed_payments():
    voucher = build_agent_voucher("A1", [
        payment(amount_cents=100),
        payment(payment_id="P2", amount_cents=200),
        payment(payment_id="P3", amount_cents=400, status="PENDING"),
    ])
    assert voucher["count"] == 2
    assert voucher["total_cents"] == 300


def test_http_delivery_retry_and_revocation(client):
    response = client.post("/api/agent-vouchers", json={
        "batch_id": "B1",
        "payments": [
            payment(),
            payment(payment_id="P2", agent_id="A2", amount_cents=2000),
            payment(payment_id="OLD", agent_id=None, amount_cents=9999),
        ],
    })
    assert response.status_code == 200
    delivery = response.get_json()
    url = delivery["A1"]["download_url"]

    first = client.get(url)
    second = client.get(url)
    assert first.status_code == 200
    assert second.status_code == 200
    first_no = first.get_json()["payments"][0]["reconciliation_no"]
    second_no = second.get_json()["payments"][0]["reconciliation_no"]
    assert first_no == second_no

    revoked = client.post("/api/agents/A1/revoke")
    assert revoked.status_code == 200
    blocked = client.get(url)
    assert blocked.status_code == 403


def test_approved_run_can_deliver_assigned_rows(client):
    run = client.post("/api/runs", json={"profile": "clean"}).get_json()
    client.post(
        f"/api/runs/{run['run_id']}/review",
        json={"decision": "APPROVED", "comment": "ok", "expected_revision": 0},
    )
    response = client.post(f"/api/runs/{run['run_id']}/agent-vouchers", json={
        "assignments": {"A1": ["T1", "T3"]},
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["A1"]["count"] == 2
    assert "T1" not in response.get_data(as_text=True)
