from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maphesto_core
from maphesto_core import (
    AuditResult, Control, OutputSelection, Session, UnsafeTrashTarget,
    audit_session, inspect_cleanup_target, trash_cleanup_target,
    move_recorded_target_to_trash, validate_trash_target,
)


class ArchiveCoreTests(unittest.TestCase):
    def test_batching_waits_for_motionphoto_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input" / "single"
            source.mkdir(parents=True)
            (source / "photo.heic").write_bytes(b"image")
            observed = []

            def run_after_scan(session: Session) -> None:
                observed.append(session.batch_no)

            with patch.object(maphesto_core, "APP_HOME", root / "state"):
                session = Session(root / "input", root / "output", root / "unused", lambda _: None, Control())
                with patch.object(session, "run_motionphoto", side_effect=lambda: run_after_scan(session)):
                    stats = session.process()

            self.assertEqual([0], observed)
            self.assertEqual("completed", stats["status"])
            self.assertEqual(1, stats["files"])

    def test_output_follows_input_until_explicit_then_can_reset(self) -> None:
        selection = OutputSelection.for_input(Path("/photos/first"))
        self.assertEqual(Path("/photos"), selection.output_base)

        selection.change_input(Path("/imports/second"))
        self.assertEqual(Path("/imports"), selection.output_base)

        selection.choose_output(Path("/exports"))
        selection.change_input(Path("/imports/third"))
        self.assertEqual(Path("/exports"), selection.output_base)
        self.assertFalse(selection.automatic)

        selection.reset_automatic()
        self.assertEqual(Path("/imports"), selection.output_base)
        self.assertTrue(selection.automatic)

    def test_single_file_group_archives_without_deleting_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            group = source / "group-one"
            group.mkdir(parents=True)
            original = group / "photo.heic"
            original.write_bytes(b"motion-photo")
            output = root / "output"
            events = []

            with patch.object(maphesto_core, "APP_HOME", root / "state"):
                stats = Session(
                    source, output, root / "unused-binary",
                    events.append, Control(),
                ).process()

            self.assertEqual("completed", stats["status"])
            self.assertTrue(original.exists())
            self.assertEqual(1, stats["files"])
            archived = list(output.glob("maphesto-*/*/photo.heic"))
            self.assertEqual(1, len(archived))
            self.assertEqual(b"motion-photo", archived[0].read_bytes())

    def test_core_has_no_textual_dependency(self) -> None:
        source = Path(maphesto_core.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from textual", source)

    def test_pair_staging_copies_when_cleanup_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "input" / "pair"
            group.mkdir(parents=True)
            files = [group / "photo.heic", group / "motion.mov"]
            for file in files:
                file.write_bytes(file.name.encode())

            with (
                patch.object(maphesto_core, "APP_HOME", root / "state"),
                patch.object(Session, "run_motionphoto", return_value=None),
            ):
                stats = Session(
                    root / "input", root / "output", root / "unused",
                    lambda event: None, Control(),
                ).process()

            self.assertEqual("completed", stats["status"])
            self.assertTrue(all(file.exists() for file in files))

    def test_session_metadata_lives_outside_media_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "input" / "single"
            group.mkdir(parents=True)
            (group / "photo.heic").write_bytes(b"image")
            state = root / "state"

            with patch.object(maphesto_core, "APP_HOME", state):
                session = Session(root / "input", root / "output", root / "unused", lambda _: None, Control())
                session.process()

            metadata = state / "sessions" / session.session_id
            self.assertTrue((metadata / "summary.json").is_file())
            self.assertTrue((metadata / "events.log").is_file())
            self.assertTrue((metadata / "manifest.jsonl").is_file())
            self.assertFalse((session.root / "summary.json").exists())
            self.assertFalse((session.root / "logs").exists())
            self.assertFalse((session.root / ".work").exists())

    def test_cancel_marks_wrapper_incomplete_and_preserves_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "input" / "pair"
            group.mkdir(parents=True)
            (group / "photo.heic").write_bytes(b"image")
            (group / "motion.mov").write_bytes(b"video")
            control = Control()
            control.cancel.set()

            with patch.object(maphesto_core, "APP_HOME", root / "state"):
                session = Session(root / "input", root / "output", root / "unused", lambda _: None, control)
                stats = session.process()

            self.assertEqual("cancelled", stats["status"])
            self.assertTrue((session.root / "INCOMPLETE").is_file())
            self.assertTrue((session.root / ".work").is_dir())

    def test_trash_guard_blocks_protected_and_allows_recorded_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            launch = root / "launch"
            launch.mkdir()
            target = root / "input"
            target.mkdir()
            output = root / "exports"
            output.mkdir()

            validated = validate_trash_target(target, target, launch, output)
            self.assertEqual(target, validated)
            for protected in (Path("/"), Path.home(), launch, output, root):
                with self.subTest(protected=protected):
                    with self.assertRaises(UnsafeTrashTarget):
                        validate_trash_target(protected, protected, launch, output)

    def test_trash_adapter_receives_only_validated_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "input"
            launch = root / "launch"
            output = root / "output"
            target.mkdir()
            launch.mkdir()
            output.mkdir()
            calls = []

            moved = move_recorded_target_to_trash(target, target, launch, output, calls.append)

            self.assertEqual(target, moved)
            self.assertEqual([str(target)], calls)

    def test_audit_exposes_missing_input_and_preserved_incomplete_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "missing-input"
            output = root / "maphesto-run"
            (output / ".work").mkdir(parents=True)
            record = {"session_id": "run", "input": str(input_dir), "output": str(output), "status": "cancelled"}

            audit = audit_session(record)

            self.assertIsInstance(audit, AuditResult)
            self.assertTrue(audit.missing_input)
            self.assertTrue(audit.incomplete_work)
            self.assertEqual("cancelled", audit.status)
            self.assertIn("missing input", audit.issues)
            self.assertIn("incomplete work", audit.issues)

    def test_completed_session_with_missing_output_is_stale(self) -> None:
        audit = audit_session({"session_id": "gone", "input": "/", "output": "/missing-maphesto-output", "status": "completed"})

        self.assertTrue(audit.stale)
        self.assertEqual("stale", audit.status)
        self.assertIn("missing output", audit.issues)

    def test_cleanup_preview_counts_exact_recorded_wrapper_and_trash_reports_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            launch = root / "launch"
            launch.mkdir()
            input_dir = root / "input"
            input_dir.mkdir()
            output_base = root / "exports"
            wrapper = output_base / "maphesto-run"
            (wrapper / "batch").mkdir(parents=True)
            (wrapper / "batch" / "prepared.jpg").write_bytes(b"12345")
            record = {"input": str(input_dir), "output": str(wrapper)}

            preview = inspect_cleanup_target("output", record, launch)
            result = trash_cleanup_target(preview, trash=lambda path: None)

            self.assertEqual(wrapper, preview.target)
            self.assertEqual(1, preview.file_count)
            self.assertEqual(5, preview.bytes)
            self.assertTrue(result.success)
            self.assertEqual(wrapper, result.target)

    def test_cleanup_preview_refuses_input_that_contains_output_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            launch = root / "launch"
            launch.mkdir()
            input_dir = root / "input"
            output_base = input_dir / "exports"
            output_base.mkdir(parents=True)
            record = {"input": str(input_dir), "output": str(output_base / "maphesto-run")}

            with self.assertRaises(UnsafeTrashTarget):
                inspect_cleanup_target("input", record, launch)

    def test_fatal_scan_error_finalizes_metadata_and_incomplete_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_input = root / "missing"
            with patch.object(maphesto_core, "APP_HOME", root / "state"):
                session = Session(missing_input, root / "output", root / "unused", lambda _: None, Control())
                stats = session.process()

            self.assertEqual("fatal", stats["status"])
            self.assertTrue((session.metadata_dir / "summary.json").is_file())
            self.assertTrue((session.root / "INCOMPLETE").is_file())

    def test_failed_file_copy_does_not_leave_empty_soul_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            source.write_bytes(b"image")
            with patch.object(maphesto_core, "APP_HOME", root / "state"):
                session = Session(root, root / "output", root / "unused", lambda _: None, Control())
                session.root.mkdir(parents=True)
                session.work.mkdir()
                with patch("maphesto_core.shutil.copy2", side_effect=OSError("copy failed")):
                    with self.assertRaises(OSError):
                        session.archive_file(source, "source.jpg")

            self.assertEqual([], [path for path in session.root.iterdir() if path.is_dir() and path.name != ".work"])


if __name__ == "__main__":
    unittest.main()
