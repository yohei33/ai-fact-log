"""Read and append-only-append ``ai-fact-log/index/events_index.jsonl``.

Follows the JSONL index contract in ``schema/fact-schema.md`` exactly:
one compact JSON object per line, UTF-8, no blank lines, exactly the
fields ``event_id``, ``date``, ``organization``, ``source_url``,
``verification`` (title and Fact body are deliberately not duplicated
into the index -- they live only in the daily Markdown).

Existing lines are never rewritten or reordered: appending is done by
reading the current file's raw bytes, concatenating the new line(s), and
writing the result out atomically (temp file + ``os.replace`` in the same
directory), so a crash mid-write can never leave a half-written index and
every pre-existing byte is preserved untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .worker_result_contract import CandidateFact


INDEX_RELATIVE_PATH = "index/events_index.jsonl"
INDEX_FIELDS = ("event_id", "date", "organization", "source_url", "verification")


class IndexWriterError(RuntimeError):
    """Raised for a genuine I/O or consistency failure while reading or
    writing the index -- never for a single bad candidate (that is
    ``validator.py``'s job, before this module is ever called)."""


@dataclass(frozen=True)
class IndexEntry:
    event_id: str
    date: str
    organization: str
    source_url: str
    verification: str

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "date": self.date,
                "organization": self.organization,
                "source_url": self.source_url,
                "verification": self.verification,
            },
            ensure_ascii=False,
            sort_keys=False,
        )


def read_known_event_ids(ai_fact_log_root: Path) -> frozenset[str]:
    """Read-only scan of the existing index for every already-registered
    ``event_id``. Missing file is treated as "no Facts yet", not an
    error -- a brand new checkout legitimately has none."""

    index_path = Path(ai_fact_log_root) / INDEX_RELATIVE_PATH
    if not index_path.exists():
        return frozenset()
    event_ids: set[str] = set()
    with open(index_path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IndexWriterError(
                    f"{INDEX_RELATIVE_PATH} line {line_number} is not valid JSON"
                ) from exc
            event_id = obj.get("event_id")
            if isinstance(event_id, str) and event_id:
                event_ids.add(event_id)
    return frozenset(event_ids)


def candidate_to_index_entry(candidate: CandidateFact, *, daily_date: str) -> IndexEntry:
    fields = candidate.fields
    return IndexEntry(
        event_id=fields["event_id"],
        date=daily_date,
        organization=fields["organization"],
        source_url=fields["source_url"],
        verification=fields["verification"],
    )


def append_index_entries(ai_fact_log_root: Path, entries: Iterable[IndexEntry]) -> int:
    """Append entries to the index, atomically. Returns the number of
    lines actually appended. A caller must have already deduplicated
    against ``read_known_event_ids`` (via ``validator.py``) -- this
    function does not silently skip duplicates, it is a pure append."""

    entries = list(entries)
    if not entries:
        return 0

    index_path = Path(ai_fact_log_root) / INDEX_RELATIVE_PATH
    index_path.parent.mkdir(parents=True, exist_ok=True)

    existing_bytes = b""
    if index_path.exists():
        with open(index_path, "rb") as handle:
            existing_bytes = handle.read()

    new_lines = "".join(entry.to_json_line() + "\n" for entry in entries)
    combined = existing_bytes
    if combined and not combined.endswith(b"\n"):
        combined += b"\n"
    combined += new_lines.encode("utf-8")

    descriptor, temporary = tempfile.mkstemp(
        prefix=index_path.name + ".", suffix=".tmp", dir=index_path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(combined)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, index_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return len(entries)
