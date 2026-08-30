"""The ``atlas`` command line interface.

Three verbs, one per surface:

- ``analyze`` -- load a trace, run the pipeline, print the diagnosis. The
  two-minute demo of what Atlas is.
- ``serve`` -- start the API and web UI over a run store.
- ``ask`` -- put a natural-language question to an LLM that may only answer
  from the deterministic query engine (thin optional layer; needs an API key).

stdlib ``argparse`` on purpose: a diagnostic tool's entry point should not
depend on a CLI framework.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atlas.analysis import Verdict, analyze_run
from atlas.ingestion import load_trace
from atlas.report import render_analysis
from atlas.store import RunStore


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="Execution intelligence for agentic AI systems.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser(
        "analyze", help="analyze a MASEF trace and print the diagnosis"
    )
    analyze.add_argument("trace", help="path to a MASEF trace JSON file")
    analyze.add_argument(
        "--format", choices=["text", "json"], default="text", help="output format"
    )
    analyze.add_argument(
        "--verdicts",
        help="optional JSON file seeding failure detection with evaluator "
        "verdicts: a list of {node_id, kind, message, source} objects",
    )
    analyze.add_argument(
        "--store",
        help="also persist the analyzed runs into this directory",
    )
    analyze.set_defaults(handler=_cmd_analyze)

    serve = subparsers.add_parser("serve", help="serve the API and web UI")
    serve.add_argument(
        "--store", default="runs", help="run store directory (default: ./runs)"
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind host")
    serve.add_argument("--port", type=int, default=8000, help="bind port")
    serve.add_argument(
        "--seed",
        action="append",
        default=[],
        metavar="TRACE",
        help="ingest this trace into the store before starting "
        "(repeatable; useful for demos)",
    )
    serve.set_defaults(handler=_cmd_serve)

    ask = subparsers.add_parser(
        "ask", help="ask a natural-language question about a stored run (LLM layer)"
    )
    ask.add_argument("question", help="what to ask, e.g. 'why did this run fail?'")
    ask.add_argument("--run", required=True, help="run id to ask about")
    ask.add_argument("--store", default="runs", help="run store directory")
    ask.set_defaults(handler=_cmd_ask)

    return parser


# ── handlers ──────────────────────────────────────────────────────────


def _cmd_analyze(args: argparse.Namespace) -> int:
    verdicts = _load_verdicts(args.verdicts) if args.verdicts else None
    try:
        runs = load_trace(args.trace)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    store = RunStore(args.store) if args.store else None
    exit_code = 0
    for run in runs:
        analysis = analyze_run(run, verdicts=verdicts)
        if store is not None:
            store.add_run(run, verdicts=verdicts)
        if args.format == "json":
            print(json.dumps(analysis.model_dump(), indent=2))
        else:
            print(render_analysis(analysis), end="")
        if analysis.failures.failures:
            exit_code = 1
    return exit_code


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: serving needs the API extras -- install with "
            "`uv sync --extra api` (or `pip install 'atlas[api]'`)",
            file=sys.stderr,
        )
        return 2

    from atlas.api import create_app

    store = RunStore(args.store)
    for trace_path in args.seed:
        try:
            runs = load_trace(trace_path)
        except (OSError, ValueError) as exc:
            print(f"error: cannot seed {trace_path}: {exc}", file=sys.stderr)
            return 2
        for run in runs:
            if run.id not in store:
                store.add_run(run)

    app = create_app(store)
    print(f"Atlas UI and API on http://{args.host}:{args.port} ({len(store)} run(s))")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    try:
        from atlas.llm import ask
    except ImportError as exc:
        print(
            f"error: the ask command needs the LLM extras -- install with "
            f"`uv sync --extra llm` ({exc})",
            file=sys.stderr,
        )
        return 2

    store = RunStore(args.store)
    try:
        answer = ask(args.question, run_id=args.run, store=store)
    except Exception as exc:  # the LLM layer reports its own actionable errors
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(answer)
    return 0


def _load_verdicts(path: str) -> list[Verdict] | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read verdicts from {path}: {exc}")
    verdicts = [Verdict.model_validate(entry) for entry in raw]
    return verdicts or None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
