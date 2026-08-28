import unittest
from datetime import date

from tools.daily_fact_worker.validator import (
    ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE,
    target_daily_date_for,
    validate_candidates,
)
from tools.daily_fact_worker.worker_result_contract import CandidateFact


WINDOW_START = date(2026, 8, 27)
WINDOW_END = date(2026, 8, 28)


def _valid_fields(**overrides) -> dict:
    fields = {
        "event_id": "openai-20260827-001",
        "title": "OpenAI announces something verifiable",
        "fact": "OpenAI announced X in an official blog post on 2026-08-27.",
        "organization": "OpenAI",
        "region": "US",
        "category": "Product",
        "published_at": "2026-08-27",
        "captured_at": "2026-08-28T09:00:00+09:00",
        "source_type": "official_blog",
        "source_url": "https://openai.com/index/example",
        "verification": "VERIFIED_PRIMARY",
    }
    fields.update(overrides)
    return fields


def _candidate(**overrides) -> CandidateFact:
    return CandidateFact.from_dict(_valid_fields(**overrides))


class TargetDailyDateForTest(unittest.TestCase):
    def test_uses_captured_at_own_offset_date(self):
        # 2026-08-28T23:30:00+09:00 is still 2026-08-28 in its own offset,
        # even though it is 2026-08-28T14:30:00Z in UTC.
        self.assertEqual(
            target_daily_date_for("2026-08-28T23:30:00+09:00"), date(2026, 8, 28)
        )


class ValidateCandidatesAcceptanceTest(unittest.TestCase):
    def test_a_fully_valid_candidate_is_accepted(self):
        outcome = validate_candidates(
            [_candidate()],
            known_event_ids=frozenset(),
            window_start_date=WINDOW_START,
            window_end_date=WINDOW_END,
        )
        self.assertEqual(len(outcome.accepted), 1)
        self.assertEqual(outcome.rejected, ())


class ValidateCandidatesRejectionTest(unittest.TestCase):
    def _assert_rejected(self, candidate: CandidateFact, expected_reason_prefix: str, **kwargs):
        outcome = validate_candidates(
            [candidate],
            known_event_ids=kwargs.get("known_event_ids", frozenset()),
            window_start_date=kwargs.get("window_start_date", WINDOW_START),
            window_end_date=kwargs.get("window_end_date", WINDOW_END),
        )
        self.assertEqual(outcome.accepted, ())
        self.assertEqual(len(outcome.rejected), 1)
        self.assertTrue(
            outcome.rejected[0].reason_code.startswith(expected_reason_prefix),
            outcome.rejected[0].reason_code,
        )

    def test_invalid_region_is_rejected(self):
        self._assert_rejected(_candidate(region="EU"), "region_invalid")

    def test_invalid_source_type_is_rejected(self):
        self._assert_rejected(_candidate(source_type="rumor"), "source_type_invalid")

    def test_secondary_verification_is_rejected(self):
        self.assertNotIn("SECONDARY", ALLOWED_VERIFICATIONS_FOR_AUTO_WRITE)
        self._assert_rejected(
            _candidate(verification="SECONDARY"), "verification_not_auto_writable"
        )

    def test_unverified_verification_is_rejected(self):
        self._assert_rejected(
            _candidate(verification="UNVERIFIED"), "verification_not_auto_writable"
        )

    def test_malformed_event_id_is_rejected(self):
        self._assert_rejected(_candidate(event_id="OpenAI 2026"), "event_id_format_invalid")

    def test_event_id_date_mismatch_with_published_at_is_rejected(self):
        self._assert_rejected(
            _candidate(event_id="openai-20260101-001"), "event_id_date_mismatch"
        )

    def test_captured_at_without_timezone_is_rejected(self):
        self._assert_rejected(
            _candidate(captured_at="2026-08-28T09:00:00"), "captured_at_no_timezone"
        )

    def test_non_https_source_url_is_rejected(self):
        self._assert_rejected(
            _candidate(source_url="http://openai.com/index/example"), "source_url_not_https"
        )

    def test_event_id_already_known_is_rejected(self):
        self._assert_rejected(
            _candidate(),
            "event_id_already_exists",
            known_event_ids=frozenset({"openai-20260827-001"}),
        )

    def test_duplicate_event_id_within_run_is_rejected(self):
        outcome = validate_candidates(
            [_candidate(), _candidate()],
            known_event_ids=frozenset(),
            window_start_date=WINDOW_START,
            window_end_date=WINDOW_END,
        )
        self.assertEqual(len(outcome.accepted), 1)
        self.assertEqual(len(outcome.rejected), 1)
        self.assertEqual(outcome.rejected[0].reason_code, "event_id_duplicate_in_run")

    def test_captured_at_outside_window_is_rejected(self):
        self._assert_rejected(
            _candidate(captured_at="2026-09-15T09:00:00+09:00"),
            "captured_at_outside_window",
        )

    def test_placeholder_category_is_rejected(self):
        self._assert_rejected(_candidate(category="Unknown"), "category_placeholder")

    def test_empty_organization_is_rejected(self):
        self._assert_rejected(_candidate(organization="   "), "organization_empty")

    def test_null_published_at_uses_captured_at_date_for_event_id(self):
        # published_at unknown -> event_id date must match captured_at's date instead.
        outcome = validate_candidates(
            [
                _candidate(
                    event_id="openai-20260828-002",
                    published_at=None,
                    captured_at="2026-08-28T09:00:00+09:00",
                )
            ],
            known_event_ids=frozenset(),
            window_start_date=WINDOW_START,
            window_end_date=WINDOW_END,
        )
        self.assertEqual(len(outcome.accepted), 1)


if __name__ == "__main__":
    unittest.main()
