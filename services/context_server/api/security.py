"""Small opt-in authentication boundary for self-hosted deployments."""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request, WebSocket


def _is_loopback_host(value: object) -> bool:
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_request_auth(request: Request) -> None:
    settings = request.app.state.settings
    is_loopback = _is_loopback_host(getattr(settings, "host", "127.0.0.1"))
    if not is_loopback and getattr(settings, "require_https_remote", True):
        if request.url.scheme != "https":
            raise HTTPException(
                status_code=400,
                detail="remote self-hosted connections require HTTPS",
            )
    token = str(getattr(settings, "auth_token", "") or "")
    if not is_loopback and not token:
        raise HTTPException(
            status_code=401,
            detail="remote self-hosted connections require bearer authentication",
        )
    if token:
        authorization = request.headers.get("authorization", "")
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bearer authentication required")


def require_websocket_auth(websocket: WebSocket) -> None:
    """Apply the same boundary to the WebSocket event stream."""

    settings = websocket.app.state.settings
    is_loopback = _is_loopback_host(getattr(settings, "host", "127.0.0.1"))
    scheme = str(getattr(websocket.url, "scheme", "ws")).lower()
    if not is_loopback and getattr(settings, "require_https_remote", True):
        if scheme not in {"wss", "https"}:
            raise HTTPException(status_code=400, detail="remote event streams require WSS")
    token = str(getattr(settings, "auth_token", "") or "")
    if not is_loopback and not token:
        raise HTTPException(
            status_code=401,
            detail="remote self-hosted connections require bearer authentication",
        )
    if token and websocket.headers.get("authorization", "") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bearer authentication required")


__all__ = ["require_request_auth", "require_websocket_auth"]
