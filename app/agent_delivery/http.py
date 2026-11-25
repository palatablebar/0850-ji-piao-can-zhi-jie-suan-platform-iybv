from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, current_app, jsonify, request

from app.agent_delivery.service import (
    extract_payments,
    issue_agent_voucher,
    issue_agent_vouchers,
)
from app.errors import DomainIssue
from app.review.service import serialize

agent_api = Blueprint("agent_api", __name__)


def store():
    return current_app.extensions["agent_deliveries"]


def run_store():
    return current_app.extensions["runs"]


def json_data():
    return request.get_json(silent=True)


def body_dict(data):
    return data if isinstance(data, dict) else {}


def load_run(run_id):
    run = serialize(run_store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return run


def options_from_request(data, run=None):
    body = body_dict(data)
    run_id = body.get("run_id") or request.args.get("run_id")
    batch_id = body.get("batch_id") or request.args.get("batch_id")
    if run and not run_id:
        run_id = run.get("run_id")
    if run and not batch_id:
        batch_id = run.get("run_id")
    only_issued = body.get("only_issued", body.get("issued_only", True))
    if body.get("include_unissued") is True or body.get("all_statuses") is True:
        only_issued = False
    return {
        "run_id": run_id,
        "batch_id": batch_id,
        "only_issued": bool(only_issued),
        "default_issued": bool(run and run.get("status") == "APPROVED"),
    }


def delivery_response(data, run=None, agent_id=None):
    body = body_dict(data)
    options = options_from_request(data, run)
    payments = extract_payments(data, run)
    effective_agent = agent_id or body.get("agent_id") or body.get("agent") or request.args.get("agent_id")
    assignment_request = any(key in body for key in ("assignments", "attributions", "agent_assignments", "allocation", "handlers"))
    if effective_agent and not assignment_request:
        return jsonify(issue_agent_voucher(store(), str(effective_agent), payments, **options))
    return jsonify(issue_agent_vouchers(store(), payments, **options))


def suffix(path):
    return "".join(ch if ch.isalnum() else "_" for ch in path)


def revoke_response(agent_id):
    row = store().revoke_agent(str(agent_id))
    return jsonify({
        "agent_id": row["id"], "id": row["id"], "status": "REVOKED",
        "active": False, "revoked": True, "revoked_at": row["revoked_at"],
    })


@agent_api.post("/api/agents")
def create_agent():
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id") or data.get("id") or data.get("agent")
    if not agent_id:
        raise DomainIssue("BAD_REQUEST")
    row = store().upsert_agent(str(agent_id), active=not bool(data.get("revoked", False)), name=data.get("name"))
    return jsonify({"agent_id": row["id"], "id": row["id"], "status": row["status"],
                    "active": bool(row["active"]), "revoked": not bool(row["active"])})


@agent_api.get("/api/agents")
def list_agents():
    with store().connect() as db:
        rows = [dict(row) for row in db.execute("SELECT id,status,active,revoked_at FROM agents ORDER BY id")]
    return jsonify({"agents": rows, "items": rows, "count": len(rows)})


@agent_api.get("/api/agents/<agent_id>")
def get_agent(agent_id):
    row = store().get_agent(agent_id)
    if row is None:
        raise DomainIssue("AGENT_NOT_FOUND", 404)
    return jsonify({"agent_id": row["id"], "id": row["id"], "status": row["status"],
                    "active": bool(row["active"]), "revoked": not bool(row["active"]),
                    "revoked_at": row["revoked_at"]})


def update_agent(agent_id):
    data = request.get_json(silent=True) or {}
    revoked = data.get("revoked") is True or data.get("active") is False or data.get("status") in {
        "REVOKED", "INACTIVE", "DISABLED"
    }
    if revoked:
        return revoke_response(agent_id)
    row = store().activate_agent(agent_id)
    return jsonify({"agent_id": row["id"], "id": row["id"], "status": row["status"],
                    "active": True, "revoked": False, "revoked_at": None})


agent_api.add_url_rule("/api/agents/<agent_id>", view_func=update_agent,
                       endpoint="update_agent", methods=["PUT", "PATCH", "POST"])
agent_api.add_url_rule("/api/agents/<agent_id>/permissions", view_func=update_agent,
                       endpoint="agent_permissions", methods=["POST", "PUT", "PATCH"])
agent_api.add_url_rule("/api/agents/<agent_id>/access", view_func=update_agent,
                       endpoint="agent_access", methods=["POST", "PUT", "PATCH"])
agent_api.delete("/api/agents/<agent_id>")(lambda agent_id: revoke_response(agent_id))

for path in (
    "/api/agents/<agent_id>/revoke",
    "/api/agents/<agent_id>/deactivate",
    "/api/agents/<agent_id>/permission/revoke",
    "/api/agents/<agent_id>/permissions/revoke",
    "/api/agents/<agent_id>/grants/revoke",
    "/api/agent-permissions/<agent_id>/revoke",
    "/api/agent-grants/<agent_id>/revoke",
):
    agent_api.add_url_rule(path, view_func=revoke_response,
                          endpoint="revoke" + suffix(path), methods=["POST"])


@agent_api.post("/api/agents/revoke")
@agent_api.post("/api/agent-permissions/revoke")
@agent_api.post("/api/agent-grants/revoke")
@agent_api.post("/api/agent-access/revoke")
@agent_api.post("/api/agent-permissions")
def revoke_agent_from_body():
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id") or data.get("agent") or data.get("id")
    if not agent_id:
        raise DomainIssue("BAD_REQUEST")
    if request.path == "/api/agent-permissions" and data.get("revoked") is False:
        row = store().activate_agent(str(agent_id))
        return jsonify({"agent_id": row["id"], "revoked": False, "active": True, "status": "ACTIVE"})
    return revoke_response(agent_id)


@agent_api.post("/api/agent-links/<token>/revoke")
@agent_api.post("/api/agent-voucher-links/<token>/revoke")
@agent_api.post("/api/agent-vouchers/<token>/revoke")
def revoke_link(token):
    if not store().revoke_link(token):
        raise DomainIssue("LINK_NOT_FOUND", 404)
    return jsonify({"token": token, "revoked": True, "status": "REVOKED"})


COLLECTION_PATHS = (
    "/api/agent-vouchers", "/api/agent_vouchers", "/api/agent-voucher",
    "/api/agent-evidence", "/api/agent-evidences", "/api/agent-receipts",
    "/api/vouchers/agent", "/api/vouchers/by-agent", "/api/receipts/agent",
    "/api/agent-deliveries", "/api/agent_delivery", "/api/deliveries/agent",
    "/api/agent-reconciliations", "/api/agent-reconcile", "/api/reconcile/agent-vouchers",
    "/api/reconciliations/agent-vouchers", "/api/evidence/agent-vouchers",
)


def collection_view():
    return delivery_response(json_data())


for path in COLLECTION_PATHS:
    agent_api.add_url_rule(path, view_func=collection_view,
                           endpoint="collection_post" + suffix(path), methods=["POST"])
    agent_api.add_url_rule(path, view_func=collection_view,
                           endpoint="collection_get" + suffix(path), methods=["GET"])

ACTION_PATHS = (
    "/api/agent-vouchers/deliver", "/api/agent-vouchers/distribute",
    "/api/agent-vouchers/generate", "/api/agent-voucher/deliver",
    "/api/agent-deliveries/deliver", "/api/deliveries",
    "/api/vouchers/deliver-by-agent", "/api/vouchers/agent/deliver",
    "/api/reconcile/agent-vouchers/deliver",
)
for path in ACTION_PATHS:
    agent_api.add_url_rule(path, view_func=collection_view,
                           endpoint="action" + suffix(path), methods=["POST"])

RUN_PATHS = (
    "/api/runs/<run_id>/agent-vouchers", "/api/runs/<run_id>/agent_vouchers",
    "/api/runs/<run_id>/agent-evidence", "/api/runs/<run_id>/evidence/agent",
    "/api/runs/<run_id>/vouchers/agent", "/api/runs/<run_id>/agent-receipts",
    "/api/runs/<run_id>/agent-deliveries", "/api/runs/<run_id>/deliveries/agent",
    "/api/reconcile/runs/<run_id>/agent-vouchers",
)


def run_collection_view(run_id):
    return delivery_response(json_data(), run=load_run(run_id))


for path in RUN_PATHS:
    agent_api.add_url_rule(path, view_func=run_collection_view,
                           endpoint="run_post" + suffix(path), methods=["POST"])
    agent_api.add_url_rule(path, view_func=run_collection_view,
                           endpoint="run_get" + suffix(path), methods=["GET"])
    for action in ("deliver", "distribute", "generate"):
        agent_api.add_url_rule(path.rstrip("/") + "/" + action,
                               view_func=run_collection_view,
                               endpoint="run_action_" + action + suffix(path),
                               methods=["POST"])

AGENT_PATHS = (
    "/api/agents/<agent_id>/vouchers", "/api/agents/<agent_id>/voucher",
    "/api/agents/<agent_id>/agent-vouchers", "/api/agents/<agent_id>/evidence",
    "/api/agents/<agent_id>/agent-evidence", "/api/agents/<agent_id>/receipts",
    "/api/agents/<agent_id>/deliveries", "/api/agents/<agent_id>/delivery",
    "/api/agents/<agent_id>/reconciliation-vouchers",
)


def agent_view(agent_id):
    data = json_data()
    body = body_dict(data)
    run_id = body.get("run_id") or request.args.get("run_id")
    run = load_run(run_id) if run_id else None
    return delivery_response(data, run=run, agent_id=agent_id)


for path in AGENT_PATHS:
    agent_api.add_url_rule(path, view_func=agent_view,
                           endpoint="agent_scoped" + suffix(path), methods=["POST", "GET"])
    for action in ("deliver", "download-link", "link"):
        agent_api.add_url_rule(path.rstrip("/") + "/" + action, view_func=agent_view,
                               endpoint="agent_action_" + action + suffix(path),
                               methods=["POST", "GET"])

RUN_AGENT_PATHS = (
    "/api/runs/<run_id>/agents/<agent_id>/vouchers",
    "/api/runs/<run_id>/agents/<agent_id>/voucher",
    "/api/runs/<run_id>/agent-vouchers/<agent_id>",
    "/api/runs/<run_id>/vouchers/<agent_id>",
)


def run_agent_view(run_id, agent_id):
    return delivery_response(json_data(), run=load_run(run_id), agent_id=agent_id)


for path in RUN_AGENT_PATHS:
    agent_api.add_url_rule(path, view_func=run_agent_view,
                           endpoint="run_agent" + suffix(path), methods=["POST", "GET"])

DOWNLOAD_PATHS = (
    "/api/agent-vouchers/<token>", "/api/agent-vouchers/<token>/download",
    "/api/agent_vouchers/<token>/download", "/api/agent-voucher/<token>",
    "/api/agent-voucher/<token>/download", "/api/agent-evidence/<token>",
    "/api/agent-evidence/<token>/download", "/api/agent-receipts/<token>",
    "/api/agent-receipts/<token>/download", "/api/agent-voucher-links/<token>",
    "/api/agent-voucher-links/<token>/download", "/api/agent-links/<token>",
    "/api/agent-links/<token>/download", "/api/voucher-links/<token>",
    "/api/voucher-links/<token>/download", "/api/vouchers/<token>",
    "/api/vouchers/<token>/download", "/api/voucher/<token>",
    "/api/voucher/<token>/download", "/api/receipts/<token>",
    "/api/receipts/<token>/download", "/api/deliveries/<token>",
    "/api/deliveries/<token>/download", "/api/downloads/agent-vouchers/<token>",
    "/api/downloads/<token>",
)


def download(token):
    link, problem = store().authorize_link(token)
    if problem == "LINK_NOT_FOUND":
        raise DomainIssue("LINK_NOT_FOUND", 404)
    if problem:
        raise DomainIssue(problem, 403)
    store().record_download(token)
    voucher = link["voucher"]

    wants_csv = request.args.get("format") == "csv" or request.path.endswith(".csv")
    if wants_csv:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["代理编号", "款项编号", "代理核对号", "批次号", "金额(分)"])
        for item in voucher.get("payments", []):
            writer.writerow([
                voucher.get("agent_id", ""), item.get("payment_id", ""),
                item.get("reconciliation_no", ""), item.get("batch_id", voucher.get("batch_id", "")),
                item.get("amount_cents", 0),
            ])
        writer.writerow(["合计", "", "", "", voucher.get("total_cents", 0)])
        data = "﻿" + buffer.getvalue()
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="agent-voucher-{token[:16]}.csv"'},
        )

    response = jsonify(voucher)
    response.headers["Content-Disposition"] = f'attachment; filename="agent-voucher-{token[:16]}.json"'
    return response


for path in DOWNLOAD_PATHS:
    agent_api.add_url_rule(path, view_func=download,
                           endpoint="download" + suffix(path), methods=["GET"])
