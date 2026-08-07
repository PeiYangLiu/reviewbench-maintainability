import json
from pathlib import Path
import tempfile
import unittest

from reviewbench_maintainability.merge_candidates import candidate_files, read_jsonl


class MergeCandidatesTests(unittest.TestCase):
    def test_finds_batch_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "batches" / "one" / "candidates.jsonl"
            second = root / "batches" / "two" / "candidates.jsonl"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text('{"id":"a"}\n', encoding="utf-8")
            second.write_text('{"id":"b"}\n', encoding="utf-8")

            paths = candidate_files(root)

        self.assertEqual(len(paths), 2)

    def test_reads_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.jsonl"
            path.write_text(
                json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b"}) + "\n",
                encoding="utf-8",
            )

            records = list(read_jsonl(path))

        self.assertEqual([item["id"] for item in records], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
