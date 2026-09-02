"""Runtime config persistence: PUT /v2/config must outlive the process.

The bug this closes: ``set_config`` only ever did ``setattr`` on the in-memory
``Config``, so every container rebuild or image upgrade silently reverted the
admission tuning to whatever the startup arguments said. Nobody found out until
a model started getting 429s at the wrong queue depth.

The two halves are tested separately because they run in different processes in
production — the write happens in a request handler, the replay happens during
server startup — and each half is silent on its own.
"""

import logging
import os

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

import gpustack
from gpustack.routes.config import _persist_runtime_config
from gpustack.schemas.runtime_config import RuntimeConfig
from gpustack.utils.config import (
    WHITELIST_CONFIG_FIELDS,
    apply_config_side_effects,
)

pytestmark = pytest.mark.asyncio

LATENCY = {"ltx2.5-hd": 265, "z-image": 7}
QUEUE_WAIT = {"ltx2.5-hd": 530, "z-image": 25}

MIGRATIONS = os.path.join(os.path.dirname(gpustack.__file__), "migrations")
# Last revision before the fork's own chain. Stamping HERE rather than directly
# in front of the runtime_configs migration keeps ``upgrade(head)`` valid as the
# chain grows: a later revision may touch a table an earlier one creates (the
# assigned_at migration ALTERs video_generation_tasks, created by e1f2a3b4c5d6),
# and stamping past that creation made the upgrade fail with "no such table".
PREVIOUS_REVISION = "c4d7e8f9a0b1"


def _build_table_via_migration(db_path) -> None:
    """Create the table the way an existing deployment gets it: alembic.

    Deliberately **not** ``RuntimeConfig.__table__.create``. That path builds the
    table from the model, so it can never disagree with the model — and would
    therefore hide the one failure this whole fixture exists to catch: a column
    the ORM maps but the migration forgot. Fresh installs do go through
    ``init_db``'s ``create_all``; every *upgraded* database goes through here.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    # Stamp instead of replaying the whole chain: upstream revisions use
    # create_foreign_key, which SQLite cannot do. Everything from
    # PREVIOUS_REVISION on is this fork's, and does replay.
    alembic_command.stamp(cfg, PREVIOUS_REVISION)
    alembic_command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def db_session(tmp_path, monkeypatch):
    """In-memory-ish database wired into ``gpustack.server.db`` the way the real
    server wires it, so ``_persist_runtime_config`` finds an engine."""
    from gpustack.server import db

    db_path = tmp_path / "runtime_config.db"
    _build_table_via_migration(db_path)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(db, "engine", engine)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def _rows(session):
    return {r.key: r.value for r in await RuntimeConfig.all(session)}


async def test_migration_matches_the_orm_model(db_session):
    """The migration must create every column the ORM maps.

    ``RuntimeConfig`` picks up ``created_at`` / ``updated_at`` / ``deleted_at``
    from ``BaseModelMixin`` → ``TimestampsMixin``. Every ORM read is
    ``select(RuntimeConfig)``, which names all of them, so a single missing
    column turns every query into ``no such column`` — on upgraded databases
    only, which is the deployment path that matters. That exact column
    (``deleted_at``) was missing from the first draft of the migration.

    Asserting the whole column set rather than that one name keeps the guard
    useful the next time a field is added to either side.
    """
    result = await db_session.exec(
        RuntimeConfig.__table__.select().limit(0)
    )  # forces the ORM's column list against the real table
    result.close()

    from sqlalchemy import inspect as sa_inspect

    def _columns(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns("runtime_configs")}

    migrated = await db_session.run_sync(lambda s: _columns(s.connection()))
    mapped = set(RuntimeConfig.__table__.columns.keys())
    assert mapped <= migrated, (
        f"migration is missing column(s) the ORM maps: {sorted(mapped - migrated)}; "
        "every select(RuntimeConfig) would fail on an upgraded database"
    )


async def test_persists_dict_valued_fields(db_session):
    """Dict fields must round-trip as JSON, not as an escaped string.

    Storing them as text is what made the env-var route so fragile — a single
    stray character turned the value into an unparseable string and the server
    refused to boot.
    """
    await _persist_runtime_config(
        {
            "lightx2v_model_latency_seconds": LATENCY,
            "lightx2v_model_queue_wait_seconds": QUEUE_WAIT,
        }
    )
    stored = await _rows(db_session)
    assert stored["lightx2v_model_latency_seconds"] == LATENCY
    assert stored["lightx2v_model_queue_wait_seconds"] == QUEUE_WAIT
    # Values must come back as real ints; the admission maths does
    # floor(depth / instances) * latency and would break on strings.
    assert all(
        isinstance(v, int) for v in stored["lightx2v_model_latency_seconds"].values()
    )


async def test_second_save_updates_instead_of_duplicating(db_session):
    """``key`` is unique-indexed; a re-save has to update the existing row."""
    await _persist_runtime_config({"lightx2v_model_latency_seconds": LATENCY})
    await _persist_runtime_config(
        {"lightx2v_model_latency_seconds": {"ltx2.5-hd": 999}}
    )
    rows = await RuntimeConfig.all(db_session)
    assert len(rows) == 1, "re-saving the same field must not insert a second row"
    assert rows[0].value == {"ltx2.5-hd": 999}


async def test_no_database_is_not_an_error(monkeypatch):
    """The /config router is also mounted on the worker, which has no database.

    There the old in-memory-only behaviour is correct; turning it into a 500
    would break worker config entirely.
    """
    from gpustack.server import db

    monkeypatch.setattr(db, "engine", None)
    await _persist_runtime_config({"lightx2v_model_latency_seconds": LATENCY})


async def _run_real_replay(cfg):
    """Drive the actual ``Server._apply_persisted_config``.

    Constructed with ``__new__`` to skip the heavy ``__init__``: the method only
    needs ``self._config``, and going through the real method is the point —
    testing a re-implementation of the replay would not notice if the production
    one stopped calling ``apply_config_side_effects``, which is exactly how the
    ``debug`` regression slipped through review.
    """
    from gpustack.server.server import Server

    server = Server.__new__(Server)
    server._config = cfg
    await server._apply_persisted_config()
    return cfg


async def test_replay_overrides_startup_config(db_session):
    """Startup replay: persisted values win over the startup configuration.

    That ordering is the whole feature — the value an operator set in the UI has
    to still be there after the next upgrade, even though the YAML/CLI still
    carries the old one.
    """
    await _persist_runtime_config({"lightx2v_model_latency_seconds": LATENCY})

    class _Cfg:
        # what --config-file / CLI supplied at boot
        lightx2v_model_latency_seconds = {"ltx2.5-hd": 111}
        lightx2v_admission_enabled = True

    cfg = await _run_real_replay(_Cfg())

    assert cfg.lightx2v_model_latency_seconds == LATENCY, (
        "the persisted override lost to the startup value — an upgrade would "
        "silently revert the operator's tuning, which is the bug this closes"
    )
    # Untouched fields keep their startup value: this is an override layer,
    # not a snapshot of the whole config.
    assert cfg.lightx2v_admission_enabled is True


async def test_replay_restores_the_debug_log_level(db_session):
    """The replay must apply ``debug``'s side effect, not just the attribute.

    ``setattr(cfg, "debug", True)`` alone is not the change an operator asked
    for: ``setup_logging`` cannot raise the level afterwards because
    ``logging.basicConfig`` is a no-op once handlers exist. This drives the real
    ``_apply_persisted_config``, so deleting its ``apply_config_side_effects``
    call fails here — a helper-only test would not have noticed.
    """
    await _persist_runtime_config({"debug": True})

    class _Cfg:
        debug = False  # what the startup arguments said

    root = logging.getLogger()
    original = root.level
    try:
        root.setLevel(logging.INFO)
        cfg = await _run_real_replay(_Cfg())
        assert cfg.debug is True
        assert root.level == logging.DEBUG, (
            "config.debug was restored but the logger stayed at INFO — the "
            "operator's debug logging silently reverts on every restart"
        )
    finally:
        root.setLevel(original)


async def test_side_effect_helper_only_touches_debug():
    """Replaying ``debug`` must also move the root logger.

    ``setattr(cfg, "debug", True)`` alone is not the change an operator asked
    for: ``setup_logging`` cannot raise the level afterwards because
    ``logging.basicConfig`` is a no-op once handlers exist. Both the request
    path and the startup replay therefore go through
    ``apply_config_side_effects``; when only the request path did, a persisted
    ``debug=True`` came back as INFO after every restart.
    """
    root = logging.getLogger()
    original = root.level
    try:
        root.setLevel(logging.INFO)
        apply_config_side_effects({"debug": True})
        assert root.level == logging.DEBUG

        apply_config_side_effects({"debug": False})
        assert root.level == logging.INFO

        # A replay that does not touch debug must leave the level alone.
        root.setLevel(logging.DEBUG)
        apply_config_side_effects({"lightx2v_model_latency_seconds": LATENCY})
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(original)


async def test_non_whitelisted_field_is_not_persisted():
    """``set_config`` filters by whitelist before calling us; nothing outside it
    should ever reach the table (secrets must not be copied into the database)."""
    assert "jwt_secret_key" not in WHITELIST_CONFIG_FIELDS
    assert "bootstrap_password" not in WHITELIST_CONFIG_FIELDS
    assert "huggingface_token" not in WHITELIST_CONFIG_FIELDS
