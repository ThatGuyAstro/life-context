"""CPU-side validation harness for an explicitly running OpenAI-compatible VLM.

This command never starts, stops, or imports a model runtime.  It only probes an
already-running endpoint, optionally submits benchmark requests, and samples
``nvidia-smi`` for VRAM/utilization evidence.  Live load requires ``--execute``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: Any
    elapsed_ms: float
    error: str | None = None


def _json_request(
    method: str,
    url: str,
    *,
    payload: Any = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> HttpResult:
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    started = time.perf_counter()
    try:
        with urlopen(
            Request(url, data=body, headers=request_headers, method=method), timeout=timeout
        ) as response:
            raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else None
            return HttpResult(response.status, value, (time.perf_counter() - started) * 1000)
    except HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        return HttpResult(error.code, value, (time.perf_counter() - started) * 1000, str(error))
    except (OSError, URLError, TimeoutError) as error:
        return HttpResult(0, None, (time.perf_counter() - started) * 1000, str(error))


def _gpu_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        raw = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=5)
        name, used, total, utilization = [item.strip() for item in raw.strip().splitlines()[0].split(",")]
        return {
            "name": name,
            "memory_used_mib": float(used),
            "memory_total_mib": float(total),
            "utilization_pct": float(utilization),
            "sampled_at": datetime.now(UTC).isoformat(),
        }
    except (OSError, IndexError, ValueError, subprocess.SubprocessError) as error:
        return {"error": str(error), "sampled_at": datetime.now(UTC).isoformat()}


def _strip_v1(url: str) -> str:
    return url.rstrip("/")[: -len("/v1")] if url.rstrip("/").endswith("/v1") else url.rstrip("/")


def _model_urls(base_url: str) -> tuple[str, str]:
    base = base_url.rstrip("/")
    root = _strip_v1(base)
    health = f"{base}/health"
    models = f"{base}/models"
    return health, models


def _context_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def _load_image(path: str | None) -> tuple[str, str, int] | None:
    if not path:
        return None
    image_path = Path(path)
    raw = image_path.read_bytes()
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{media_type};base64,{base64.b64encode(raw).decode()}", media_type, len(raw)


def _chat_payload(model: str, *, image_url: str | None, max_tokens: int, seed: int) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Describe the supplied desktop context in one concise sentence."}
    ]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}})
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": seed,
        "stream": False,
    }


def _run_request(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    result = _json_request("POST", url, payload=payload, timeout=timeout)
    value = result.payload if isinstance(result.payload, dict) else {}
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    timings = value.get("timings") if isinstance(value.get("timings"), dict) else {}
    return {
        "status": result.status,
        "ok": result.status == 200 and result.error is None,
        "elapsed_ms": round(result.elapsed_ms, 3),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "predicted_tokens_per_second": timings.get("predicted_per_second"),
        "error": result.error or (value.get("error") if value else None),
    }


class GpuSampler:
    def __init__(self, interval_s: float = 0.25) -> None:
        self.interval_s = interval_s
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gpu-validation-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(_gpu_snapshot())
            self._stop.wait(self.interval_s)


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    health_url, models_url = _model_urls(args.model_url)
    health = _json_request("GET", health_url, timeout=args.timeout)
    models = _json_request("GET", models_url, timeout=args.timeout)
    result: dict[str, Any] = {
        "model_health": {"status": health.status, "payload": health.payload, "elapsed_ms": health.elapsed_ms},
        "model_list": {"status": models.status, "payload": models.payload, "elapsed_ms": models.elapsed_ms},
        "gpu_before": _gpu_snapshot(),
    }
    if args.mode in {"queue", "full"}:
        context_health = _json_request("GET", _context_url(args.context_url, "health"), timeout=args.timeout)
        context_status = _json_request("GET", _context_url(args.context_url, "status"), timeout=args.timeout)
        result["context_health"] = {"status": context_health.status, "payload": context_health.payload}
        result["context_status"] = {"status": context_status.status, "payload": context_status.payload}
    return result


def _assert_ready(preflight: dict[str, Any], args: argparse.Namespace) -> None:
    model_health = preflight["model_health"]
    models = preflight["model_list"]
    if model_health["status"] != 200 or models["status"] != 200:
        raise RuntimeError("model endpoint is not healthy/listed; refusing benchmark load")
    if args.mode in {"queue", "full"}:
        status = preflight.get("context_status", {}).get("payload") or {}
        model = status.get("model") if isinstance(status, dict) else None
        if not isinstance(model, dict) or not model.get("configured") or model.get("model_status") != "healthy":
            raise RuntimeError("context server model is not configured and healthy; refusing queue load")


def _run_direct(args: argparse.Namespace, image_url: str | None) -> dict[str, Any]:
    url = f"{args.model_url.rstrip('/')}/chat/completions"
    sampler = GpuSampler()
    sampler.start()
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="gpu-validation") as pool:
        futures = [
            pool.submit(
                _run_request,
                url,
                _chat_payload(args.model, image_url=image_url, max_tokens=args.max_tokens, seed=args.seed + index),
                args.timeout,
            )
            for index in range(args.request_count)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed_s = time.perf_counter() - started
    sampler.stop()
    successful = [item for item in results if item["ok"]]
    token_count = sum(item.get("completion_tokens") or 0 for item in successful)
    gpu_samples = [item for item in sampler.samples if "memory_used_mib" in item]
    return {
        "request_count": args.request_count,
        "concurrency": args.concurrency,
        "elapsed_s": round(elapsed_s, 3),
        "throughput_requests_per_second": round(len(successful) / elapsed_s, 3) if elapsed_s else 0,
        "completion_tokens": token_count,
        "throughput_tokens_per_second": round(token_count / elapsed_s, 3) if elapsed_s and token_count else None,
        "failure_count": args.request_count - len(successful),
        "failure_rate": round((args.request_count - len(successful)) / args.request_count, 4),
        "latency_ms": {
            "min": min((item["elapsed_ms"] for item in results), default=None),
            "p50": sorted(item["elapsed_ms"] for item in results)[len(results) // 2] if results else None,
            "max": max((item["elapsed_ms"] for item in results), default=None),
        },
        "gpu": {
            "sample_count": len(gpu_samples),
            "peak_memory_used_mib": max((item["memory_used_mib"] for item in gpu_samples), default=None),
            "peak_utilization_pct": max((item["utilization_pct"] for item in gpu_samples), default=None),
            "samples": gpu_samples,
        },
        "requests": results,
    }


def _queue_record(run_id: str, index: int, image_ref: dict[str, Any] | None) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    event_id = f"gpu-validation:{run_id}:{index}"
    artifacts = [image_ref] if image_ref else []
    return {
        "envelope_id": f"envelope:{event_id}",
        "schema_version": "1",
        "captured_at": now,
        "source_kind": "other",
        "events": [{
            "event_id": event_id,
            "source_id": "gpu-validation",
            "source_kind": "other",
            "event_type": "observed",
            "occurred_at": now,
            "captured_at": now,
            "payload": {"benchmark": run_id, "index": index},
            "artifacts": artifacts,
            "metadata": {"benchmark": "gpu-validation"},
        }],
        "artifacts": artifacts,
        "producer": "scripts/gpu_validation.py",
        "metadata": {"benchmark": "gpu-validation"},
    }


def _run_queue(args: argparse.Namespace, image_ref: dict[str, Any] | None) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    before = _json_request("GET", _context_url(args.context_url, "status"), timeout=args.timeout).payload or {}
    records = [_queue_record(run_id, index, image_ref) for index in range(args.queue_count)]
    started = time.perf_counter()
    ingest = _json_request(
        "POST", _context_url(args.context_url, "ingest/batches"), payload={"records": records}, timeout=args.timeout
    )
    samples: list[dict[str, Any]] = []
    deadline = time.perf_counter() + args.queue_timeout
    while time.perf_counter() < deadline:
        status = _json_request("GET", _context_url(args.context_url, "status"), timeout=args.timeout)
        payload = status.payload if isinstance(status.payload, dict) else {}
        queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
        sample = {key: queue.get(key) for key in ("pending", "processing", "completed", "failed")}
        sample["sampled_at"] = datetime.now(UTC).isoformat()
        samples.append(sample)
        if (sample.get("pending") or 0) == 0 and (sample.get("processing") or 0) == 0:
            break
        time.sleep(0.5)
    after = _json_request("GET", _context_url(args.context_url, "status"), timeout=args.timeout).payload or {}
    before_queue = before.get("queue") if isinstance(before, dict) else {}
    after_queue = after.get("queue") if isinstance(after, dict) else {}
    peak = max(((item.get("pending") or 0) + (item.get("processing") or 0) for item in samples), default=0)
    baseline = (before_queue.get("pending") or 0) + (before_queue.get("processing") or 0)
    return {
        "submitted": args.queue_count,
        "ingest_status": ingest.status,
        "ingest_elapsed_ms": round(ingest.elapsed_ms, 3),
        "queue_growth_peak": peak - baseline,
        "before": before_queue,
        "after": after_queue,
        "samples": samples,
        "failure_count": 0 if ingest.status == 202 else args.queue_count,
        "failure_rate": 0 if ingest.status == 202 else 1,
        "elapsed_s": round(time.perf_counter() - started, 3),
    }


def _upload_image(args: argparse.Namespace, image: tuple[str, str, int] | None) -> dict[str, Any] | None:
    if not image:
        return None
    image_url, media_type, _size = image
    raw = base64.b64decode(image_url.split(",", 1)[1])
    digest = hashlib.sha256(raw).hexdigest()
    request = Request(
        _context_url(args.context_url, f"artifacts/{digest}"),
        data=raw,
        headers={"Content-Type": media_type, "X-Artifact-Sha256": digest},
        method="PUT",
    )
    with urlopen(request, timeout=args.timeout) as response:
        if response.status not in {200, 201}:
            raise RuntimeError(f"artifact upload failed with HTTP {response.status}")
    return {
        "artifact_id": digest,
        "kind": "image",
        "uri": f"/v1/artifacts/{digest}",
        "media_type": media_type,
        "size_bytes": len(raw),
        "sha256": digest,
        "metadata": {"benchmark": "gpu-validation"},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-url", default=os.environ.get("CONTEXT_MODEL_ENDPOINT", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--context-url", default=os.environ.get("CONTEXT_SERVER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--model", default=os.environ.get("CONTEXT_MODEL_NAME", "gemma-4-26b-a4b-it"))
    parser.add_argument("--image", help="Optional local image path for multimodal requests")
    parser.add_argument("--mode", choices=("direct", "queue", "full"), default="full")
    parser.add_argument("--request-count", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--queue-count", type=int, default=1)
    parser.add_argument("--queue-timeout", type=float, default=180.0)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="Actually send load; without this, only print the plan")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.request_count < 1 or args.concurrency < 1 or args.queue_count < 1:
        raise SystemExit("request and concurrency counts must be positive")
    plan = {
        "mode": args.mode,
        "model_url": args.model_url,
        "context_url": args.context_url,
        "model": args.model,
        "request_count": args.request_count,
        "concurrency": args.concurrency,
        "queue_count": args.queue_count,
        "image": args.image,
        "execute": args.execute,
        "gpu_process_policy": "observation only; this command never launches a model",
    }
    if not args.execute:
        print(json.dumps({"status": "dry_run", "plan": plan}, indent=2))
        return 0
    preflight = _preflight(args)
    _assert_ready(preflight, args)
    image = _load_image(args.image)
    image_url = image[0] if image else None
    report: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "plan": plan, "preflight": preflight}
    if args.mode in {"direct", "full"}:
        report["direct"] = _run_direct(args, image_url)
    if args.mode in {"queue", "full"}:
        image_ref = _upload_image(args, image)
        report["queue"] = _run_queue(args, image_ref)
    report["gpu_after"] = _gpu_snapshot()
    report["finished_at"] = datetime.now(UTC).isoformat()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}), file=sys.stderr)
        raise SystemExit(2) from error
