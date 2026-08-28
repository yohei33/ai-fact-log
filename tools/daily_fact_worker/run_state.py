"""Private, git-ignored run-state for the Daily AI Fact Refresh Worker.

Mirrors the shape of Personal Brain Lab's
``brainlab_pipeline.ai_fact_news_importer_v0_1.AtomicJsonImporterCheckpointRepository``
(temp file + same-directory ``os.replace`` atomic write, one lock for the
whole read-modify-write sequence) but tracks a different thing: not a
per-file content-hash cursor, but "which calendar dates has the Discovery
step of this worker already covered, and what happened on the last run",
so a later run (possibly a fresh Claude session with no memory of this
one) can answer "did it actually run yesterday?", "was there just nothing
new?", and "where do I resume from?" without guessing.

This module never touches ``daily/`` or ``index/events_index.jsonl`` --
it only reads/writes its own state file under this repository's
git-ignored ``var/daily_fact_worker/`` directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


RUN_STATE_VERSION = "daily_fact_worker_run_state.v0.1"
DEFAULT_STATE_RELATIVE_PATH = "var/daily_fact_worker/run_state.json"

# Maximum number of past runs kept in run_history. Older entries are
# dropped from the *state* file (not from the per-run audit JSON files,
# which are kept individually under var/daily_fact_worker/runs/ and are
# never pruned by this module).
MAX_RUN_HISTORY_ENTRIES = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_optional_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _parse_optional_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


class RunOutcome(str, Enum):
    """What happened on one worker run. Never silently collapsed into a
    generic "success"/"failure" boolean -- the whole point of this state
    is to let a person or a later run tell these apart precisely."""

    COMPLETED_WITH_NEW_FACTS = "completed_with_new_facts"
    COMPLETED_NO_NEW_FACTS = "completed_no_new_facts"
    FAILED_VALIDATION = "failed_validation"
    FAILED_WRITE = "failed_write"
    FAILED_BRAINLAB_IMPORT = "failed_brainlab_import"
    FAILED_RUN_ERROR = "failed_run_error"


# Outcomes after which the run's discovery window is considered fully
# and honestly covered -- i.e. it is safe to resume the *next* run's
# window from this run's window_end_date + 1 day.
#
# FAILED_BRAINLAB_IMPORT is deliberately included here even though it is
# a "failed" outcome: the ai-fact-log write (the system of record) has
# already genuinely succeeded by the time the Brain Lab import step could
# fail, and that write must never be rolled back (see
# brainlab_import_wrapper.py). Excluding it would make the *next* run
# re-discover and re-offer the same, already-written event_ids, which
# validator.py would then correctly reject as
# ``event_id_already_exists`` -- turning an honest "source write ok,
# downstream projection sync failed" state into a confusing, misleading
# FAILED_VALIDATION on the next run. A validation or write failure, by
# contrast, means nothing was safely committed to ai-fact-log, so those
# outcomes deliberately do NOT advance the covered date -- that run's
# window is retried, not skipped, on the next invocation.
WINDOW_COVERED_OUTCOMES = frozenset(
    {
        RunOutcome.COMPLETED_WITH_NEW_FACTS,
        RunOutcome.COMPLETED_NO_NEW_FACTS,
        RunOutcome.FAILED_BRAINLAB_IMPORT,
    }
)


@dataclass(frozen=True)
class RunHistoryEntry:
    run_id: str
    run_at: datetime
    trigger: str
    window_start_date: date
    window_end_date: date
    outcome: RunOutcome
    new_fact_count: int
    event_ids_written: tuple[str, ...]
    brainlab_import_outcome: str | None
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_at": _iso(self.run_at),
            "trigger": self.trigger,
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "outcome": self.outcome.value,
            "new_fact_count": self.new_fact_count,
            "event_ids_written": list(self.event_ids_written),
            "brainlab_import_outcome": self.brainlab_import_outcome,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunHistoryEntry":
        return cls(
            run_id=str(value["run_id"]),
            run_at=_parse_optional_iso(value.get("run_at")) or _utc_now(),
            trigger=str(value.get("trigger") or "unknown"),
            window_start_date=_parse_optional_date(value["window_start_date"]),
            window_end_date=_parse_optional_date(value["window_end_date"]),
            outcome=RunOutcome(value["outcome"]),
            new_fact_count=int(value.get("new_fact_count", 0)),
            event_ids_written=tuple(value.get("event_ids_written", []) or []),
            brainlab_import_outcome=value.get("brainlab_import_outcome"),
            error_type=value.get("error_type"),
            error_message=value.get("error_message"),
        )


@dataclass
class RunState:
    """Everything the worker needs to resume correctly, and everything a
    person needs to see "did this actually run, and what happened"
    without reading logs."""

    last_covered_date: date | None = None
    last_run_at: datetime | None = None
    last_run_trigger: str | None = None
    last_run_outcome: RunOutcome | None = None
    last_successful_run_at: datetime | None = None
    run_history: tuple[RunHistoryEntry, ...] = field(default_factory=tuple)
    state_version: str = RUN_STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_version": self.state_version,
            "last_covered_date": (
                self.last_covered_date.isoformat() if self.last_covered_date else None
            ),
            "last_run_at": _iso(self.last_run_at),
            "last_run_trigger": self.last_run_trigger,
            "last_run_outcome": (
                self.last_run_outcome.value if self.last_run_outcome else None
            ),
            "last_successful_run_at": _iso(self.last_successful_run_at),
            "run_history": [entry.to_dict() for entry in self.run_history],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunState":
        if value.get("state_version") != RUN_STATE_VERSION:
            raise ValueError(f"state_version must be {RUN_STATE_VERSION}")
        raw_outcome = value.get("last_run_outcome")
        history_raw = value.get("run_history", [])
        if not isinstance(history_raw, list):
            raise ValueError("run_history must be an array")
        return cls(
            last_covered_date=_parse_optional_date(value.get("last_covered_date")),
            last_run_at=_parse_optional_iso(value.get("last_run_at")),
            last_run_trigger=value.get("last_run_trigger"),
            last_run_outcome=RunOutcome(raw_outcome) if raw_outcome else None,
            last_successful_run_at=_parse_optional_iso(
                value.get("last_successful_run_at")
            ),
            run_history=tuple(RunHistoryEntry.from_dict(e) for e in history_raw),
        )

    def record_run(self, entry: RunHistoryEntry) -> "RunState":
        """Return a new RunState reflecting one more completed run.
        Advances ``last_covered_date`` only for outcomes in
        ``SUCCESSFUL_OUTCOMES`` -- a failed run leaves the resume point
        exactly where it was so the next run retries the same window
        rather than silently skipping past uncovered dates."""

        new_history = (entry,) + self.run_history
        if len(new_history) > MAX_RUN_HISTORY_ENTRIES:
            new_history = new_history[:MAX_RUN_HISTORY_ENTRIES]

        new_covered = self.last_covered_date
        if entry.outcome in WINDOW_COVERED_OUTCOMES:
            if new_covered is None or entry.window_end_date > new_covered:
                new_covered = entry.window_end_date

        new_last_successful = self.last_successful_run_at
        if entry.outcome in (
            RunOutcome.COMPLETED_WITH_NEW_FACTS,
            RunOutcome.COMPLETED_NO_NEW_FACTS,
        ):
            new_last_successful = entry.run_at

        return RunState(
            last_covered_date=new_covered,
            last_run_at=entry.run_at,
            last_run_trigger=entry.trigger,
            last_run_outcome=entry.outcome,
            last_successful_run_at=new_last_successful,
            run_history=new_history,
        )


class RunStateRepository(Protocol):
    def load(self) -> RunState: ...

    def save(self, state: RunState) -> None: ...


class InMemoryRunStateRepository:
    """Test/debug repository; no filesystem access."""

    def __init__(self, initial: RunState | None = None):
        self._state = initial or RunState()

    def load(self) -> RunState:
        return RunState.from_dict(self._state.to_dict())

    def save(self, state: RunState) -> None:
        self._state = RunState.from_dict(state.to_dict())


class AtomicJsonRunStateRepository:
    """Real, git-ignored runtime state file. Same atomic-write posture as
    Personal Brain Lab's ``AtomicJsonImporterCheckpointRepository``: temp
    file in the same directory + ``os.replace``, one lock for the whole
    read-modify-write sequence, never a partial write left on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> RunState:
        with self._lock:
            if not self.path.exists():
                return RunState()
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return RunState.from_dict(data)

    def save(self, state: RunState) -> None:
        payload = (
            json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
