"""Daily AI Fact Refresh Worker v0.1 for ``ai-fact-log``.

This package implements the *deterministic* half of the Daily AI Fact
Refresh Worker: run-state tracking, strict schema/enum/uniqueness
validation, an append-only ``daily/`` + ``index/events_index.jsonl``
writer, and a thin wrapper that invokes Personal Brain Lab's existing
``scripts/run_ai_fact_news_import_v0_1.py`` after a successful write.

It deliberately does NOT perform Web Discovery itself. Finding and
verifying new AI industry Facts against a primary source is done by the
External Fact Worker (a Claude session with real web search/fetch tools)
once per run; that worker hands this package a structured
``daily_fact_worker_result.v0.1`` document (see
``worker_result_contract.py``) describing the candidate Facts it found
(or genuinely found none). This package's job starts there: validate
fail-closed, write only what survives validation, never invent or pad
missing Facts, and keep an honest run-state history so a later run (or a
person) can tell "ran and found nothing new" apart from "did not run" or
"failed midway".

Boundaries kept everywhere in this package (see
``docs/DAILY_AI_FACT_WORKER_V0_1.md`` in this repository):

- The public ``daily/YYYY/MM/YYYY-MM-DD.md`` Fact schema
  (``schema/fact-schema.md``, the exact 11 keys) is never extended,
  renamed, or reshaped.
- This package never runs ``git`` commands and never stages, commits, or
  pushes anything -- publishing newly written Facts to Git is left
  entirely to the repository owner.
- This package never edits or removes an existing Fact block's bytes.
  New Facts are only ever appended into a region section (or replace that
  region's ``_No facts recorded._`` placeholder); pre-existing blocks are
  byte-for-byte untouched so Personal Brain Lab's content-hash based
  Importer never misclassifies an unrelated existing Fact as ``updated``.
- All run-state and per-run audit data lives under this repository's
  git-ignored ``var/daily_fact_worker/`` directory (see this repo's
  ``.gitignore``), never inside ``daily/`` or ``index/`` themselves.
"""

from __future__ import annotations

__all__: list[str] = []
