import json
import tempfile
import unittest
from pathlib import Path

from tools.daily_fact_worker.index_writer import (
    IndexEntry,
    append_index_entries,
    read_known_event_ids,
)


class IndexWriterTest(unittest.TestCase):
    def test_missing_index_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_known_event_ids(Path(tmp)), frozenset())

    def test_append_then_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = IndexEntry(
                event_id="openai-20260828-001",
                date="2026-08-28",
                organization="OpenAI",
                source_url="https://openai.com/index/example",
                verification="VERIFIED_PRIMARY",
            )
            written = append_index_entries(root, [entry])
            self.assertEqual(written, 1)

            known = read_known_event_ids(root)
            self.assertEqual(known, frozenset({"openai-20260828-001"}))

            index_path = root / "index" / "events_index.jsonl"
            lines = index_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(set(obj.keys()), {"event_id", "date", "organization", "source_url", "verification"})

    def test_pre_existing_lines_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_path = root / "index" / "events_index.jsonl"
            index_path.parent.mkdir(parents=True)
            original_line = '{"event_id":"anthropic-20260825-001","date":"2026-08-26","organization":"Anthropic","source_url":"https://claude.com/blog/example","verification":"VERIFIED_PRIMARY"}\n'
            index_path.write_text(original_line, encoding="utf-8")

            new_entry = IndexEntry(
                event_id="openai-20260828-001",
                date="2026-08-28",
                organization="OpenAI",
                source_url="https://openai.com/index/example",
                verification="VERIFIED_PRIMARY",
            )
            append_index_entries(root, [new_entry])

            content = index_path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith(original_line))
            self.assertEqual(len(content.splitlines()), 2)

    def test_appending_empty_list_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = append_index_entries(root, [])
            self.assertEqual(written, 0)
            self.assertFalse((root / "index" / "events_index.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
