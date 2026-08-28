# Daily AI Fact Refresh Worker v0.1

- Status: Implemented and Manual-Run verified. `tools/daily_fact_worker/` (deterministic
  half) is unit-tested (51 tests, `unittest`, no external test framework required) and has
  been exercised end-to-end against this repository's real `daily/`/`index/` files and
  against Personal Brain Lab's real, unmodified Importer (4 real Facts written and imported
  on 2026-08-28, then a live idempotent re-run correctly rejected all 4 as duplicates).
- Scheduled local execution: **not yet possible**, and this is not a code problem. A
  Scheduled Task requires a "local device binding" (a separate, persistent grant beyond an
  interactive session's live device connection) to run `device_bash`/local scripts
  unattended. Two independent capability-gate tests against this account/device both
  returned `not bound: no_signed_approval -- this task will run in the cloud only` from
  Scheduled Task creation itself. Until a person grants that signed approval, this worker can
  only be run manually (an interactive Claude session doing real Web Discovery, then
  `tools/daily_fact_worker/cli.py run`) -- there is no autonomous, unattended path yet, and
  none is faked here.
- Updated: see the Manual Run entry in `var/daily_fact_worker/run_state.json` (private,
  git-ignored) for the actual last-run timestamp; this file is not updated automatically.

## What this is

A **half-automated** daily collector for this repository's `daily/YYYY/MM/YYYY-MM-DD.md` +
`index/events_index.jsonl` Public Fact records, plus a downstream call into Personal Brain
Lab's existing, unmodified Importer (`scripts/run_ai_fact_news_import_v0_1.py`) once a write
has succeeded.

It is **not** a bot that autonomously finds and publishes AI news. Be precise about what each
half actually does:

- **The External Fact Worker** (a Claude session, either run manually or by the daily
  Scheduled Task once/if one exists) does the part this package cannot: real web search,
  reading real pages, and deciding whether a claim is actually verified against a primary
  source. This is judgment, not a deterministic function, and this package does not attempt
  to replace it.
- **This package** (`tools/daily_fact_worker/`) does the part that should never depend on an
  LLM's judgment: strict schema/enum/uniqueness validation (fail-closed -- a Fact that does
  not check out is rejected and reported, never silently written and never silently dropped
  without a reason), byte-preserving writes to `daily/`+`index/`, private run-state so a
  fresh session always knows what window to search next, and the handoff into Personal Brain
  Lab's Importer.

Nothing in this package calls an LLM, a search API, or the network. It only reads/writes
local files (this repository's `daily/`/`index/`/`var/`, plus invoking Personal Brain Lab's
own importer script as a subprocess).

## Why the split, concretely

Every "did the worker actually run correctly" claim in this document is checkable by reading
a file, not by trusting a description:

- Did it run at all, and when? `var/daily_fact_worker/run_state.json` -> `last_run_at`,
  `last_run_outcome`.
- Was yesterday a real "nothing new" day, or did it just not run?
  `run_state.json` -> `run_history[]` has one entry per actual invocation, each with an
  explicit `outcome` (`completed_with_new_facts` / `completed_no_new_facts` /
  `failed_validation` / `failed_write` / `failed_brainlab_import` / `failed_run_error`) --
  never collapsed into a generic true/false.
- What did the External Fact Worker actually consider, including things it decided not to
  write? `var/daily_fact_worker/runs/<run_id>_worker_result.json` -- a private, unabridged
  copy of the structured result handed to this package for that run, kept regardless of
  outcome.
- What got rejected and why? The CLI's `run` output includes a `rejected` list with a
  `reason_code` and message per rejected candidate (see `validator.py`'s reason codes).

## Boundaries this worker keeps (same posture as `docs/AI_FACT_NEWS_V0_1.md`)

- The public 11-field Fact schema (`schema/fact-schema.md`) is never extended, renamed, or
  reshaped. `worker_result_contract.REQUIRED_PUBLIC_FACT_FIELDS` is checked against that exact
  set.
- This worker only auto-writes `verification: VERIFIED_PRIMARY` or
  `VERIFIED_PRIMARY_ARCHIVED` Facts (`validator.ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE`).
  `SECONDARY`/`UNVERIFIED` Facts are schema-valid but this worker refuses to write them
  automatically -- a Fact the External Fact Worker could not verify against a primary source
  is not written at all, never padded in as a weaker record.
- A day with genuinely no new Facts is a valid, expected outcome
  (`RunOutcome.COMPLETED_NO_NEW_FACTS`), never treated as an error and never papered over with
  an invented Fact.
- Every pre-existing fenced ```` ```yaml ``` ```` block's inner text is preserved
  byte-for-byte when a new Fact is inserted into the same daily file (`daily_writer.py`) --
  Personal Brain Lab's Importer classifies Facts by content hash, so an untouched Fact must
  never look "updated" just because this worker wrote nearby.
- The ai-fact-log write (`daily/` + `index/`) always happens before, and independently of,
  the Brain Lab import step. A Brain Lab import failure is recorded
  (`RunOutcome.FAILED_BRAINLAB_IMPORT`) but **never** rolls back or otherwise touches the
  already-successful ai-fact-log write (`orchestrator.py`, `brainlab_import_wrapper.py`).
- This worker never runs `git`, and never stages, commits, or pushes anything in either
  repository. Newly written Facts sit as ordinary uncommitted changes in this working tree
  until the repository owner reviews and commits them.
- This worker never creates a Personal Observation, Personal Context, Personal Decision, or
  Signal from AI Fact data, and never touches the separate Weekly Semantic Synthesis task or
  its schedule.

## Layout

```
tools/daily_fact_worker/
  __init__.py                    package-level boundary docstring
  worker_result_contract.py      daily_fact_worker_result.v0.1 (shape only)
  validator.py                   fail-closed business-rule validation, per candidate
  daily_writer.py                daily/YYYY/MM/YYYY-MM-DD.md reader/writer
  index_writer.py                index/events_index.jsonl reader/appender
  run_state.py                   private run-state model + atomic JSON repository
  brainlab_import_wrapper.py     subprocess wrapper around the existing Brain Lab importer
  orchestrator.py                perform_run(): ties the above together for one run
  cli.py                         `window` / `run` subcommands (see below)
  tests/                         unittest suite (51 tests as of this Manual Run)

var/daily_fact_worker/           git-ignored (see .gitignore); created on first real run
  run_state.json                 this worker's private resume cursor + run history
  runs/<run_id>_worker_result.json   private audit copy of each run's structured input
```

## How to actually run it

### 1. Ask what window to search

```
python3 tools/daily_fact_worker/cli.py window
```

Prints the `[window_start_date, window_end_date]` (inclusive, JST-local calendar dates as
recorded in each Fact's own `captured_at`) the External Fact Worker should search this run,
computed from `var/daily_fact_worker/run_state.json` if it exists, or (first run only) from
the most recent existing `daily/` file, or (empty repository) a flat 7-day fallback. Always
capped at 7 days regardless of how long the worker has been idle.

### 2. Do real Web Discovery for that window

The External Fact Worker searches, opens, and verifies candidate Facts against primary
sources for the printed window, and writes a `daily_fact_worker_result.v0.1` JSON document
(see `worker_result_contract.py` for the exact shape) -- either listing verified candidates,
or `"zero_new_facts": true` with an empty `candidate_facts` list if nothing new and
verifiable was found. A zero-new-facts result is not an error and must not be padded.

### 3. Validate, write, and import

```
python3 tools/daily_fact_worker/cli.py run --worker-result-file <path to the JSON from step 2>
```

Validates every candidate independently (fail-closed: a bad candidate is rejected and
reported, never blocking the others), writes whatever survives to `daily/`+`index/`, then
(only if at least one Fact was actually written) invokes Personal Brain Lab's existing
`scripts/run_ai_fact_news_import_v0_1.py` unmodified, and finally saves run state. Prints a
JSON run report. Exit code is non-zero only when nothing usable was written this run
(`failed_validation`, `failed_write`, `failed_run_error`); `completed_no_new_facts` and
`failed_brainlab_import` (ai-fact-log write already succeeded; only the downstream Brain Lab
sync failed) both exit 0.

### Path resolution

- `--ai-fact-log-root` / `AI_FACT_LOG_ROOT` env var / default: this script's own repository
  root (it always knows where it physically lives).
- `--personal-brain-lab-root` / `PERSONAL_BRAIN_LAB_ROOT` env var / default: a sibling
  `PersonalBrain_Lab` directory next to ai-fact-log (true for this deployment; override for
  any other layout).

## What is intentionally NOT implemented in v0.1

- Any autonomous discovery, crawling, or scraping performed by this package's own code --
  Discovery is the External Fact Worker's job, every run, by design.
- Any semantic analysis, summarization, categorization beyond the recommended `category`
  values, or AKARI-layer interpretation of Facts.
- Rewriting, correcting, or removing an already-written Fact. This worker only ever inserts
  new Facts; correcting an existing one remains a manual, ordinary Git-tracked edit by the
  repository owner (see `schema/fact-schema.md` "Daily file rules").
- Any `git add`/`commit`/`push` from this worker, ever.
- Region or fact-count quotas of any kind -- a run with zero verifiable Facts and a run with
  ten are both simply reported honestly.
