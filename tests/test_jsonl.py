import contextlib
from functools import partial
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reviewbench_maintainability import (
    curate_pool,
    evaluate,
    evidence,
    jsonl,
    merge_candidates,
    quality_gate,
    recovery,
    release,
)


class JsonlTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_writers_preserve_literal_bytes_and_create_parents(self) -> None:
        writers = (
            jsonl.write_jsonl,
            partial(jsonl.write_jsonl, atomic=True),
            recovery.write_jsonl,
            quality_gate._write_jsonl,
        )
        cases = (
            ([], b""),
            ([{"z": "café雪", "a": {"z": 2, "a": 1}}, {"id": "b"}],
             '{"a": {"a": 1, "z": 2}, "z": "café雪"}\n{"id": "b"}\n'.encode("utf-8")),
        )
        for index, writer in enumerate(writers):
            for case, (records, expected) in enumerate(cases):
                with self.subTest(writer=index, case=case):
                    path = self.root / str(index) / str(case) / "rows.jsonl"
                    self.assertIsNone(writer(path, records))
                    self.assertEqual(path.read_bytes(), expected)
                    self.assertFalse(path.with_suffix(".jsonl.tmp").exists())

    def test_direct_write_does_not_replace_or_touch_sibling(self) -> None:
        path = self.root / "rows.jsonl"
        temporary = path.with_suffix(".jsonl.tmp")
        path.write_bytes(b"old\n")
        temporary.write_bytes(b"unrelated\n")
        with patch.object(Path, "replace", side_effect=AssertionError("not atomic")):
            recovery.write_jsonl(path, [])
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(temporary.read_bytes(), b"unrelated\n")

    def test_atomic_write_uses_suffix_and_replaces_only_after_write(self) -> None:
        replace = Path.replace
        for filename in ("rows.jsonl", "rows", "rows.part.jsonl"):
            with self.subTest(filename=filename):
                path = self.root / filename
                temporary = path.with_suffix(path.suffix + ".tmp")
                path.write_bytes(b"old\n")
                temporary.write_bytes(b"stale\n")

                def observe_replace(source, destination):
                    self.assertEqual(source, temporary)
                    self.assertEqual(destination, path)
                    self.assertEqual(path.read_bytes(), b"old\n")
                    self.assertEqual(source.read_bytes(), b'{"id": "new"}\n')
                    return replace(source, destination)

                with patch.object(Path, "replace", autospec=True, side_effect=observe_replace) as replaced:
                    quality_gate._write_jsonl(path, [{"id": "new"}])
                replaced.assert_called_once()
                self.assertEqual(path.read_bytes(), b'{"id": "new"}\n')
                self.assertFalse(temporary.exists())

    def test_failed_replace_keeps_original_and_written_temporary(self) -> None:
        path = self.root / "rows.jsonl"
        path.write_bytes(b"old\n")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                quality_gate._write_jsonl(path, [{"id": "new"}])
        self.assertEqual(path.read_bytes(), b"old\n")
        self.assertEqual(path.with_suffix(".jsonl.tmp").read_bytes(), b'{"id": "new"}\n')

    def test_failed_temporary_write_does_not_replace_original(self) -> None:
        path = self.root / "rows.jsonl"
        path.write_bytes(b"old\n")
        with patch.object(Path, "write_text", side_effect=OSError("write failed")):
            with patch.object(Path, "replace") as replaced:
                with self.assertRaisesRegex(OSError, "write failed"):
                    quality_gate._write_jsonl(path, [{"id": "new"}])
        replaced.assert_not_called()
        self.assertEqual(path.read_bytes(), b"old\n")

    def test_serialization_failure_does_not_truncate_existing_files(self) -> None:
        path = self.root / "rows.jsonl"
        temporary = path.with_suffix(".jsonl.tmp")
        for atomic in (False, True):
            with self.subTest(atomic=atomic):
                path.write_bytes(b"old\n")
                temporary.write_bytes(b"stale\n")
                with self.assertRaises(TypeError):
                    jsonl.write_jsonl(path, [{"invalid": object()}], atomic=atomic)
                self.assertEqual(path.read_bytes(), b"old\n")
                self.assertEqual(temporary.read_bytes(), b"stale\n")

    def test_readers_preserve_order_blank_lines_unicode_and_values(self) -> None:
        path = self.root / "rows.jsonl"
        readers = (jsonl.read_jsonl, merge_candidates.read_jsonl)
        for text, expected in (
            ("", []),
            ("\n \t\r\n", []),
            ('\n \t\r\n{"id":"b","text":"雪"}\r\n\n{"id":"a"}\n',
             [{"id": "b", "text": "雪"}, {"id": "a"}]),
            ('{}\v[]\fnull\r1\n', [{}, [], None, 1]),
        ):
            path.write_text(text, encoding="utf-8")
            for reader in readers:
                with self.subTest(reader=reader, text=text):
                    self.assertEqual(list(reader(path)), expected)

    def test_reader_is_lazy_and_keeps_json_decode_error_details(self) -> None:
        path = self.root / "rows.jsonl"
        records = merge_candidates.read_jsonl(path)
        path.write_text('{"id":"ok"}\n \t\n  invalid\n', encoding="utf-8")
        self.assertEqual(next(records), {"id": "ok"})
        with self.assertRaises(json.JSONDecodeError) as error:
            next(records)
        self.assertEqual(error.exception.doc, "  invalid")
        self.assertEqual((error.exception.lineno, error.exception.colno), (1, 3))
        with self.assertRaises(FileNotFoundError):
            list(jsonl.read_jsonl(self.root / "missing.jsonl"))

    def test_release_index_retains_id_filtering_and_last_record(self) -> None:
        path = self.root / "rows.jsonl"
        path.write_text(
            '\n{"id":"a","v":1}\n{}\n{"id":0}\n{"id":false}\n'
            '{"id":null}\n{"id":""}\n{"id":2}\n{"id":"a","v":2}\n',
            encoding="utf-8",
        )
        self.assertEqual(release.load_jsonl_by_id(path), {
            "a": {"id": "a", "v": 2}, "2": {"id": 2},
        })

    def test_curate_reader_still_counts_nonblank_rows_without_ids(self) -> None:
        path = self.root / "rows.jsonl"
        path.write_text('\n{}\n{"id":"a"}\n \t\n{"id":"a"}\n', encoding="utf-8")
        self.assertEqual(curate_pool.merge_records([path]), ([{"id": "a"}], 2))

    def test_evidence_writer_preserves_order_duplicates_and_atomicity(self) -> None:
        path = self.root / "rows.jsonl"
        records = [{"id": "b"}, {"id": "missing"}, {"id": "a"}, {"id": "b"}]
        output = {"a": {"id": "a"}, "b": {"id": "b"}}
        path.write_bytes(b"old\n")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                evidence._write_ordered(path, records, output)
        self.assertEqual(path.read_bytes(), b"old\n")
        expected = b'{"id": "b"}\n{"id": "a"}\n{"id": "b"}\n'
        self.assertEqual(path.with_suffix(".jsonl.tmp").read_bytes(), expected)
        evidence._write_ordered(path, records, output)
        self.assertEqual(path.read_bytes(), expected)
        self.assertFalse(path.with_suffix(".jsonl.tmp").exists())
        evidence._write_ordered(path, records, {})
        self.assertEqual(path.read_bytes(), b"")

    def test_merge_cli_preserves_bytes_and_empty_output(self) -> None:
        source = self.root / "input.jsonl"
        out = self.root / "nested" / "out.jsonl"
        for text, expected in (
            ("\n \t\n", b""),
            ('{"id":"b"}\n\n{"id":"a","z":"雪"}\n{"id":"b","a":1}\n',
             '{"id": "a", "z": "雪"}\n{"a": 1, "id": "b"}\n'.encode("utf-8")),
        ):
            with self.subTest(text=text), contextlib.redirect_stdout(io.StringIO()):
                source.write_text(text, encoding="utf-8")
                self.assertEqual(merge_candidates.main(["--in", str(source), "--out", str(out)]), 0)
                self.assertEqual(out.read_bytes(), expected)

    def test_empty_cli_outputs_retain_distinct_contracts(self) -> None:
        source = self.root / "empty.jsonl"
        source.write_text("\n \t\n", encoding="utf-8")
        for module in (recovery, release, curate_pool, evaluate):
            with self.subTest(module=module.__name__), contextlib.redirect_stdout(io.StringIO()):
                out = self.root / module.__name__ / "out.jsonl"
                if module in (recovery, release):
                    args = ["--tasks", str(source), "--evaluations", str(source),
                            "--report", str(out.with_suffix(".report.json"))]
                else:
                    args = ["--tasks" if module is evaluate else "--in", str(source)]
                if module is evaluate:
                    args.extend(["--work-dir", str(self.root / "work"),
                                 "--patch-dir", str(self.root / "patches")])
                self.assertEqual(module.main([*args, "--out", str(out)]), 1 if module is release else 0)
                self.assertEqual(out.read_bytes(), b"\n" if module is evaluate else b"")

    def test_evaluate_reader_preserves_single_json_and_jsonl_modes(self) -> None:
        path = self.root / "tasks.jsonl"
        path.write_text('\n{"id":"a"}\n\n{"id":"b"}\n', encoding="utf-8")
        self.assertEqual(evaluate.load_tasks(None, path), [{"id": "a"}, {"id": "b"}])
        path.write_text('{\n "id": "single"\n}\n', encoding="utf-8")
        self.assertEqual(evaluate.load_tasks(path, None), [{"id": "single"}])
        with self.assertRaisesRegex(ValueError, "Either --task or --tasks is required"):
            evaluate.load_tasks(None, None)

    def test_resume_readers_keep_completed_empty_ids_and_skip_blank_lines(self) -> None:
        source = self.root / "input.jsonl"
        source.write_text('\n{"id":"a"}\n \t\n{}\n', encoding="utf-8")
        judgments = self.root / "judgments.jsonl"
        judgments.write_text("\n", encoding="utf-8")
        for module in (quality_gate, evidence):
            with self.subTest(module=module.__name__):
                out = self.root / (module.__name__ + ".jsonl")
                ready = {"status": "ready", "patch_complete": True,
                         "version": evidence.QUALITY_EVIDENCE_VERSION}
                prior = [{"id": "a", "quality_evidence": ready}, {"quality_evidence": ready}]
                contents = "\n \t\n" + "\n\n".join(json.dumps(item) for item in prior) + "\n"
                out.write_text(contents, encoding="utf-8")
                args = ["--in", str(source), "--out", str(out), "--resume"]
                args += (["--judgments", str(judgments)] if module is quality_gate
                         else ["--repo-cache", str(self.root / "cache")])
                with patch.object(evidence.GitRepoCache, "evidence_for", side_effect=AssertionError("already complete")):
                    self.assertEqual(module.main(args), 0)
                self.assertEqual(out.read_text(encoding="utf-8"), contents)
                self.assertFalse(out.with_suffix(".jsonl.tmp").exists())

    def test_empty_quality_and_evidence_inputs_do_not_create_output(self) -> None:
        source = self.root / "empty.jsonl"
        source.write_text("\n \t\n", encoding="utf-8")
        for module in (quality_gate, evidence):
            with self.subTest(module=module.__name__):
                out = self.root / (module.__name__ + ".jsonl")
                args = ["--in", str(source), "--out", str(out)]
                args += (["--judgments", str(source)] if module is quality_gate
                         else ["--repo-cache", str(self.root / "cache")])
                self.assertEqual(module.main(args), 0)
                self.assertFalse(out.exists())

    def test_release_evaluations_keep_json_and_jsonl_contracts(self) -> None:
        path = self.root / "evaluations.jsonl"
        for text in ('{\n"id":"a"\n}\n', '[{"id":"a"},null]\n',
                     '\n{"id":"a"}\n \t\nnull\n'):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(release.load_evaluations([path]), {"a": {"id": "a"}})

    def test_external_judgments_keep_physical_line_numbers(self) -> None:
        path = self.root / "judgments.jsonl"
        path.write_text('\n \t\n{"id":"a"}\n', encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            quality_gate.load_external_judgments([path])
        self.assertEqual(str(error.exception), f"{path}:3: expected id, model, and verdict object")


if __name__ == "__main__":
    unittest.main()
