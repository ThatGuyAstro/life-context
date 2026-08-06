"""Capture-envelope and artifact ingestion endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..schemas import ArtifactRef, PublicIngestRequest, PublicTimelineResponse
from ..security import require_request_auth

router = APIRouter(tags=["ingest"])


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "context_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="context runtime is not configured")
    return runtime


async def _ingest(
    request: Request,
    payload: Any,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        try:
            payload = model_dump(mode="json", exclude_unset=True)
        except TypeError:
            payload = model_dump(exclude_unset=True)
    try:
        result = _runtime(request).ingest_batch(payload, idempotency_key=idempotency_key)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"status": "accepted", **result}


@router.post(
    "/ingest/batches",
    status_code=202,
    dependencies=[Depends(require_request_auth)],
)
async def ingest_batches(
    request: Request,
    payload: PublicIngestRequest = Body(...),
) -> dict[str, Any]:
    return await _ingest(request, payload, request.headers.get("idempotency-key"))


@router.post(
    "/ingest/batch",
    status_code=202,
    dependencies=[Depends(require_request_auth)],
)
async def ingest_batch_compat(
    request: Request,
    payload: PublicIngestRequest = Body(...),
) -> dict[str, Any]:
    """Compatibility alias for the first collector transport draft."""

    return await _ingest(request, payload, request.headers.get("idempotency-key"))


@router.put(
    "/artifacts/{digest}",
    status_code=201,
    response_model=ArtifactRef,
    dependencies=[Depends(require_request_auth)],
)
async def put_artifact(digest: str, request: Request) -> ArtifactRef:
    body = await request.body()
    supplied = request.headers.get("x-artifact-sha256")
    if supplied and supplied.lower() != digest.lower():
        raise HTTPException(status_code=400, detail="artifact digest header does not match URL")
    metadata: dict[str, Any] = {}
    raw_metadata = request.headers.get("x-artifact-metadata")
    if raw_metadata:
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=400,
                detail="invalid X-Artifact-Metadata JSON",
            ) from error
        if isinstance(parsed, dict):
            metadata = parsed
    try:
        result = _runtime(request).put_artifact(
            digest,
            body,
            media_type=request.headers.get("content-type", "application/octet-stream"),
            metadata=metadata,
            retention_tier=str(metadata.get("retention_tier", "raw")),
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    media_type = result.get("media_type", "application/octet-stream")
    kind = (
        "image"
        if media_type.startswith("image/")
        else "audio"
        if media_type.startswith("audio/")
        else "video"
        if media_type.startswith("video/")
        else "text"
        if media_type.startswith("text/")
        else "document"
        if media_type in {"application/pdf", "application/msword"}
        else "archive"
        if media_type in {"application/zip", "application/gzip", "application/x-tar"}
        else "other"
    )
    return ArtifactRef.model_validate(
        {
            "artifact_id": result["artifact_id"],
            "kind": kind,
            "uri": result.get("uri"),
            "media_type": media_type,
            "size_bytes": result.get("size_bytes"),
            "sha256": result.get("sha256"),
            "metadata": metadata,
        }
    )


@router.get(
    "/artifacts/{digest}",
    dependencies=[Depends(require_request_auth)],
)
async def get_artifact(digest: str, request: Request) -> Any:
    from fastapi.responses import FileResponse

    runtime = _runtime(request)
    record = runtime.storage.get_artifact(digest)
    path = runtime.artifact_path(digest)
    if record is None or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(path, media_type=record.media_type, filename=path.name)


@router.get(
    "/observations",
    response_model=PublicTimelineResponse,
    dependencies=[Depends(require_request_auth)],
)
async def get_observations(request: Request) -> dict[str, Any]:
    return _runtime(request).observations(dict(request.query_params))


__all__ = ["router"]
