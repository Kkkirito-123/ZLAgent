"""SQLite-backed registry of runtime-attached MCP servers (v0.33).

This is the persistence layer that lets ``mcp_manage`` survive a process
restart. The static YAML seed (``config/mcp_servers.yaml``) is still the
operator-edited source for everything that ships with a deployment; this
store holds the *dynamic overlay* — anything attached on the fly through
the agent tool.

Design intent:

* No long-lived sessions. Every method opens, runs, commits, closes — the
  store is safe to call from FastAPI request handlers, the agent loop,
  and background tasks alike.
* Idempotent enough for the IM agent's needs: ``upsert`` updates the row
  if the name already exists; ``delete`` returns False (rather than
  raising) on missing names so the agent gets a clean ``ok=False`` reply.
* Strict source filter on delete: a YAML-sourced row should never appear
  here in normal flow, but ``delete()`` still refuses to remove rows
  marked ``source='yaml'`` to keep the static seed sacrosanct.
* JSON columns are decoded eagerly in :meth:`list_all` so callers get
  ``MCPServerConfig`` instances ready to feed into :class:`MCPManager`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from ..db.models import MCPServerRegistration
from .config import MCPServerConfig


# -- Result wrappers ----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StoredServer:
    """One row's worth of state, ready to surface to /api/mcp or the tool."""

    config: MCPServerConfig
    source: str
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime


# -- Store --------------------------------------------------------------------

class MCPServerStore:
    """Thin wrapper around the ``mcp_servers`` table.

    The store does not own a session; the caller passes the ``session_factory``
    (a callable returning a fresh :class:`sqlalchemy.orm.Session`). This is
    the same pattern :class:`backend.db.confirmations.ConfirmationStore` uses,
    so the rest of the runtime can keep its single :class:`SessionLocal`
    factory.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    # -- write paths ---------------------------------------------------------

    def upsert(
        self,
        cfg: MCPServerConfig,
        *,
        source: str = "im",
        created_by: Optional[str] = None,
    ) -> StoredServer:
        """Create or update a row keyed by ``cfg.name``.

        Returns the resulting :class:`StoredServer`. Updating preserves the
        original ``source`` and ``created_by`` to keep audit trails honest.
        """
        if source not in ("im", "rest", "yaml"):
            raise ValueError(f"invalid source {source!r}")
        with self._session_factory() as session:  # type: Session
            row = (
                session.query(MCPServerRegistration)
                .filter_by(name=cfg.name)
                .one_or_none()
            )
            if row is None:
                row = MCPServerRegistration(
                    name=cfg.name,
                    source=source,
                    created_by=created_by,
                )
                session.add(row)
            self._apply_config(row, cfg)
            session.commit()
            session.refresh(row)
            return _row_to_stored(row)

    def delete(self, name: str) -> bool:
        """Delete a runtime-attached row by name.

        Refuses to delete rows whose ``source`` is ``'yaml'`` (which would
        only ever appear if an operator hand-inserted one — rare). Returns
        True if a row was removed, False otherwise.
        """
        with self._session_factory() as session:
            row = (
                session.query(MCPServerRegistration)
                .filter_by(name=name)
                .one_or_none()
            )
            if row is None:
                return False
            if row.source == "yaml":
                logger.warning(
                    "[mcp.store] refusing to delete yaml-sourced row {!r}",
                    name,
                )
                return False
            session.delete(row)
            session.commit()
            return True

    # -- read paths ----------------------------------------------------------

    def list_all(self) -> list[StoredServer]:
        """Return every persisted runtime registration, oldest first."""
        with self._session_factory() as session:
            rows = (
                session.query(MCPServerRegistration)
                .order_by(MCPServerRegistration.created_at.asc())
                .all()
            )
            return [_row_to_stored(r) for r in rows]

    def get(self, name: str) -> Optional[StoredServer]:
        with self._session_factory() as session:
            row = (
                session.query(MCPServerRegistration)
                .filter_by(name=name)
                .one_or_none()
            )
            return _row_to_stored(row) if row is not None else None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _apply_config(row: MCPServerRegistration, cfg: MCPServerConfig) -> None:
        """Mirror an :class:`MCPServerConfig` into ORM column state."""
        row.transport = cfg.transport
        row.command = cfg.command
        row.args_json = json.dumps(list(cfg.args), ensure_ascii=False)
        row.env_json = json.dumps(cfg.env, ensure_ascii=False)
        row.url = cfg.url
        row.headers_json = json.dumps(cfg.headers, ensure_ascii=False)
        row.description = cfg.description
        row.enabled = bool(cfg.enabled)
        # v1.1.0 — per-tool permission overrides surface here so a
        # promote() call survives a restart. Stringify keys/values
        # defensively because the config dataclass doesn't constrain
        # the dict's content.
        overrides = cfg.tool_override_permission or {}
        row.tool_override_permission_json = json.dumps(
            {str(k): str(v) for k, v in overrides.items()},
            ensure_ascii=False,
        )


def _row_to_stored(row: MCPServerRegistration) -> StoredServer:
    """Inflate an ORM row into the in-memory ``MCPServerConfig`` shape."""
    args = _safe_json_list(row.args_json)
    env = _safe_json_dict(row.env_json)
    headers = _safe_json_dict(row.headers_json)
    overrides = _safe_json_dict(
        getattr(row, "tool_override_permission_json", "") or "",
    )
    cfg = MCPServerConfig(
        name=row.name,
        command=row.command or "",
        args=tuple(str(a) for a in args),
        env={str(k): str(v) for k, v in env.items()},
        url=row.url or "",
        headers={str(k): str(v) for k, v in headers.items()},
        enabled=bool(row.enabled),
        description=row.description or "",
        tool_override_permission={
            str(k): str(v) for k, v in overrides.items()
        },
    )
    return StoredServer(
        config=cfg,
        source=row.source,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _safe_json_list(raw: str) -> list:
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def _safe_json_dict(raw: str) -> dict:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except (ValueError, TypeError):
        return {}


__all__ = ["MCPServerStore", "StoredServer"]
