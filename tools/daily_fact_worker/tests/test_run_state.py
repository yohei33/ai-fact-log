import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from tools.daily_fact_worker.run_state import (
    AtomicJsonRunStateRepository,
    RunHistoryEntry,
    RunOutcome,
    RunState,
)


def _entry(
    outcome: RunOutcome,
    window_start: date,
    window_end: date,
    *,
    run_id: str = "run_1",
) -> RunHistoryEntry:
    return RunHistoryEntry(
        run_id=run_id,
        run_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        trigger="manual",
        window_start_date=window_start,
        window_end_date=window_end,
        outcome=outcome,
        new_fact_count=0,
        event_ids_written=(),
        brainlab_import_outcome=None,
        error_type=None,
        error_message=None,
    )


class RunStateTransitionsTest(unittest.TestCase):
    def test_successful_run_advances_covered_date(self):
        state = RunState()
        entry = _entry(RunOutcome.COMPLETED_WITH_NEW_FACTS, date(2026, 8, 27), date(2026, 8, 28))
        new_state = state.record_run(entry)
        self.assertEqual(new_state.last_covered_date, date(2026, 8, 28))
        self.assertEqual(new_state.last_run_outcome, RunOutcome.COMPLETED_WITH_NEW_FACTS)
        self.assertIsNotNone(new_state.last_successful_run_at)

    def test_zero_new_facts_still_advances_covered_date(self):
        state = RunState()
        entry = _entry(RunOutcome.COMPLETED_NO_NEW_FACTS, date(2026, 8, 27), date(2026, 8, 28))
        new_state = state.record_run(entry)
        self.assertEqual(new_state.last_covered_date, date(2026, 8, 28))

    def test_failed_validation_does_not_advance_covered_date(self):
        state = RunState(last_covered_date=date(2026, 8, 26))
        entry = _entry(RunOutcome.FAILED_VALIDATION, date(2026, 8, 27), date(2026, 8, 28))
        new_state = state.record_run(entry)
        self.assertEqual(new_state.last_covered_date, date(2026, 8, 26))
        self.assertIsNone(new_state.last_successful_run_at)

    def test_failed_write_does_not_advance_covered_date(self):
        state = RunState(last_covered_date=date(2026, 8, 26))
        entry = _entry(RunOutcome.FAILED_WRITE, date(2026, 8, 27), date(2026, 8, 28))
        new_state = state.record_run(entry)
        self.assertEqual(new_state.last_covered_date, date(2026, 8, 26))

    def test_failed_brainlab_import_still_advances_covered_date(self):
        # The ai-fact-log write already succeeded by the time Brain Lab
        # import can fail; the source-of-truth window is genuinely
        # covered, so the next run must not re-discover the same Facts.
        state = RunState()
        entry = _entry(RunOutcome.FAILED_BRAINLAB_IMPORT, date(2026, 8, 27), date(2026, 8, 28))
        new_state = state.record_run(entry)
        self.assertEqual(new_state.last_covered_date, date(2026, 8, 28))
        # But it is not a "clean" success for last_successful_run_at.
        self.assertIsNone(new_state.last_successful_run_at)

    def test_run_history_is_bounded(self):
        state = RunState()
        for i in range(250):
            entry = _entry(
                RunOutcome.COMPLETED_NO_NEW_FACTS,
                date(2026, 1, 1),
                date(2026, 1, 1),
                run_id=f"run_{i}",
            )
            state = state.record_run(entry)
        self.assertLessEqual(len(state.run_history), 200)
        self.assertEqual(state.run_history[0].run_id, "run_249")


class AtomicJsonRunStateRepositoryTest(unittest.TestCase):
    def test_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "var" / "daily_fact_worker" / "run_state.json"
            repo = AtomicJsonRunStateRepository(path)

            loaded_empty = repo.load()
            self.assertIsNone(loaded_empty.last_covered_date)

            entry = _entry(RunOutcome.COMPLETED_WITH_NEW_FACTS, date(2026, 8, 27), date(2026, 8, 28))
            repo.save(loaded_empty.record_run(entry))

            reloaded = repo.load()
            self.assertEqual(reloaded.last_covered_date, date(2026, 8, 28))
            self.assertEqual(len(reloaded.run_history), 1)
            self.assertTrue(path.exists())
            # No stray temp files left behind.
            temp_files = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
            self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
