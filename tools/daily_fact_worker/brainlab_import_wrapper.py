"""Thin wrapper that invokes Personal Brain Lab's existing, unmodified
``scripts/run_ai_fact_news_import_v0_1.py`` after this worker has
successfully written new Facts to ``ai-fact-log``.

This module contains no import logic of its own -- it never reads
``daily/`` or writes ``data/ai_fact_news_store.json`` directly. It only
runs the existing script as a subprocess (same python3 interpreter this
process is running under, so it works identically whether a venv is
present or not) and reports what happened.

Per this worker's explicit boundary: a Brain Lab import failure here must
**never** roll back or otherwise touch the ai-fact-log write that already
succeeded. The orchestrator calls this only after
``daily_writer``/``index_writer`` have already committed, and always
records the outcome (success or failure) in run state either way -- it
never re-raises in a way that would make the caller think the ai-fact-log
write itself failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


IMPORT_SCRIPT_RELATIVE_PATH = "scripts/run_ai_fact_news_import_v0_1.py"
DEFAULT_TIMEOUT_SECONDS = 120


class BrainLabImportWrapperError(RuntimeError):
    """Raised when the wrapper itself could not even attempt the import
    (script missing, Personal Brain Lab root not found, subprocess could
    not start, or the run timed out) -- distinct from the imported
    script running and reporting failed_count > 0, which is captured in
    ``BrainLabImportOutcome.succeeded = False`` instead of raising."""


@dataclass(frozen=True)
class BrainLabImportOutcome:
    succeeded: bool
    exit_code: int | None
    stdout: str
    stderr: str
    parsed_result: dict | None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "succeeded": self.succeeded,
            "exit_code": self.exit_code,
            "new_count": (self.parsed_result or {}).get("new_count"),
            "updated_count": (self.parsed_result or {}).get("updated_count"),
            "unchanged_count": (self.parsed_result or {}).get("unchanged_count"),
            "failed_count": (self.parsed_result or {}).get("failed_count"),
            "projection_record_count": (self.parsed_result or {}).get(
                "projection_record_count"
            ),
            "error_message": self.error_message,
        }


def run_brainlab_import(
    *,
    personal_brain_lab_root: Path,
    ai_fact_log_root: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> BrainLabImportOutcome:
    root = Path(personal_brain_lab_root)
    script_path = root / IMPORT_SCRIPT_RELATIVE_PATH
    if not root.is_dir():
        raise BrainLabImportWrapperError(
            f"Personal Brain Lab root does not exist or is not a directory: {root}"
        )
    if not script_path.is_file():
        raise BrainLabImportWrapperError(
            f"expected import script not found at {script_path}"
        )

    env = dict(os.environ)
    env["AI_FACT_LOG_ROOT"] = str(Path(ai_fact_log_root))

    try:
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BrainLabImportWrapperError(
            f"Brain Lab import script did not finish within {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise BrainLabImportWrapperError(
            f"could not start Brain Lab import script: {exc}"
        ) from exc

    parsed_result: dict | None = None
    try:
        parsed_result = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        parsed_result = None

    succeeded = completed.returncode == 0
    error_message = None
    if not succeeded:
        error_message = (
            f"import script exited with code {completed.returncode}"
            + (f": {completed.stderr.strip()}" if completed.stderr.strip() else "")
        )

    return BrainLabImportOutcome(
        succeeded=succeeded,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        parsed_result=parsed_result,
        error_message=error_message,
    )
