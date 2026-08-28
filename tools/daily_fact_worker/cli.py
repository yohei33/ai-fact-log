#!/usr/bin/env python3
"""CLI entry point for the Daily AI Fact Refresh Worker's deterministic
half. This script never performs Web Discovery -- it is invoked by an
External Fact Worker (a Claude session with real web search/fetch tools)
in two steps:

1. ``python3 tools/daily_fact_worker/cli.py window`` -- prints the
   Discovery window (start/end calendar dates, inclusive) this run
   should search, computed from the private run state. Run this first,
   every time, rather than guessing a window.
2. After performing real Web Discovery and primary-source verification
   for that window and writing a ``daily_fact_worker_result.v0.1`` JSON
   document (see ``worker_result_contract.py``) -- possibly with
   ``zero_new_facts: true`` if nothing new was found, which is a fully
   valid, expected outcome, never something to pad with weak Facts --
   run:
   ``python3 tools/daily_fact_worker/cli.py run --worker-result-file <path>``
   This validates (fail-closed), writes ``daily/`` + ``index/`` for
   whatever candidates pass, runs the Brain Lab import, and saves run
   state. It prints a JSON run report and exits non-zero only for
   outcomes that mean nothing usable was written
   (``failed_validation``/``failed_write``/``failed_run_error``) --
   ``completed_no_new_facts`` and a Brain-Lab-import-only failure
   (``failed_brainlab_import``, where the ai-fact-log write itself
   already succeeded) both exit 0, since the ai-fact-log system of
   record is in a correct, honest state either way.

This script never runs ``git`` and never stages, commits, or pushes
anything in either repository -- publishing newly written Facts to Git
is left entirely to the repository owner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_AI_FACT_LOG_ROOT_DEFAULT = _PACKAGE_DIR.parent.parent  # tools/daily_fact_worker/ -> tools/ -> repo root
sys.path.insert(0, str(_AI_FACT_LOG_ROOT_DEFAULT))

from tools.daily_fact_worker.orchestrator import (  # noqa: E402
    compute_next_discovery_window,
    perform_run,
)
from tools.daily_fact_worker.run_state import (  # noqa: E402
    AtomicJsonRunStateRepository,
    DEFAULT_STATE_RELATIVE_PATH,
    RunOutcome,
)
from tools.daily_fact_worker.worker_result_contract import (  # noqa: E402
    DailyFactWorkerResult,
    WorkerResultContractError,
)
from tools.daily_fact_worker.daily_writer import find_latest_daily_file_date  # noqa: E402


_FAILING_OUTCOMES = frozenset(
    {
        RunOutcome.FAILED_VALIDATION,
        RunOutcome.FAILED_WRITE,
        RunOutcome.FAILED_RUN_ERROR,
    }
)

DEFAULT_RUN_AUDIT_RELATIVE_DIR = "var/daily_fact_worker/runs"
PERSONAL_BRAIN_LAB_ROOT_ENV_VAR = "PERSONAL_BRAIN_LAB_ROOT"


def _resolve_ai_fact_log_root(args: argparse.Namespace) -> Path:
    if args.ai_fact_log_root:
        return Path(args.ai_fact_log_root).resolve()
    env_value = os.environ.get("AI_FACT_LOG_ROOT")
    if env_value:
        return Path(env_value).resolve()
    return _AI_FACT_LOG_ROOT_DEFAULT.resolve()


def _resolve_personal_brain_lab_root(args: argparse.Namespace, ai_fact_log_root: Path) -> Path:
    if args.personal_brain_lab_root:
        return Path(args.personal_brain_lab_root).resolve()
    env_value = os.environ.get(PERSONAL_BRAIN_LAB_ROOT_ENV_VAR)
    if env_value:
        return Path(env_value).resolve()
    # Default assumption: the two repositories are sibling checkouts
    # under the same parent directory (true for this deployment: both
    # live directly under the user's Documents folder). Always
    # overridable with --personal-brain-lab-root or
    # PERSONAL_BRAIN_LAB_ROOT for any other layout, including whatever a
    # Scheduled Task's own runtime resolves paths to.
    return (ai_fact_log_root.parent / "PersonalBrain_Lab").resolve()


def _cmd_window(args: argparse.Namespace) -> int:
    ai_fact_log_root = _resolve_ai_fact_log_root(args)
    state_path = Path(args.state_file) if args.state_file else (
        ai_fact_log_root / DEFAULT_STATE_RELATIVE_PATH
    )
    state = AtomicJsonRunStateRepository(state_path).load()
    today = date.fromisoformat(args.today) if args.today else date.today()
    latest_existing_daily_date = find_latest_daily_file_date(ai_fact_log_root)
    start_date, end_date = compute_next_discovery_window(
        state, today=today, latest_existing_daily_date=latest_existing_daily_date
    )
    print(
        json.dumps(
            {
                "window_start_date": start_date.isoformat(),
                "window_end_date": end_date.isoformat(),
                "last_covered_date": (
                    state.last_covered_date.isoformat() if state.last_covered_date else None
                ),
                "latest_existing_daily_file_date": (
                    latest_existing_daily_date.isoformat() if latest_existing_daily_date else None
                ),
                "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
                "last_run_outcome": (
                    state.last_run_outcome.value if state.last_run_outcome else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ai_fact_log_root = _resolve_ai_fact_log_root(args)
    personal_brain_lab_root = _resolve_personal_brain_lab_root(args, ai_fact_log_root)
    state_path = Path(args.state_file) if args.state_file else (
        ai_fact_log_root / DEFAULT_STATE_RELATIVE_PATH
    )
    run_audit_dir = (
        Path(args.run_audit_dir)
        if args.run_audit_dir
        else ai_fact_log_root / DEFAULT_RUN_AUDIT_RELATIVE_DIR
    )

    try:
        worker_result = DailyFactWorkerResult.from_json_file(args.worker_result_file)
    except (WorkerResultContractError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "stage": "worker_result_parsing",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    report = perform_run(
        ai_fact_log_root=ai_fact_log_root,
        personal_brain_lab_root=personal_brain_lab_root,
        state_repository=AtomicJsonRunStateRepository(state_path),
        run_audit_dir=run_audit_dir,
        worker_result=worker_result,
        skip_brainlab_import=args.skip_brainlab_import,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 1 if report.outcome in _FAILING_OUTCOMES else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ai-fact-log-root",
        default=None,
        help="ai-fact-log repository root (default: AI_FACT_LOG_ROOT env var, "
        "else this script's own repository root)",
    )
    parser.add_argument(
        "--personal-brain-lab-root",
        default=None,
        help=f"Personal Brain Lab repository root (default: "
        f"{PERSONAL_BRAIN_LAB_ROOT_ENV_VAR} env var, else a sibling "
        f"'PersonalBrain_Lab' directory next to ai-fact-log)",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=f"override the run-state JSON path (default: ai-fact-log-root/"
        f"{DEFAULT_STATE_RELATIVE_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    window_parser = subparsers.add_parser(
        "window", help="print the Discovery window this run should search"
    )
    window_parser.add_argument(
        "--today",
        default=None,
        help="override 'today' for the window calculation (YYYY-MM-DD, testing only)",
    )
    window_parser.set_defaults(func=_cmd_window)

    run_parser = subparsers.add_parser(
        "run", help="validate + write + import one worker result"
    )
    run_parser.add_argument(
        "--worker-result-file",
        required=True,
        help="path to a daily_fact_worker_result.v0.1 JSON document",
    )
    run_parser.add_argument(
        "--run-audit-dir",
        default=None,
        help="override the private per-run audit directory",
    )
    run_parser.add_argument(
        "--skip-brainlab-import",
        action="store_true",
        help="write ai-fact-log only; do not invoke the Brain Lab importer "
        "(useful for the Scheduled Capability Gate check)",
    )
    run_parser.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
