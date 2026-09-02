import logging
from fastapi import APIRouter, Request
from typing import Any, Dict

from gpustack.api.exceptions import InvalidException
from gpustack.config.config import Config, set_global_config
from gpustack.utils.config import (
    WHITELIST_CONFIG_FIELDS,
    READ_ONLY_CONFIG_FIELDS,
    apply_config_side_effects,
    coerce_value_by_field,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# Security model for /config:
#   - Server mount (routes/routes.py) wraps this router with
#     Depends(get_admin_user) — only admin users may read or mutate.
#   - Worker mount (worker/worker.py) wraps it with Depends(worker_auth) —
#     only callers holding the worker / registration token may read or
#     mutate that worker's runtime config.
# Authentication is intentionally enforced at the mount sites, not on the
# handlers, because the two deployments use different auth mechanisms
# (user DB vs. shared token) and the worker has no access to the user DB.
# Any new mount of this router MUST declare an appropriate auth dependency.


@router.get("/config")
async def get_config(request: Request):
    app_state = request.app.state
    cfg: Config = getattr(app_state, "server_config", None) or getattr(
        app_state, "config", None
    )
    if cfg is None:
        raise InvalidException(message="Config is not available")
    result: Dict[str, Any] = {}
    for field in READ_ONLY_CONFIG_FIELDS:
        if hasattr(cfg, field):
            result[field] = getattr(cfg, field)
    return result


@router.put("/config")
async def set_config(request: Request):
    app_state = request.app.state
    cfg: Config = getattr(app_state, "server_config", None) or getattr(
        app_state, "config", None
    )
    if cfg is None:
        raise InvalidException(message="Config is not available")
    data = await request.json()
    updates: Dict[str, Any] = {}
    for k, v in data.items():
        if k in WHITELIST_CONFIG_FIELDS:
            updates[k] = coerce_value_by_field(k, v)
    for k, v in updates.items():
        setattr(cfg, k, v)
    apply_config_side_effects(updates)
    set_global_config(cfg)
    await _persist_runtime_config(updates)
    logger.info("Applied runtime config updates: %s", sorted(updates))
    return "ok"


async def _persist_runtime_config(updates: Dict[str, Any]) -> None:
    """Store the overrides so they survive a restart.

    Without this the whole endpoint is a scratchpad: ``setattr`` above only
    touches the in-memory ``Config``, so a container rebuild or image upgrade
    reverts every edit to whatever the startup arguments said — silently, which
    is the worst part. ``server.py`` replays these rows right after the database
    is ready.

    **No database means no persistence, not an error.** This router is also
    mounted on the worker (see the security note at the top of this file), where
    ``db.engine`` is None; there the previous in-memory-only behaviour is the
    correct one and must not turn into a 500.

    When a database *is* present, a write failure is surfaced. Swallowing it
    would leave the caller believing the value is saved while the next restart
    quietly discards it — exactly the failure mode this table exists to remove.
    """
    if not updates:
        return

    from gpustack.server import db

    if db.engine is None:
        logger.debug(
            "No database bound (worker mount); runtime config updates stay in memory."
        )
        return

    from gpustack.server.db import async_session
    from gpustack.schemas.runtime_config import RuntimeConfig

    async with async_session() as session:
        for key, value in updates.items():
            existing = await RuntimeConfig.first_by_field(session, "key", key)
            if existing is None:
                await RuntimeConfig.create(session, {"key": key, "value": value})
            else:
                await existing.update(session, {"value": value})
