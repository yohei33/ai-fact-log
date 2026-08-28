"""``daily_fact_worker_result.v0.1`` -- the structured handoff between the
External Fact Worker (a Claude session doing real Web Discovery and
primary-source verification for one run) and this package's deterministic
validate/write/import pipeline.

This module only parses and serializes the *shape* of that handoff. It
deliberately does not enforce business rules (enum membership, event_id
uniqueness, URL scheme, date consistency with the target daily file) --
that fail-closed checking lives in ``validator.py`` so this contract stays
a thin, reusable "did the JSON have the right shape" layer, the same
separation Personal Brain Lab keeps between a contract module and an
importer/validator module.

A ``CandidateFact`` carries **exactly** the 11 ai-fact-log Public Fact
keys (see ``schema/fact-schema.md`` / ``REQUIRED_PUBLIC_FACT_FIELDS``
below) -- nothing more. Any worker-side reasoning, source snippets, or
confidence notes the External Fact Worker wants to keep for its own
record belongs in ``DailyFactWorkerResult.discovery_notes`` (free text,
never written to ``daily/`` or ``index/``), never smuggled onto a
``CandidateFact`` as an extra field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping


WORKER_RESULT_CONTRACT_VERSION = "daily_fact_worker_result.v0.1"

# Identical set and order to
# brainlab_contracts.ai_fact_news_v0_1.REQUIRED_FACT_FIELDS on the
# Personal Brain Lab side, and to ai-fact-log/schema/fact-schema.md's
# "必須キー". Duplicated here (not imported) deliberately: this package
# must keep working even if Personal Brain Lab is temporarily unreachable
# (see brainlab_import_wrapper.py), and it must never silently drift from
# the public schema, so the exact tuple is asserted against in tests.
REQUIRED_PUBLIC_FACT_FIELDS = (
    "event_id",
    "title",
    "fact",
    "organization",
    "region",
    "category",
    "published_at",
    "captured_at",
    "source_type",
    "source_url",
    "verification",
)


class WorkerResultContractError(ValueError):
    """Raised when a worker-result JSON document does not have the shape
    this contract requires. This is a *shape* error, not a business-rule
    rejection -- see ``validator.ValidationError`` for those."""


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerResultContractError(f"{path} must be a non-empty string")
    return value


def _require_date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise WorkerResultContractError(f"{path} must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkerResultContractError(f"{path} must be a valid YYYY-MM-DD date") from exc


def _require_datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkerResultContractError(f"{path} must be an ISO 8601 datetime string")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkerResultContractError(f"{path} must be a valid ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise WorkerResultContractError(f"{path} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CandidateFact:
    """One candidate Fact the External Fact Worker found and verified
    against a primary source this run. Carries exactly the 11 public
    Fact fields, as plain JSON-compatible values (strings or ``None`` for
    ``published_at``) -- the same wire shape ai-fact-log's own fenced YAML
    uses, so no lossy re-encoding happens between "what the worker
    verified" and "what eventually gets written to disk"."""

    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise WorkerResultContractError("candidate_facts[].fields must be an object")
        keys = set(self.fields.keys())
        required = set(REQUIRED_PUBLIC_FACT_FIELDS)
        missing = required - keys
        extra = keys - required
        if missing:
            raise WorkerResultContractError(
                "candidate fact is missing required field(s): "
                + ", ".join(sorted(missing))
            )
        if extra:
            raise WorkerResultContractError(
                "candidate fact has field(s) outside the ai-fact-log public "
                "schema (worker reasoning/notes do not belong here): "
                + ", ".join(sorted(extra))
            )

    def ordered_dict(self) -> dict[str, Any]:
        """The 11 fields in the canonical schema order, ready for the YAML
        emitter in ``daily_writer.py``."""

        return {key: self.fields[key] for key in REQUIRED_PUBLIC_FACT_FIELDS}

    def to_dict(self) -> dict[str, Any]:
        return self.ordered_dict()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateFact":
        if not isinstance(value, Mapping):
            raise WorkerResultContractError("each candidate_facts[] entry must be an object")
        return cls(fields=dict(value))


@dataclass(frozen=True)
class DailyFactWorkerResult:
    """The full structured handoff for one worker run."""

    run_id: str
    trigger: str
    generated_at: datetime
    window_start_date: date
    window_end_date: date
    discovery_notes: str
    zero_new_facts: bool
    candidate_facts: tuple[CandidateFact, ...] = field(default_factory=tuple)
    contract_version: str = WORKER_RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.window_start_date > self.window_end_date:
            raise WorkerResultContractError(
                "window_start_date must not be after window_end_date"
            )
        if self.zero_new_facts and self.candidate_facts:
            raise WorkerResultContractError(
                "zero_new_facts=true is inconsistent with a non-empty "
                "candidate_facts list -- a run that found candidates is "
                "not a zero-new-facts run"
            )
        if not self.zero_new_facts and not self.candidate_facts:
            raise WorkerResultContractError(
                "candidate_facts is empty but zero_new_facts is not true -- "
                "a genuinely empty result must say so explicitly"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "generated_at": self.generated_at.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "discovery_notes": self.discovery_notes,
            "zero_new_facts": self.zero_new_facts,
            "candidate_facts": [c.to_dict() for c in self.candidate_facts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DailyFactWorkerResult":
        if not isinstance(value, Mapping):
            raise WorkerResultContractError("worker result must be a JSON object")
        if value.get("contract_version") != WORKER_RESULT_CONTRACT_VERSION:
            raise WorkerResultContractError(
                f"contract_version must be {WORKER_RESULT_CONTRACT_VERSION!r}"
            )
        run_id = _require_str(value.get("run_id"), "run_id")
        trigger = _require_str(value.get("trigger"), "trigger")
        generated_at = _require_datetime(value.get("generated_at"), "generated_at")
        window_start_date = _require_date(value.get("window_start_date"), "window_start_date")
        window_end_date = _require_date(value.get("window_end_date"), "window_end_date")
        discovery_notes = value.get("discovery_notes")
        if not isinstance(discovery_notes, str):
            raise WorkerResultContractError("discovery_notes must be a string (may be empty)")
        zero_new_facts = value.get("zero_new_facts")
        if not isinstance(zero_new_facts, bool):
            raise WorkerResultContractError("zero_new_facts must be a boolean")
        raw_candidates = value.get("candidate_facts")
        if not isinstance(raw_candidates, list):
            raise WorkerResultContractError("candidate_facts must be an array")
        candidates = tuple(CandidateFact.from_dict(c) for c in raw_candidates)
        return cls(
            run_id=run_id,
            trigger=trigger,
            generated_at=generated_at,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
            discovery_notes=discovery_notes,
            zero_new_facts=zero_new_facts,
            candidate_facts=candidates,
        )

    @classmethod
    def from_json_file(cls, path: str) -> "DailyFactWorkerResult":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))
