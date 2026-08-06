"""The ``contextctl`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from services.context_server.query.models import QueryRequest, jsonable

from .client import ContextClient, ContextClientError, UrllibContextClient


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")


def _add_subcommand_common_options(parser: argparse.ArgumentParser) -> None:
    """Allow common flags both before and after a subcommand."""

    parser.add_argument("--base-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--pretty", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contextctl", description="Control and query the context server"
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="context server URL (default: CONTEXT_SERVER_URL or localhost:8000)",
    )
    _add_output_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start or resume context processing")
    _add_subcommand_common_options(start)
    start.add_argument("--mode", default=None, help="optional processing mode")

    pause = subparsers.add_parser("pause", help="pause context processing")
    _add_subcommand_common_options(pause)
    stop = subparsers.add_parser("stop", help="stop capture processing (alias for pause)")
    _add_subcommand_common_options(stop)
    health = subparsers.add_parser("health", help="check server liveness and readiness")
    _add_subcommand_common_options(health)
    status = subparsers.add_parser("status", help="show service and index status")
    _add_subcommand_common_options(status)

    query = subparsers.add_parser(
        "query", help="search context with hybrid, FTS, or semantic retrieval"
    )
    _add_subcommand_common_options(query)
    query.add_argument("text", help="natural-language query")
    query.add_argument("--mode", choices=("hybrid", "fts", "semantic"), default="hybrid")
    query.add_argument("--limit", type=int, default=10)
    query.add_argument("--filter", dest="filters", action="append", default=[], metavar="KEY=VALUE")
    query.add_argument("--from", dest="start", default=None)
    query.add_argument("--to", dest="end", default=None)
    query.add_argument("--no-evidence", action="store_true")
    query.add_argument("--evidence-limit", type=int, default=None)
    query.add_argument("--fts-weight", type=float, default=0.5)
    query.add_argument("--semantic-weight", type=float, default=0.5)
    query.add_argument("--explain", action="store_true")

    timeline = subparsers.add_parser("timeline", help="list context chronologically")
    _add_subcommand_common_options(timeline)
    timeline.add_argument("--from", dest="start", default=None)
    timeline.add_argument("--to", dest="end", default=None)
    timeline.add_argument("--limit", type=int, default=None)
    timeline.add_argument("--subject", default=None)
    timeline.add_argument("--kind", default=None)

    purge = subparsers.add_parser("purge", help="purge context data using an explicit policy")
    _add_subcommand_common_options(purge)
    purge.add_argument("--before", default=None, help="remove data before an ISO timestamp")
    purge.add_argument("--source", default=None, help="limit the purge to a source")
    purge.add_argument("--project", default=None, help="limit the purge to a project")
    purge.add_argument("--all", action="store_true", help="select all purgeable context data")
    purge.add_argument(
        "--dry-run", action="store_true", help="report eligible data without deleting"
    )
    purge.add_argument("--yes", action="store_true", help="confirm the destructive operation")

    config = subparsers.add_parser("config", help="read or update context configuration")
    _add_subcommand_common_options(config)
    config.add_argument("key", nargs="?", default=None)
    config.add_argument("value", nargs="?", default=None)
    config.add_argument("--get", dest="get_key", default=None, metavar="KEY")
    config.add_argument("--set", dest="set_pair", nargs=2, default=None, metavar=("KEY", "VALUE"))
    config.add_argument("--show", action="store_true")

    model = subparsers.add_parser(
        "model", help="show or configure the OpenAI-compatible inference model"
    )
    _add_subcommand_common_options(model)
    model.add_argument("--endpoint", default=None, help="local model endpoint URL")
    model.add_argument("--name", default=None, help="model name or served model ID")
    model.add_argument("--revision", default=None, help="model revision or build identifier")
    model.add_argument(
        "--clear", action="store_true", help="clear endpoint, model name, and revision"
    )

    return parser


def _parse_filter(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise ValueError(f"filter must be KEY=VALUE: {value}")
    key, raw = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"filter key must not be empty: {value}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return key, parsed


def _query_payload(args: argparse.Namespace) -> QueryRequest:
    filters = dict(_parse_filter(item) for item in args.filters)
    return QueryRequest.from_value(
        {
            "query": args.text,
            "mode": args.mode,
            "limit": args.limit,
            "filters": filters,
            "start": args.start,
            "end": args.end,
            "include_evidence": not args.no_evidence,
            "evidence_limit": args.evidence_limit,
            "fts_weight": args.fts_weight,
            "semantic_weight": args.semantic_weight,
            "explain": args.explain,
        }
    )


def _timeline_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "start": args.start,
            "end": args.end,
            "limit": args.limit,
            "subject": args.subject,
            "kind": args.kind,
        }.items()
        if value is not None
    }


def _config_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.set_pair:
        key, value = args.set_pair
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        return {"operation": "set", "key": key, "value": value}
    key = args.get_key if args.get_key is not None else args.key
    if key is not None:
        return {"operation": "get", "key": key}
    if args.value is not None:
        return {"operation": "set", "key": args.key, "value": args.value}
    return {"operation": "get"}


def _model_payload(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        key: value
        for key, value in {
            "model_endpoint": args.endpoint,
            "model_name": args.name,
            "model_revision": args.revision,
        }.items()
        if value is not None
    }
    if args.clear and values:
        raise ValueError("model --clear cannot be combined with model fields")
    if args.clear:
        values = {
            "model_endpoint": "",
            "model_name": "",
            "model_revision": "",
        }
    return {"operation": "update", "settings": values} if values else {"operation": "get"}


def _dispatch(args: argparse.Namespace, client: ContextClient) -> Any:
    if args.command == "start":
        options = {"mode": args.mode} if args.mode else {}
        return client.start(options)
    if args.command == "pause":
        return client.pause()
    if args.command == "stop":
        return client.pause()
    if args.command == "health":
        return client.health()
    if args.command == "status":
        return client.status()
    if args.command == "query":
        return client.query(_query_payload(args))
    if args.command == "timeline":
        return client.timeline(_timeline_payload(args))
    if args.command == "purge":
        if not args.yes and not args.dry_run:
            raise ValueError("purge requires --yes confirmation")
        if not args.before and not args.all:
            raise ValueError("purge requires --before TIMESTAMP or --all")
        options = {
            "before": args.before,
            "all": args.all,
            "confirmed": bool(args.yes),
            "dry_run": bool(args.dry_run),
        }
        if args.source is not None:
            options["source"] = args.source
        if args.project is not None:
            options["project"] = args.project
        return client.purge(options)
    if args.command == "config":
        return client.config(_config_payload(args))
    if args.command == "model":
        return client.config(_model_payload(args))
    raise ValueError(f"unknown command: {args.command}")


def _write_result(result: Any, args: argparse.Namespace, stdout: TextIO) -> None:
    value = result.to_dict() if hasattr(result, "to_dict") else jsonable(result)
    if isinstance(value, str) and not args.json and not args.pretty:
        stdout.write(value + "\n")
        return
    indent = 2 if args.pretty else None
    stdout.write(
        json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=args.pretty, default=str)
        + "\n"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client: ContextClient | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run ``contextctl`` and return a process-style exit code."""

    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if client is None:
            if args.base_url:
                client = UrllibContextClient(
                    args.base_url,
                    bearer_token=(
                        os.environ.get("CONTEXT_SERVER_TOKEN")
                        or os.environ.get("CONTEXT_SERVER_AUTH_TOKEN")
                    ),
                )
            else:
                client = UrllibContextClient.from_environment()
        result = _dispatch(args, client)
        _write_result(result, args, stdout)
        return 0
    except (ContextClientError, ValueError, TypeError) as error:
        stderr.write(f"contextctl: error: {error}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
