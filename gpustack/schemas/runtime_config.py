from typing import Any, Optional

from sqlalchemy import Column
from sqlmodel import Field, SQLModel

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import JSON


class RuntimeConfig(SQLModel, BaseModelMixin, table=True):
    """A config field that was overridden at runtime through ``PUT /v2/config``.

    Why this table exists: ``Config`` is a pydantic ``BaseSettings`` built once
    at process start from CLI args / ``--config-file`` YAML / env, and
    ``routes/config.py::set_config`` can only ``setattr`` onto that live object.
    Every edit made in the UI therefore died with the process — a container
    rebuild or an image upgrade silently reverted the whole admission
    configuration back to whatever the startup arguments said, with no warning.
    Persisting the overrides here is what makes the UI a real control surface
    instead of a scratchpad.

    **Only explicitly-changed fields get a row.** The table is an override
    layer, not a snapshot of the full config: a field with no row keeps
    resolving through the normal startup chain. That matters for secrets and
    for anything the operator tunes per-deployment — we never copy the whole
    ``Config`` into the database.

    Rows are keyed by the ``Config`` field name and hold the coerced value as
    JSON, so dict-valued fields (``lightx2v_model_latency_seconds`` and
    friends) round-trip without any string escaping — which is exactly the
    class of bug that made the env-var route so fragile.

    ``key`` is a unique-indexed column rather than the primary key: MySQL
    cannot index an unbounded ``TEXT`` primary key, and the surrogate ``id``
    keeps this table shaped like every other table in the schema.
    """

    __tablename__ = 'runtime_configs'

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    value: Optional[Any] = Field(default=None, sa_column=Column(JSON, nullable=True))
