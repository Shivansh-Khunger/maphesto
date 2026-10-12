from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maphesto import ConfirmationModal, DirectoryPickerScreen, HelpModal, HistoryScreen, HomeScreen, MaphestoApp, OutputChooserScreen, SettingsScreen


class TuiRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_quit_has_no_letter_or_ctrl_q_override(self) -> None:
        bindings = [binding[0] if isinstance(binding, tuple) else binding.key
                    for binding in (*HomeScreen.BINDINGS, *MaphestoApp.BINDINGS)]
        self.assertNotIn("q", bindings)
        self.assertNotIn("ctrl+q", bindings)

    def test_reserved_flow_control_keys_are_not_bound(self) -> None:
        bindings = [binding[0] if isinstance(binding, tuple) else binding.key
                    for binding in (*SettingsScreen.BINDINGS, *MaphestoApp.BINDINGS)]
        self.assertNotIn("ctrl+s", bindings)
        self.assertNotIn("ctrl+q", bindings)

    async def test_help_is_globally_discoverable(self) -> None:
        app = MaphestoApp()
        async with app.run_test() as pilot:
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpModal)

    async def test_single_pane_picker_hides_dotfiles_dims_files_and_selects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "child"
            child.mkdir()
            (child / "photo.heic").write_bytes(b"image")
            (root / ".secret").write_bytes(b"hidden")

            app = MaphestoApp()
            async with app.run_test() as pilot:
                app.push_screen(DirectoryPickerScreen(root=root))
                await pilot.pause()
                picker = app.screen
                picker.show_directory(child)
                await pilot.pause()

                self.assertEqual(child.resolve(), picker.current_directory)
                contents = picker.query_one("#directory-list")
                text = "\n".join(str(label.render()) for label in contents.query("Label"))
                self.assertIn("photo.heic", text)
                self.assertNotIn(".secret", text)
                await pilot.press("space")
                self.assertIsInstance(app.screen, ConfirmationModal)
                self.assertEqual(child.resolve(), app.output_selection.input_dir)

    async def test_space_on_file_selects_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "photo.heic").write_bytes(b"image")
            app = MaphestoApp()
            async with app.run_test() as pilot:
                app.push_screen(DirectoryPickerScreen(root))
                await pilot.pause()
                await pilot.press("space")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmationModal)
                self.assertEqual(root.resolve(), app.output_selection.input_dir)

    async def test_below_minimum_shows_resize_notice(self) -> None:
        app = MaphestoApp()
        async with app.run_test(size=(79, 23)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("too-small"))
            self.assertIn("80 × 24", str(app.screen.query_one(".resize-notice").render()))

    async def test_output_chooser_can_reset_to_automatic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            input_dir = root / "input"
            input_dir.mkdir()
            app = MaphestoApp()
            async with app.run_test() as pilot:
                app.output_selection.change_input(input_dir)
                app.output_selection.choose_output(root / "explicit")
                app.push_screen(OutputChooserScreen(root))
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
                self.assertTrue(app.output_selection.automatic)
                self.assertEqual(root, app.output_selection.output_base)

    async def test_empty_history_explains_next_action(self) -> None:
        with patch("maphesto.read_session_records", return_value=[]):
            app = MaphestoApp()
            async with app.run_test() as pilot:
                app.push_screen(HistoryScreen())
                await pilot.pause()
                text = "\n".join(str(label.render()) for label in app.screen.query("Label"))
                self.assertIn("No runs", text)

    async def test_picker_starts_at_launch_directory(self) -> None:
        app = MaphestoApp()
        async with app.run_test() as pilot:
            app.screen.action_new_batch()
            await pilot.pause()
            self.assertEqual(app.launch_dir, app.screen.current_directory)

    def test_no_header_footer_or_archive_terminology(self) -> None:
        source = Path(__import__("maphesto").__file__).read_text(encoding="utf-8")
        self.assertNotIn("Header", source)
        self.assertNotIn("Footer", source)
        self.assertNotIn("New session", source)
        self.assertNotIn("archive", source.lower())


if __name__ == "__main__":
    unittest.main()
