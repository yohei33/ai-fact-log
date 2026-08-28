"""Strict, fail-closed validation of ``CandidateFact`` entries before they
are ever allowed to reach ``daily_writer.py`` / ``index_writer.py``.

This module enforces two layers of rules:

1. The exact rules ``ai-fact-log/schema/fact-schema.md`` documents for a
   Public Fact (enum membership, ``event_id`` format, https URLs, date
   consistency between ``event_id``/``published_at``/``captured_at``).
2. This worker's own, *stricter* policy for what it is allowed to write
   automatically (documented in ``docs/DAILY_AI_FACT_WORKER_V0_1.md``):
   only ``VERIFIED_PRIMARY`` / ``VERIFIED_PRIMARY_ARCHIVED`` Facts, never
   ``SECONDARY`` or ``UNVERIFIED`` -- a Fact the External Fact Worker
   could not verify against a primary source is not written at all, never
   padded in as a weaker record.

Validation is **per-candidate**, not all-or-nothing for the whole run:
one malformed or unverifiable candidate is rejected and reported, but
does not block other, valid candidates from being written -- the same
"isolate, never let one bad record silently take down the rest, and
never let one bad record silently corrupt a good one" posture Personal
Brain Lab's own Importer uses (see
``brainlab_pipeline/ai_fact_news_importer_v0_1.py``). A run in which
*every* offered candidate fails validation is not the same thing as a
genuine zero-new-facts day, and the orchestrator treats it as a failed
run (``RunOutcome.FAILED_VALIDATION``), never as silently equivalent to
"nothing new today".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from .worker_result_contract import CandidateFact, REQUIRED_PUBLIC_FACT_FIELDS


ALLOWED_REGIONS = frozenset({"US", "CN", "JP", "GLOBAL", "OTHER"})
ALLOWED_SOURCE_TYPES = frozenset(
    {
        "official_announcement",
        "official_blog",
        "official_documentation",
        "official_github",
        "paper",
        "government",
        "primary_other",
        "secondary",
    }
)
# All four values are valid ai-fact-log schema values (see
# schema/fact-schema.md); this worker additionally restricts which of
# them it is willing to *auto-write* to the two "verified against a
# primary source" values. SECONDARY/UNVERIFIED Facts, if a person wants
# to record them, remain a manual daily-file edit outside this worker.
ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE = frozenset(
    {"VERIFIED_PRIMARY", "VERIFIED_PRIMARY_ARCHIVED"}
)

_EVENT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-(\d{8})-(\d{3})$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ValidationError(ValueError):
    """One candidate's rejection reason. Never raised to abort a whole
    run -- always caught per-candidate by ``validate_candidates`` and
    turned into a ``RejectedCandidate``."""


@dataclass(frozen=True)
class RejectedCandidate:
    candidate: CandidateFact
    reason_code: str
    message: str

    def to_dict(self) -> dict:
        event_id = self.candidate.fields.get("event_id")
        return {
            "event_id": event_id if isinstance(event_id, str) else None,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: tuple[CandidateFact, ...]
    rejected: tuple[RejectedCandidate, ...]


def _fail(reason_code: str, message: str) -> None:
    raise ValidationError(f"{reason_code}: {message}")


def _parse_captured_at(value: str) -> datetime:
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        _fail("captured_at_invalid", "captured_at is not a valid ISO 8601 datetime")
    if parsed.tzinfo is None:
        _fail("captured_at_no_timezone", "captured_at must include a timezone offset")
    return parsed


def target_daily_date_for(captured_at_value: str) -> date:
    """The calendar date this Fact's ``captured_at`` belongs to, in
    ``captured_at``'s own recorded offset (never converted to UTC first)
    -- this is the date ``daily_writer.py`` targets, matching
    fact-schema.md's "パスの日付は原則としてcaptured_atの暦日と一致させる"."""

    return _parse_captured_at(captured_at_value).date()


def _validate_one(
    candidate: CandidateFact,
    *,
    known_event_ids: frozenset[str],
    seen_in_run: set[str],
    window_start_date: date,
    window_end_date: date,
) -> None:
    fields = candidate.fields

    # 1. Exact key set (defense in depth -- CandidateFact.__post_init__
    #    already enforces this, but a validator that could not stand on
    #    its own would defeat the "fail-closed" intent).
    keys = set(fields.keys())
    required = set(REQUIRED_PUBLIC_FACT_FIELDS)
    if keys != required:
        _fail("key_set_mismatch", "candidate fields do not exactly match the public schema")

    def text(name: str, *, required_field: bool = True) -> str | None:
        value = fields.get(name)
        if value is None:
            if required_field:
                _fail(f"{name}_missing", f"{name} must not be null")
            return None
        if not isinstance(value, str) or not value.strip():
            _fail(f"{name}_empty", f"{name} must be a non-empty string")
        return value.strip()

    event_id = text("event_id") or ""
    title = text("title") or ""
    fact_text = text("fact") or ""
    organization = text("organization") or ""
    region = text("region") or ""
    category = text("category") or ""
    source_type = text("source_type") or ""
    source_url = text("source_url") or ""
    verification = text("verification") or ""
    published_at_raw = fields.get("published_at")
    captured_at_raw = text("captured_at") or ""

    # 2. Enum membership.
    if region not in ALLOWED_REGIONS:
        _fail("region_invalid", f"region {region!r} is not one of {sorted(ALLOWED_REGIONS)}")
    if source_type not in ALLOWED_SOURCE_TYPES:
        _fail(
            "source_type_invalid",
            f"source_type {source_type!r} is not one of {sorted(ALLOWED_SOURCE_TYPES)}",
        )
    if verification not in ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE:
        _fail(
            "verification_not_auto_writable",
            f"verification {verification!r} is not one of "
            f"{sorted(ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE)} -- this worker only "
            "auto-writes Facts verified against a primary source",
        )

    # 3. event_id format: <slug>-<YYYYMMDD>-<NNN>.
    match = _EVENT_ID_RE.match(event_id)
    if not match:
        _fail(
            "event_id_format_invalid",
            f"event_id {event_id!r} does not match "
            "<organization-slug>-<YYYYMMDD>-<NNN>",
        )
    event_id_date_text = match.group(1)
    try:
        event_id_date = date(
            int(event_id_date_text[0:4]),
            int(event_id_date_text[4:6]),
            int(event_id_date_text[6:8]),
        )
    except ValueError:
        _fail("event_id_date_invalid", f"event_id date segment {event_id_date_text!r} is not a real date")

    # 4. source_url must be an absolute https:// URL.
    if not source_url.lower().startswith("https://"):
        _fail("source_url_not_https", "source_url must be an absolute https:// URL")

    # 5. published_at: null, or a date-only / full ISO datetime string.
    published_at_date: date | None = None
    if published_at_raw is not None:
        if not isinstance(published_at_raw, str) or not published_at_raw.strip():
            _fail("published_at_invalid", "published_at must be null or a non-empty string")
        raw = published_at_raw.strip()
        if _DATE_ONLY_RE.match(raw):
            try:
                published_at_date = date.fromisoformat(raw)
            except ValueError:
                _fail("published_at_invalid", "published_at date-only value is not a real date")
        else:
            published_at_date = _parse_captured_at(raw).date()

    # 6. captured_at must parse with a timezone (already required by
    #    ``text()`` returning a non-empty string; re-parse for the date).
    captured_at_date = _parse_captured_at(captured_at_raw).date()

    # 7. event_id date segment must match published_at's date when known,
    #    else captured_at's date (fact-schema.md event_id rule).
    expected_event_id_date = published_at_date if published_at_date is not None else captured_at_date
    if event_id_date != expected_event_id_date:
        _fail(
            "event_id_date_mismatch",
            f"event_id date segment {event_id_date.isoformat()} does not match "
            f"{'published_at' if published_at_date is not None else 'captured_at'} "
            f"date {expected_event_id_date.isoformat()}",
        )

    # 8. captured_at's calendar date must fall inside the declared
    #    Discovery window -- a candidate claiming a capture time outside
    #    the window it says it covers is treated as inconsistent, not
    #    silently accepted.
    if not (window_start_date <= captured_at_date <= window_end_date):
        _fail(
            "captured_at_outside_window",
            f"captured_at date {captured_at_date.isoformat()} is outside the "
            f"declared window [{window_start_date.isoformat()}, {window_end_date.isoformat()}]",
        )

    # 9. category / organization / title / fact non-empty already
    #    enforced by text(); category is intentionally not restricted to
    #    a fixed enum (fact-schema.md: recommended, not exhaustive), but
    #    must not be one of the disallowed placeholder values.
    if category.lower() in {"unknown", "n/a", "none", "tbd"}:
        _fail("category_placeholder", f"category {category!r} looks like a placeholder, not a real category")

    # 10. Uniqueness: not already known (existing index) and not a
    #     duplicate within this same run.
    if event_id in known_event_ids:
        _fail("event_id_already_exists", f"event_id {event_id!r} already exists in index/events_index.jsonl")
    if event_id in seen_in_run:
        _fail("event_id_duplicate_in_run", f"event_id {event_id!r} appears more than once in this run's candidates")


def validate_candidates(
    candidates: Iterable[CandidateFact],
    *,
    known_event_ids: frozenset[str],
    window_start_date: date,
    window_end_date: date,
) -> ValidationOutcome:
    """Validate every candidate independently. Returns which ones may be
    written and which were rejected (with a reason), and never raises for
    a per-candidate problem -- only ``TypeError``/similar for a genuinely
    malformed call is allowed to propagate."""

    accepted: list[CandidateFact] = []
    rejected: list[RejectedCandidate] = []
    seen_in_run: set[str] = set()

    for candidate in candidates:
        try:
            _validate_one(
                candidate,
                known_event_ids=known_event_ids,
                seen_in_run=seen_in_run,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
            )
        except ValidationError as exc:
            message = str(exc)
            reason_code = message.split(":", 1)[0]
            rejected.append(
                RejectedCandidate(candidate=candidate, reason_code=reason_code, message=message)
            )
            continue
        event_id = candidate.fields.get("event_id")
        if isinstance(event_id, str):
            seen_in_run.add(event_id)
        accepted.append(candidate)

    return ValidationOutcome(accepted=tuple(accepted), rejected=tuple(rejected))
