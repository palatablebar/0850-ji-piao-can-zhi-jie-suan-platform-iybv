from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentDeliveryStore:
    """Persistent capability links and current agent permission state."""

    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._memory_connection: sqlite3.Connection | None = None
        if str(path) == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", timeout=8, check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents(
                  id TEXT PRIMARY KEY,
                  status TEXT NOT NULL DEFAULT 'ACTIVE',
                  active INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  revoked_at TEXT,
                  payload TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS agent_links(
                  token TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  scope_key TEXT NOT NULL DEFAULT '',
                  run_id TEXT,
                  batch_id TEXT,
                  voucher TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0,
                  downloads INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_agent_links_agent
                  ON agent_links(agent_id);
                CREATE INDEX IF NOT EXISTS idx_agent_links_scope
                  ON agent_links(agent_id, scope_key);
                """
            )

    def upsert_agent(self, agent_id: str, *, active: bool = True, **payload: Any) -> dict[str, Any]:
        agent_id = str(agent_id)
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO agents(id,status,active,created_at,updated_at,payload)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (agent_id, "ACTIVE" if active else "REVOKED", 1 if active else 0,
                 now, now, json.dumps(payload, ensure_ascii=False, default=str)),
            )
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (str(agent_id),)).fetchone()
        return dict(row) if row else None

    def is_agent_active(self, agent_id: str) -> bool:
        row = self.get_agent(agent_id)
        # Unknown agents are allowed during link issuance, but a token always
        # causes issuance to create the agent row first.
        if row is None:
            return True
        return bool(row["active"]) and row["status"] not in {"REVOKED", "INACTIVE", "DISABLED"} and not row["revoked_at"]

    def revoke_agent(self, agent_id: str) -> dict[str, Any]:
        agent_id = str(agent_id)
        now = _now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO agents(id,status,active,created_at,updated_at,revoked_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE
                  SET status='REVOKED', active=0, updated_at=excluded.updated_at,
                      revoked_at=excluded.revoked_at
                """,
                (agent_id, "REVOKED", 0, now, now, now),
            )
            # Existing capability links are invalid immediately and permanently.
            db.execute("UPDATE agent_links SET revoked=1 WHERE agent_id=?", (agent_id,))
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def activate_agent(self, agent_id: str) -> dict[str, Any]:
        now = _now()
        self.upsert_agent(agent_id, active=True)
        with self.connect() as db:
            db.execute(
                "UPDATE agents SET status='ACTIVE', active=1, revoked_at=NULL, updated_at=? WHERE id=?",
                (now, str(agent_id)),
            )
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def issue_link(
        self,
        agent_id: str,
        voucher: dict[str, Any],
        *,
        token: str | None = None,
        run_id: str | None = None,
        batch_id: str | None = None,
        scope_key: str | None = None,
        reuse_scope: bool = True,
    ) -> dict[str, Any]:
        agent_id = str(agent_id)
        self.upsert_agent(agent_id, active=True)
        batch_id = batch_id or voucher.get("batch_id")
        run_id = run_id or voucher.get("run_id")
        scope_key = scope_key if scope_key is not None else str(run_id or batch_id or "")

        if reuse_scope and scope_key:
            existing = self.active_link_for_scope(agent_id, scope_key)
            if existing:
                return existing

        token = token or secrets.token_urlsafe(24)
        now = _now()
        stored_voucher = json.dumps(voucher, ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO agent_links(
                  token,agent_id,scope_key,run_id,batch_id,voucher,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (token, agent_id, scope_key, run_id, batch_id, stored_voucher, now),
            )
        return self.get_link(token)  # type: ignore[return-value]

    def active_link_for_scope(self, agent_id: str, scope_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM agent_links
                WHERE agent_id=? AND scope_key=? AND revoked=0
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(agent_id), scope_key),
            ).fetchone()
        return dict(row) if row else None

    def get_link(self, token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_links WHERE token=?", (str(token),)).fetchone()
        if not row:
            return None
        link = dict(row)
        link["voucher"] = json.loads(link["voucher"])
        return link

    def revoke_link(self, token: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE agent_links SET revoked=1 WHERE token=?", (str(token),))
            return cursor.rowcount > 0

    def record_download(self, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE agent_links SET downloads=downloads+1 WHERE token=?", (str(token),))

    def authorize_link(self, token: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return link and None when downloadable, otherwise link and error code."""
        link = self.get_link(token)
        if link is None:
            return None, "LINK_NOT_FOUND"
        if not self.is_agent_active(link["agent_id"]):
            return link, "AGENT_PERMISSION_REVOKED"
        if link["revoked"]:
            return link, "LINK_REVOKED"
        return link, None
