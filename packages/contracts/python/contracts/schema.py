"""JSON Schema export helpers for the shared contract models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import BaseModel

from .models import (
    ArtifactRef,
    BacklogStatus,
    CaptureEnvelope,
    HealthSnapshot,
    ObservationV1,
    QueryRequest,
    QueryResponse,
    SourceEvent,
)


ContractModelType: TypeAlias = type[BaseModel]

CONTRACT_MODELS: tuple[ContractModelType, ...] = (
    CaptureEnvelope,
    ArtifactRef,
    SourceEvent,
    ObservationV1,
    QueryRequest,
    QueryResponse,
    BacklogStatus,
    HealthSnapshot,
)


def model_json_schema(model: ContractModelType) -> dict[str, Any]:
    """Return the canonical serialization schema for one contract model."""

    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("model must be a Pydantic BaseModel subclass")
    return model.model_json_schema(
        by_alias=True,
        mode="serialization",
        ref_template="#/$defs/{model}",
    )


json_schema_for = model_json_schema


def write_json_schema(model: ContractModelType, destination: str | Path) -> Path:
    """Write one model schema to ``destination`` and return its resolved path."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model_json_schema(model), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def export_json_schemas(directory: str | Path) -> dict[str, Path]:
    """Write every public model schema into ``directory``.

    Files use the stable ``<ModelName>.schema.json`` naming convention.  The
    returned mapping makes the generated artifact locations explicit to callers.
    """

    output_directory = Path(directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    return {
        model.__name__: write_json_schema(
            model,
            output_directory / f"{model.__name__}.schema.json",
        )
        for model in CONTRACT_MODELS
    }


def export_schema(
    model: ContractModelType,
    destination: str | Path | None = None,
) -> dict[str, Any] | Path:
    """Return a schema, or write it when ``destination`` is provided."""

    if destination is None:
        return model_json_schema(model)
    return write_json_schema(model, destination)


export_all_schemas = export_json_schemas


__all__ = [
    "CONTRACT_MODELS",
    "export_all_schemas",
    "export_json_schemas",
    "export_schema",
    "json_schema_for",
    "model_json_schema",
    "write_json_schema",
]
