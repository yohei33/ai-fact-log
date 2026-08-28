"""Ties the deterministic pieces of the Daily AI Fact Refresh Worker
together into one run: load state -> validate the External Fact Worker's
structured result -> write ``daily/`` + ``index/`` -> run the Brain Lab
import -> save run state. See ``__init__.py`` for the package-level
boundaries this orchestrator keeps.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from . import daily_writer, index_writer
from .brainlab_import_wrapper import (
    BrainLabImportOutcome,
    BrainLabImportWrapperError,
    run_brainlab_import,
)
from .run_state import RunHistoryEntry, RunOutcome, RunState, RunStateRepository
from .validator import RejectedCandidate, ValidationOutcome, validate_candidates
from .worker_result_contract import CandidateFact, DailyFactWorkerResult


DEFAULT_BOOTSTRAP_WINDOW_DAYS = 7
DEFAULT_MAX_WINDOW_DAYS = 7


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return f"run_{_utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"


def compute_next_discovery_window(
    state: RunState,
    *,
    today: date,
    latest_existing_daily_date: date | None = None,
    bootstrap_window_days: int = DEFAULT_BOOTSTRAP_WINDOW_DAYS,
    max_window_days: int = DEFAULT_MAX_WINDOW_DAYS,
) -> tuple[date, date]:
    """The [start, end] calendar-date window (inclusive) the External Fact
    Worker should search this run.

    - If this worker has already run before (``state.last_covered_date``
      is known), resume from the day after that -- this is the normal,
      steady-state case and does not consult ``latest_existing_daily_date``
      at all.
    - Otherwise (this worker's very first run, no run state yet) and
      ``latest_existing_daily_date`` is given (the most recent date that
      already has a ``daily/`` file, e.g. from earlier manual entries),
      start the day after that -- "bootstrap from the last known Fact
      forward", not from an arbitrary fixed number of days before today.
    - Otherwise (first run, and no daily files exist at all yet), fall
      back to a flat ``bootstrap_window_days``-day window ending today.

    Every branch is capped at ``max_window_days`` so a long-idle worker,
    or a repository whose last daily file is very old, never silently
    balloons into an unbounded backlog search in one run.
    """

    end_date = today
    if state.last_covered_date is not None:
        start_date = state.last_covered_date + timedelta(days=1)
    elif latest_existing_daily_date is not None:
        start_date = latest_existing_daily_date + timedelta(days=1)
    else:
        start_date = end_date - timedelta(days=bootstrap_window_days - 1)

    if start_date > end_date:
        start_date = end_date

    window_days = (end_date - start_date).days + 1
    if window_days > max_window_days:
        start_date = end_date - timedelta(days=max_window_days - 1)

    return start_date, end_date


@dataclass(frozen=True)
class OrchestratorRunReport:
    run_id: str
    trigger: str
    run_at: datetime
    window_start_date: date
    window_end_date: date
    outcome: RunOutcome
    accepted_event_ids: tuple[str, ...]
    rejected: tuple[RejectedCandidate, ...]
    daily_files_written: tuple[str, ...]
    index_entries_written: int
    brainlab_import: BrainLabImportOutcome | None
    error_type: str | None
    error_message: str | None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "run_at": self.run_at.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "outcome": self.outcome.value,
            "accepted_event_ids": list(self.accepted_event_ids),
            "rejected": [r.to_dict() for r in self.rejected],
            "daily_files_written": list(self.daily_files_written),
            "index_entries_written": self.index_entries_written,
            "brainlab_import": (
                self.brainlab_import.to_dict() if self.brainlab_import else None
            ),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


def _write_audit_copy(run_audit_dir: Path, worker_result: DailyFactWorkerResult) -> None:
    run_audit_dir.mkdir(parents=True, exist_ok=True)
    path = run_audit_dir / f"{worker_result.run_id}_worker_result.json"
    path.write_text(
        json.dumps(worker_result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _group_by_date_and_region(
    candidates: tuple[CandidateFact, ...],
) -> dict[date, dict[str, list[CandidateFact]]]:
    from .validator import target_daily_date_for

    grouped: dict[date, dict[str, list[CandidateFact]]] = defaultdict(lambda: defaultdict(list))
    for candidate in candidates:
        target_date = target_daily_date_for(candidate.fields["captured_at"])
        region = candidate.fields["region"]
        grouped[target_date][region].append(candidate)
    return grouped


def perform_run(
    *,
    ai_fact_log_root: Path,
    personal_brain_lab_root: Path,
    state_repository: RunStateRepository,
    run_audit_dir: Path,
    worker_result: DailyFactWorkerResult,
    skip_brainlab_import: bool = False,
) -> OrchestratorRunReport:
    """Execute exactly one worker run. Never raises for an ordinary
    validation/write/import problem -- those become a reported
    ``RunOutcome`` on the returned report. Only a genuinely unexpected
    internal error is caught, converted into
    ``RunOutcome.FAILED_RUN_ERROR``, and still recorded (never silently
    swallowed, never left unrecorded in run state)."""

    run_at = _utc_now()
    ai_fact_log_root = Path(ai_fact_log_root)
    personal_brain_lab_root = Path(personal_brain_lab_root)

    state = state_repository.load()
    _write_audit_copy(run_audit_dir, worker_result)

    accepted_event_ids: tuple[str, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()
    daily_files_written: tuple[str, ...] = ()
    index_entries_written = 0
    brainlab_outcome: BrainLabImportOutcome | None = None
    error_type: str | None = None
    error_message: str | None = None

    try:
        if worker_result.zero_new_facts:
            outcome = RunOutcome.COMPLETED_NO_NEW_FACTS
        else:
            known_event_ids = index_writer.read_known_event_ids(ai_fact_log_root)
            validation: ValidationOutcome = validate_candidates(
                worker_result.candidate_facts,
                known_event_ids=known_event_ids,
                window_start_date=worker_result.window_start_date,
                window_end_date=worker_result.window_end_date,
            )
            rejected = validation.rejected

            if not validation.accepted:
                outcome = RunOutcome.FAILED_VALIDATION
                error_type = "AllCandidatesRejected"
                error_message = (
                    f"all {len(rejected)} candidate(s) failed validation; "
                    "nothing was written"
                )
            else:
                grouped = _group_by_date_and_region(validation.accepted)
                written_paths: list[str] = []
                total_index_entries = 0
                write_failed = False
                try:
                    for target_date, facts_by_region_lists in sorted(grouped.items()):
                        facts_by_region: Mapping[str, tuple[CandidateFact, ...]] = {
                            region: tuple(facts)
                            for region, facts in facts_by_region_lists.items()
                        }
                        relative_path = daily_writer.write_daily_file(
                            ai_fact_log_root, target_date, facts_by_region
                        )
                        written_paths.append(relative_path)

                        entries = [
                            index_writer.candidate_to_index_entry(
                                candidate, daily_date=target_date.isoformat()
                            )
                            for facts in facts_by_region.values()
                            for candidate in facts
                        ]
                        total_index_entries += index_writer.append_index_entries(
                            ai_fact_log_root, entries
                        )
                except Exception as exc:  # noqa: BLE001 -- must not lose partial progress
                    write_failed = True
                    error_type = type(exc).__name__
                    error_message = (
                        f"writing daily/index failed part-way through this run's "
                        f"candidates: {exc}"
                    )
                finally:
                    daily_files_written = tuple(written_paths)
                    index_entries_written = total_index_entries
                    accepted_event_ids = tuple(
                        c.fields["event_id"] for c in validation.accepted
                    )

                outcome = (
                    RunOutcome.FAILED_WRITE
                    if write_failed
                    else RunOutcome.COMPLETED_WITH_NEW_FACTS
                )

        if outcome == RunOutcome.COMPLETED_WITH_NEW_FACTS and not skip_brainlab_import:
            try:
                brainlab_outcome = run_brainlab_import(
                    personal_brain_lab_root=personal_brain_lab_root,
                    ai_fact_log_root=ai_fact_log_root,
                )
                if not brainlab_outcome.succeeded:
                    outcome = RunOutcome.FAILED_BRAINLAB_IMPORT
                    error_type = "BrainLabImportReportedFailure"
                    error_message = brainlab_outcome.error_message
            except BrainLabImportWrapperError as exc:
                outcome = RunOutcome.FAILED_BRAINLAB_IMPORT
                error_type = type(exc).__name__
                error_message = str(exc)

    except Exception as exc:  # noqa: BLE001 -- last-resort safety net
        outcome = RunOutcome.FAILED_RUN_ERROR
        error_type = type(exc).__name__
        error_message = str(exc)

    entry = RunHistoryEntry(
        run_id=worker_result.run_id,
        run_at=run_at,
        trigger=worker_result.trigger,
        window_start_date=worker_result.window_start_date,
        window_end_date=worker_result.window_end_date,
        outcome=outcome,
        new_fact_count=len(accepted_event_ids),
        event_ids_written=accepted_event_ids,
        brainlab_import_outcome=(
            "succeeded"
            if brainlab_outcome and brainlab_outcome.succeeded
            else ("failed" if brainlab_outcome else None)
        ),
        error_type=error_type,
        error_message=error_message,
    )
    state_repository.save(state.record_run(entry))

    return OrchestratorRunReport(
        run_id=worker_result.run_id,
        trigger=worker_result.trigger,
        run_at=run_at,
        window_start_date=worker_result.window_start_date,
        window_end_date=worker_result.window_end_date,
        outcome=outcome,
        accepted_event_ids=accepted_event_ids,
        rejected=rejected,
        daily_files_written=daily_files_written,
        index_entries_written=index_entries_written,
        brainlab_import=brainlab_outcome,
        error_type=error_type,
        error_message=error_message,
    )
