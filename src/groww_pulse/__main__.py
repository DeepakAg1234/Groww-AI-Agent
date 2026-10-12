"""CLI for a local weekly run, publishing that run, and the local page."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from groww_pulse.config import load_env_file
from groww_pulse.pipeline import STATUS_READY, publish_week, run_week
from groww_pulse.publish.publisher import STATUS_PUBLISHED
from groww_pulse.weeks import parse_iso_week

__all__ = ["main", "parse_iso_week"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="groww_pulse")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "publish"):
        command = subparsers.add_parser(name)
        command.add_argument("--week", required=True, help="ISO week, YYYY-Www")
        command.add_argument(
            "--config",
            default="config.yaml",
            help="Path to config.yaml (default: ./config.yaml)",
        )
    serve = subparsers.add_parser("serve")
    serve.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1). Railway passes 0.0.0.0.",
    )
    serve.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Path(args.config)
    if args.command == "serve":
        return _serve(config, args.host, args.port)
    try:
        parse_iso_week(args.week)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    load_env_file(config.resolve().parent / ".env")
    try:
        if args.command == "publish":
            result = publish_week(config, args.week)
            success = STATUS_PUBLISHED
        else:
            result = run_week(config, args.week)
            success = STATUS_READY
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(result.status)
    if result.status != success:
        return 1
    return 0


def _serve(config: Path, host: str, port: int) -> int:
    """Serve the page. The default bind is localhost; Railway passes 0.0.0.0."""
    load_env_file(config.resolve().parent / ".env")
    import uvicorn

    from groww_pulse.web.app import create_app

    uvicorn.run(create_app(config), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
