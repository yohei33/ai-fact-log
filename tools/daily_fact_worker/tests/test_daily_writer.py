import hashlib
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from tools.daily_fact_worker.daily_writer import (
    DailyWriterError,
    build_fresh_daily_file,
    daily_relative_path_for_date,
    find_latest_daily_file_date,
    insert_into_existing_daily_file,
    write_daily_file,
)
from tools.daily_fact_worker.worker_result_contract import CandidateFact


# Same fence regex Personal Brain Lab's real Importer uses, duplicated
# here on purpose (see brainlab_pipeline/ai_fact_news_importer_v0_1.py)
# so these tests prove round-trip compatibility with the actual consumer
# rather than only with this package's own writer.
_YAML_FENCE_RE = re.compile(r"```yaml[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
_REQUIRED_FIELDS = (
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


def _candidate(event_id="openai-20260828-001", region="US", organization="OpenAI", title="Example title", **overrides) -> CandidateFact:
    fields = {
        "event_id": event_id,
        "title": title,
        "fact": "OpenAI announced an example fact for this test.",
        "organization": organization,
        "region": region,
        "category": "Product",
        "published_at": "2026-08-28",
        "captured_at": "2026-08-28T20:00:00+09:00",
        "source_type": "official_blog",
        "source_url": "https://openai.com/index/example",
        "verification": "VERIFIED_PRIMARY",
    }
    fields.update(overrides)
    return CandidateFact.from_dict(fields)


class DailyRelativePathTest(unittest.TestCase):
    def test_path_format(self):
        self.assertEqual(
            daily_relative_path_for_date(date(2026, 8, 28)),
            "daily/2026/08/2026-08-28.md",
        )


class BuildFreshDailyFileTest(unittest.TestCase):
    def test_every_block_round_trips_and_has_exact_key_set(self):
        candidate = _candidate()
        text = build_fresh_daily_file(date(2026, 8, 28), {"US": (candidate,)})

        self.assertIn('date: "2026-08-28"', text)
        self.assertIn("# Daily AI Facts — 2026-08-28", text)
        self.assertIn("## US", text)
        self.assertIn("_No facts recorded._", text)  # other 4 regions

        blocks = _YAML_FENCE_RE.findall(text)
        self.assertEqual(len(blocks), 1)
        parsed = yaml.safe_load(blocks[0])
        self.assertEqual(set(parsed.keys()), set(_REQUIRED_FIELDS))
        self.assertEqual(parsed["event_id"], "openai-20260828-001")
        # Critically: published_at must round-trip as a *string*, not a
        # date object, or Personal Brain Lab's ObservationTime parsing
        # would reject it.
        self.assertIsInstance(parsed["published_at"], str)
        self.assertIsInstance(parsed["captured_at"], str)

    def test_all_five_regions_present_in_order(self):
        text = build_fresh_daily_file(date(2026, 8, 28), {})
        for region in ("US", "CN", "JP", "GLOBAL", "OTHER"):
            self.assertIn(f"## {region}", text)
        us_pos = text.index("## US")
        cn_pos = text.index("## CN")
        jp_pos = text.index("## JP")
        global_pos = text.index("## GLOBAL")
        other_pos = text.index("## OTHER")
        self.assertTrue(us_pos < cn_pos < jp_pos < global_pos < other_pos)

    def test_no_facts_at_all_produces_five_placeholders(self):
        text = build_fresh_daily_file(date(2026, 8, 28), {})
        self.assertEqual(text.count("_No facts recorded._"), 5)
        self.assertEqual(_YAML_FENCE_RE.findall(text), [])


class InsertIntoExistingDailyFileTest(unittest.TestCase):
    def _existing_file_with_one_us_fact(self) -> str:
        original_candidate = _candidate(
            event_id="anthropic-20260827-001",
            organization="Anthropic",
            title="Pre-existing fact",
        )
        return build_fresh_daily_file(date(2026, 8, 28), {"US": (original_candidate,)})

    def test_existing_block_bytes_are_preserved_when_adding_to_same_region(self):
        existing_text = self._existing_file_with_one_us_fact()
        original_blocks = _YAML_FENCE_RE.findall(existing_text)
        self.assertEqual(len(original_blocks), 1)
        original_hash = hashlib.sha256(original_blocks[0].encode("utf-8")).hexdigest()

        new_candidate = _candidate(
            event_id="openai-20260828-002", organization="OpenAI", title="New fact"
        )
        updated_text = insert_into_existing_daily_file(
            existing_text, date(2026, 8, 28), {"US": (new_candidate,)}
        )

        updated_blocks = _YAML_FENCE_RE.findall(updated_text)
        self.assertEqual(len(updated_blocks), 2)
        # The pre-existing block's raw text must appear byte-for-byte
        # unchanged among the updated blocks.
        rehashed = [hashlib.sha256(b.encode("utf-8")).hexdigest() for b in updated_blocks]
        self.assertIn(original_hash, rehashed)
        self.assertIn(original_blocks[0], updated_blocks)

    def test_placeholder_region_is_replaced_not_appended_to(self):
        existing_text = self._existing_file_with_one_us_fact()  # CN is a placeholder
        new_candidate = _candidate(
            event_id="tencent-20260828-001",
            organization="Tencent",
            title="CN fact",
            region="CN",
        )
        updated_text = insert_into_existing_daily_file(
            existing_text, date(2026, 8, 28), {"CN": (new_candidate,)}
        )
        cn_section = updated_text.split("## CN", 1)[1].split("## JP", 1)[0]
        self.assertNotIn("_No facts recorded._", cn_section)
        self.assertIn("Tencent", cn_section)

    def test_wrong_target_date_is_refused(self):
        existing_text = self._existing_file_with_one_us_fact()
        with self.assertRaises(DailyWriterError):
            insert_into_existing_daily_file(existing_text, date(2026, 8, 29), {})

    def test_unrecognized_shape_is_refused(self):
        with self.assertRaises(DailyWriterError):
            insert_into_existing_daily_file("not a real daily file at all", date(2026, 8, 28), {})


class WriteDailyFileOnDiskTest(unittest.TestCase):
    def test_create_then_update_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _candidate(event_id="anthropic-20260828-001", organization="Anthropic")
            relative_path = write_daily_file(root, date(2026, 8, 28), {"US": (first,)})
            full_path = root / relative_path
            first_write_text = full_path.read_text(encoding="utf-8")
            original_block = _YAML_FENCE_RE.findall(first_write_text)[0]

            second = _candidate(event_id="openai-20260828-002", organization="OpenAI")
            write_daily_file(root, date(2026, 8, 28), {"US": (second,)})
            second_write_text = full_path.read_text(encoding="utf-8")
            blocks = _YAML_FENCE_RE.findall(second_write_text)

            self.assertEqual(len(blocks), 2)
            self.assertIn(original_block, blocks)
            # No leftover temp files.
            temp_files = [p for p in full_path.parent.iterdir() if p.suffix == ".tmp"]
            self.assertEqual(temp_files, [])


class FindLatestDailyFileDateTest(unittest.TestCase):
    def test_no_daily_directory_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_latest_daily_file_date(Path(tmp)))

    def test_finds_the_most_recent_date_across_months(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_daily_file(root, date(2026, 7, 30), {})
            write_daily_file(root, date(2026, 8, 5), {})
            write_daily_file(root, date(2026, 8, 26), {})
            self.assertEqual(find_latest_daily_file_date(root), date(2026, 8, 26))


if __name__ == "__main__":
    unittest.main()
