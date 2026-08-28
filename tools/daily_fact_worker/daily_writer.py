"""Deterministic writer for ``ai-fact-log/daily/YYYY/MM/YYYY-MM-DD.md``.

Two hard guarantees this module keeps:

1. **Every pre-existing fenced ```yaml``` block's inner text is preserved
   byte-for-byte.** Personal Brain Lab's Importer classifies a Fact as
   ``unchanged``/``updated`` purely from a SHA-256 of that inner text (see
   ``brainlab_pipeline/ai_fact_news_importer_v0_1.py``); if this writer
   ever reformatted an existing block (different quoting, different key
   order, a stray trailing space), an untouched Fact would suddenly look
   "updated" to the Importer for no real reason. New Facts are only ever
   *inserted* -- appended into a region's existing subsections, or used
   to replace that region's ``_No facts recorded._`` placeholder when the
   region was empty -- never mixed into or used to regenerate existing
   subsection text.
2. **Values round-trip as strings.** Every field is emitted as a
   YAML double-quoted scalar (JSON string-escaping is a valid subset of
   YAML double-quoted scalar syntax), even fields that would be "safe"
   unquoted in the common case, specifically so that ``yaml.safe_load``
   can never silently turn e.g. ``published_at: 2026-08-27`` into a
   ``datetime.date`` object instead of the string
   ``AIFactNewsRecord``/``ObservationTime`` expect (see
   ``schema/fact-schema.md``: "YAMLによる暗黙の日付型変換を避けるため、
   日時文字列は引用符で囲む").

Only the region headings ``## US`` / ``## CN`` / ``## JP`` / ``## GLOBAL``
/ ``## OTHER`` (in exactly that order) are understood. A daily file that
does not match this canonical shape is refused rather than guessed at --
this writer fails closed, never attempting speculative text surgery on an
unexpected structure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .worker_result_contract import CandidateFact, REQUIRED_PUBLIC_FACT_FIELDS


REGION_ORDER = ("US", "CN", "JP", "GLOBAL", "OTHER")
NO_FACTS_PLACEHOLDER = "_No facts recorded._"
_HEADING_SPLIT_RE = re.compile(r"\n\n(?=## (?:US|CN|JP|GLOBAL|OTHER)\n)")
_HEADING_LINE_RE = re.compile(r"^## (US|CN|JP|GLOBAL|OTHER)\n\n(.*)$", re.DOTALL)
_SUBSECTION_SPLIT_RE = re.compile(r"\n\n(?=### )")


class DailyWriterError(RuntimeError):
    """Raised when a daily file does not match the canonical shape this
    writer understands, or an internal consistency check fails. Never
    raised for "just append here anyway" -- an unexpected shape is a hard
    stop, surfaced by the orchestrator as a failed run, not guessed at."""


def daily_relative_path_for_date(target_date: date) -> str:
    return (
        f"daily/{target_date.year:04d}/{target_date.month:02d}/"
        f"{target_date.isoformat()}.md"
    )


_DAILY_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")


def find_latest_daily_file_date(ai_fact_log_root: Path) -> date | None:
    """Read-only scan of ``daily/YYYY/MM/YYYY-MM-DD.md`` for the most
    recent date that already has a file, regardless of what it contains.
    Used only to seed a sensible bootstrap Discovery window on this
    worker's very first run (before it has any run state of its own) --
    once ``run_state.json`` exists, the worker's own
    ``last_covered_date`` is the source of truth and this is not
    consulted again. Returns ``None`` for a brand-new repository with no
    daily files at all."""

    daily_root = Path(ai_fact_log_root) / "daily"
    if not daily_root.is_dir():
        return None
    latest: date | None = None
    for path in daily_root.glob("*/*/*.md"):
        match = _DAILY_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            candidate = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_fact_yaml_body(candidate: CandidateFact) -> str:
    fields = candidate.fields
    lines: list[str] = []
    for key in REQUIRED_PUBLIC_FACT_FIELDS:
        value = fields[key]
        if key == "published_at" and value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {_quoted(value)}")
    return "\n".join(lines)


def render_fact_subsection(candidate: CandidateFact) -> str:
    fields = candidate.fields
    organization = fields["organization"]
    title = fields["title"]
    yaml_body = render_fact_yaml_body(candidate)
    return f"### {organization} — {title}\n\n```yaml\n{yaml_body}\n```"


def build_fresh_daily_file(
    target_date: date, new_facts_by_region: Mapping[str, tuple[CandidateFact, ...]]
) -> str:
    """Build a brand-new daily file from scratch, matching
    ``templates/daily-template.md``'s real (non-instructional) shape."""

    frontmatter = (
        "---\n"
        "schema_version: 1\n"
        f'date: "{target_date.isoformat()}"\n'
        "record_format: fenced_yaml\n"
        "---"
    )
    title_line = f"# Daily AI Facts — {target_date.isoformat()}"

    region_blocks = []
    for region in REGION_ORDER:
        candidates = new_facts_by_region.get(region, ())
        if candidates:
            body = "\n\n".join(render_fact_subsection(c) for c in candidates)
        else:
            body = NO_FACTS_PLACEHOLDER
        region_blocks.append(f"## {region}\n\n{body}")

    return "\n\n".join([frontmatter, title_line, *region_blocks]) + "\n"


@dataclass(frozen=True)
class _ParsedDailyFile:
    preamble: str  # frontmatter + title, verbatim, no trailing newline
    subsections_by_region: dict[str, tuple[str, ...]]  # byte-exact existing blobs
    frontmatter_date: str | None


def _parse_existing_daily_file(text: str, *, expected_date: date) -> _ParsedDailyFile:
    first_heading_match = re.search(r"\n\n(?=## US\n)", text)
    if first_heading_match is None:
        raise DailyWriterError(
            "existing daily file does not contain a '## US' heading in the "
            "expected position; refusing to modify an unrecognized shape"
        )
    preamble = text[: first_heading_match.start()]
    rest = text[first_heading_match.end() :]

    date_match = re.search(r'^date:\s*"([^"]+)"\s*$', preamble, re.MULTILINE)
    frontmatter_date = date_match.group(1) if date_match else None
    if frontmatter_date != expected_date.isoformat():
        raise DailyWriterError(
            f"existing daily file's frontmatter date {frontmatter_date!r} does not "
            f"match the target date {expected_date.isoformat()!r}; refusing to modify"
        )

    chunks = _HEADING_SPLIT_RE.split(rest.rstrip("\n"))
    if len(chunks) != len(REGION_ORDER):
        raise DailyWriterError(
            f"expected exactly {len(REGION_ORDER)} region sections "
            f"({', '.join(REGION_ORDER)}) in canonical order, found {len(chunks)}; "
            "refusing to modify an unrecognized shape"
        )

    subsections_by_region: dict[str, tuple[str, ...]] = {}
    for expected_region, chunk in zip(REGION_ORDER, chunks):
        match = _HEADING_LINE_RE.match(chunk)
        if not match or match.group(1) != expected_region:
            raise DailyWriterError(
                f"expected region section '## {expected_region}' at this position; "
                "refusing to modify an unrecognized shape"
            )
        body = match.group(2)
        if body.strip() == NO_FACTS_PLACEHOLDER:
            subsections_by_region[expected_region] = ()
        else:
            subsections_by_region[expected_region] = tuple(
                _SUBSECTION_SPLIT_RE.split(body)
            )

    return _ParsedDailyFile(
        preamble=preamble,
        subsections_by_region=subsections_by_region,
        frontmatter_date=frontmatter_date,
    )


def insert_into_existing_daily_file(
    existing_text: str,
    target_date: date,
    new_facts_by_region: Mapping[str, tuple[CandidateFact, ...]],
) -> str:
    """Insert new Fact subsections into an already-existing daily file.
    Every pre-existing subsection blob is carried through completely
    unmodified; new subsections are appended after them (or replace the
    ``_No facts recorded._`` placeholder for a region that was empty)."""

    parsed = _parse_existing_daily_file(existing_text, expected_date=target_date)

    region_blocks = []
    for region in REGION_ORDER:
        existing_subsections = parsed.subsections_by_region[region]
        new_candidates = new_facts_by_region.get(region, ())
        new_subsections = tuple(render_fact_subsection(c) for c in new_candidates)
        combined = existing_subsections + new_subsections
        body = "\n\n".join(combined) if combined else NO_FACTS_PLACEHOLDER
        region_blocks.append(f"## {region}\n\n{body}")

    return "\n\n".join([parsed.preamble.rstrip("\n"), *region_blocks]) + "\n"


def write_daily_file(
    ai_fact_log_root: Path,
    target_date: date,
    new_facts_by_region: Mapping[str, tuple[CandidateFact, ...]],
) -> str:
    """Write (creating or updating) one daily file. Returns the relative
    path written. Uses atomic temp-file + ``os.replace`` like the other
    writers in this package."""

    import os
    import tempfile

    relative_path = daily_relative_path_for_date(target_date)
    full_path = Path(ai_fact_log_root) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if full_path.exists():
        existing_text = full_path.read_text(encoding="utf-8")
        new_text = insert_into_existing_daily_file(
            existing_text, target_date, new_facts_by_region
        )
    else:
        new_text = build_fresh_daily_file(target_date, new_facts_by_region)

    descriptor, temporary = tempfile.mkstemp(
        prefix=full_path.name + ".", suffix=".tmp", dir=full_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, full_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return relative_path
