import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from tools.daily_fact_worker.brainlab_import_wrapper import BrainLabImportOutcome
from tools.daily_fact_worker.orchestrator import (
    compute_next_discovery_window,
    perform_run,
)
from tools.daily_fact_worker.run_state import AtomicJsonRunStateRepository, RunOutcome, RunState
from tools.daily_fact_worker.worker_result_contract import DailyFactWorkerResult


def _worker_result(zero_new_facts: bool, candidate_fields_list=None, run_id="run_test") -> DailyFactWorkerResult:
    return DailyFactWorkerResult.from_dict(
        {
            "contract_version": "daily_fact_worker_result.v0.1",
            "run_id": run_id,
            "trigger": "manual",
            "generated_at": "2026-08-28T12:00:00Z",
            "window_start_date": "2026-08-27",
            "window_end_date": "2026-08-28",
            "discovery_notes": "test",
            "zero_new_facts": zero_new_facts,
            "candidate_facts": candidate_fields_list or [],
        }
    )


def _one_candidate_field(event_id="openai-20260828-001"):
    return {
        "event_id": event_id,
        "title": "Example title",
        "fact": "OpenAI announced an example fact for this test.",
        "organization": "OpenAI",
        "region": "US",
        "category": "Product",
        "published_at": "2026-08-28",
        "captured_at": "2026-08-28T20:00:00+09:00",
        "source_type": "official_blog",
        "source_url": "https://openai.com/index/example",
        "verification": "VERIFIED_PRIMARY",
    }


class ComputeNextDiscoveryWindowTest(unittest.TestCase):
    def test_bootstrap_window_when_never_run(self):
        state = RunState()
        start, end = compute_next_discovery_window(state, today=date(2026, 8, 28))
        self.assertEqual(end, date(2026, 8, 28))
        self.assertEqual((end - start).days + 1, 7)

    def test_resumes_from_day_after_last_covered_date(self):
        state = RunState(last_covered_date=date(2026, 8, 26))
        start, end = compute_next_discovery_window(state, today=date(2026, 8, 28))
        self.assertEqual(start, date(2026, 8, 27))
        self.assertEqual(end, date(2026, 8, 28))

    def test_window_never_exceeds_cap_even_after_long_gap(self):
        state = RunState(last_covered_date=date(2026, 1, 1))
        start, end = compute_next_discovery_window(state, today=date(2026, 8, 28), max_window_days=7)
        self.assertEqual((end - start).days + 1, 7)

    def test_already_covered_today_gives_single_day_window(self):
        state = RunState(last_covered_date=date(2026, 8, 28))
        start, end = compute_next_discovery_window(state, today=date(2026, 8, 28))
        self.assertEqual(start, date(2026, 8, 28))
        self.assertEqual(end, date(2026, 8, 28))

    def test_first_ever_run_bootstraps_from_latest_existing_daily_file(self):
        # No run state yet, but daily/ already has manually-entered Facts
        # up to 2026-08-26 -- the bootstrap window must start the day
        # after that, not blindly N days before today.
        state = RunState()
        start, end = compute_next_discovery_window(
            state,
            today=date(2026, 8, 28),
            latest_existing_daily_date=date(2026, 8, 26),
        )
        self.assertEqual(start, date(2026, 8, 27))
        self.assertEqual(end, date(2026, 8, 28))

    def test_first_ever_run_with_no_daily_files_falls_back_to_flat_bootstrap(self):
        state = RunState()
        start, end = compute_next_discovery_window(
            state, today=date(2026, 8, 28), latest_existing_daily_date=None
        )
        self.assertEqual((end - start).days + 1, 7)

    def test_latest_existing_daily_date_ignored_once_state_exists(self):
        state = RunState(last_covered_date=date(2026, 8, 26))
        start, end = compute_next_discovery_window(
            state,
            today=date(2026, 8, 28),
            latest_existing_daily_date=date(2026, 1, 1),
        )
        self.assertEqual(start, date(2026, 8, 27))


class PerformRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ai_fact_log_root = Path(self._tmp.name) / "ai-fact-log"
        self.ai_fact_log_root.mkdir()
        self.personal_brain_lab_root = Path(self._tmp.name) / "PersonalBrain_Lab"
        self.personal_brain_lab_root.mkdir()
        self.state_repo = AtomicJsonRunStateRepository(
            self.ai_fact_log_root / "var" / "daily_fact_worker" / "run_state.json"
        )
        self.run_audit_dir = self.ai_fact_log_root / "var" / "daily_fact_worker" / "runs"

    def _perform(self, worker_result, skip_brainlab_import=True):
        return perform_run(
            ai_fact_log_root=self.ai_fact_log_root,
            personal_brain_lab_root=self.personal_brain_lab_root,
            state_repository=self.state_repo,
            run_audit_dir=self.run_audit_dir,
            worker_result=worker_result,
            skip_brainlab_import=skip_brainlab_import,
        )

    def test_zero_new_facts_day_writes_nothing_and_advances_state(self):
        report = self._perform(_worker_result(zero_new_facts=True))
        self.assertEqual(report.outcome, RunOutcome.COMPLETED_NO_NEW_FACTS)
        self.assertEqual(report.daily_files_written, ())
        self.assertEqual(report.index_entries_written, 0)
        self.assertFalse((self.ai_fact_log_root / "daily").exists())

        state = self.state_repo.load()
        self.assertEqual(state.last_covered_date, date(2026, 8, 28))

    def test_valid_candidate_is_written_to_daily_and_index(self):
        result = _worker_result(zero_new_facts=False, candidate_fields_list=[_one_candidate_field()])
        report = self._perform(result)
        self.assertEqual(report.outcome, RunOutcome.COMPLETED_WITH_NEW_FACTS)
        self.assertEqual(report.daily_files_written, ("daily/2026/08/2026-08-28.md",))
        self.assertEqual(report.index_entries_written, 1)
        self.assertTrue((self.ai_fact_log_root / "daily/2026/08/2026-08-28.md").exists())
        self.assertTrue((self.ai_fact_log_root / "index/events_index.jsonl").exists())

    def test_rerunning_the_same_candidate_is_idempotent(self):
        result = _worker_result(zero_new_facts=False, candidate_fields_list=[_one_candidate_field()])
        first_report = self._perform(result)
        self.assertEqual(first_report.outcome, RunOutcome.COMPLETED_WITH_NEW_FACTS)

        daily_path = self.ai_fact_log_root / "daily/2026/08/2026-08-28.md"
        text_after_first_run = daily_path.read_text(encoding="utf-8")

        # Re-offer the exact same candidate again (e.g. a re-run after a
        # crash, or a scheduler double-fire).
        second_result = _worker_result(
            zero_new_facts=False, candidate_fields_list=[_one_candidate_field()], run_id="run_test_2"
        )
        second_report = self._perform(second_result)

        self.assertEqual(second_report.outcome, RunOutcome.FAILED_VALIDATION)
        self.assertEqual(len(second_report.rejected), 1)
        self.assertEqual(second_report.rejected[0].reason_code, "event_id_already_exists")
        self.assertEqual(second_report.daily_files_written, ())

        # No duplicate content was written.
        text_after_second_run = daily_path.read_text(encoding="utf-8")
        self.assertEqual(text_after_first_run, text_after_second_run)

    def test_all_candidates_rejected_does_not_advance_state(self):
        bad_field = _one_candidate_field()
        bad_field["region"] = "EU"  # invalid
        result = _worker_result(zero_new_facts=False, candidate_fields_list=[bad_field])
        report = self._perform(result)
        self.assertEqual(report.outcome, RunOutcome.FAILED_VALIDATION)

        state = self.state_repo.load()
        self.assertIsNone(state.last_covered_date)

    def test_brainlab_import_success_boundary(self):
        result = _worker_result(zero_new_facts=False, candidate_fields_list=[_one_candidate_field()])
        fake_outcome = BrainLabImportOutcome(
            succeeded=True, exit_code=0, stdout="{}", stderr="", parsed_result={"failed_count": 0}
        )
        with mock.patch(
            "tools.daily_fact_worker.orchestrator.run_brainlab_import", return_value=fake_outcome
        ):
            report = self._perform(result, skip_brainlab_import=False)
        self.assertEqual(report.outcome, RunOutcome.COMPLETED_WITH_NEW_FACTS)
        self.assertIsNotNone(report.brainlab_import)
        self.assertTrue(report.brainlab_import.succeeded)

    def test_brainlab_import_failure_does_not_undo_ai_fact_log_write(self):
        result = _worker_result(zero_new_facts=False, candidate_fields_list=[_one_candidate_field()])
        fake_outcome = BrainLabImportOutcome(
            succeeded=False,
            exit_code=1,
            stdout="{}",
            stderr="boom",
            parsed_result=None,
            error_message="import script exited with code 1",
        )
        with mock.patch(
            "tools.daily_fact_worker.orchestrator.run_brainlab_import", return_value=fake_outcome
        ):
            report = self._perform(result, skip_brainlab_import=False)

        self.assertEqual(report.outcome, RunOutcome.FAILED_BRAINLAB_IMPORT)
        # The ai-fact-log write itself must still be present on disk.
        self.assertTrue((self.ai_fact_log_root / "daily/2026/08/2026-08-28.md").exists())
        self.assertEqual(report.index_entries_written, 1)

        # And the covered window still advances, so a re-run does not
        # re-discover and re-reject the same, already-written Fact.
        state = self.state_repo.load()
        self.assertEqual(state.last_covered_date, date(2026, 8, 28))
        self.assertIsNone(state.last_successful_run_at)


if __name__ == "__main__":
    unittest.main()
