#!/usr/bin/env python3
"""Maphesto Textual TUI."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static

from maphesto_core import (
    APP_HOME, LIMIT, AuditResult, CleanupPreview, Control, OutputSelection, Session,
    audit_session, fmt_bytes, inspect_cleanup_target, load_config, read_session_records,
    save_config, trash_cleanup_target,
)


def middle_ellipsis(value: str, width: int = 68) -> str:
    if len(value) <= width:
        return value
    side = (width - 1) // 2
    return f"{value[:side]}…{value[-side:]}"


class CommandStrip(Static):
    pass


class ResponsiveMixin:
    async def _sync_terminal_floor(self) -> None:
        too_small = self.size.width < 80 or self.size.height < 24
        self.set_class(too_small, "too-small")
        if not self.query(".resize-notice"):
            await self.mount(Static("Terminal too small\nNeed 80 × 24", classes="resize-notice"))

    async def on_resize(self) -> None:
        await self._sync_terminal_floor()

    async def on_screen_resume(self) -> None:
        await self._sync_terminal_floor()


class HelpModal(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape,question_mark", "dismiss_help", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal help-modal"):
            yield Static("KEYBOARD", classes="modal-title")
            yield Static(
                "Navigate\n  ↑/↓  j/k       move\n  ←/→  h/l       parent / open\n"
                "  Enter           activate\n  Space           select directory\n\n"
                "Actions\n  /               filter history\n  N               new output directory\n"
                "  A               automatic output\n  P               pause / resume\n  C               safe cancel\n"
                "  L               full log\n\nGlobal\n  Esc             dismiss / back\n"
                "  ?               this help\n  Ctrl+C          quit"
            )
        yield CommandStrip("Esc or ?  Close")

    def action_dismiss_help(self) -> None:
        self.app.pop_screen()


class Brand(Static):
    def render(self) -> str:
        return "M A P H E S T O\nconversion · batching"


class HomeScreen(ResponsiveMixin, Screen):
    BINDINGS = [Binding("n", "new_batch", "New batch run"), Binding("j,down", "menu_down", "Down", show=False), Binding("k,up", "menu_up", "Up", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="home-center"):               
            yield Brand(id="brand")
            yield ListView(ListItem(Label("New batch run"), id="menu-new"), ListItem(Label("History"), id="menu-history"), ListItem(Label("Settings"), id="menu-settings"), id="main-menu")
            yield Static(self.recent_summary(), id="home-status")
        yield CommandStrip("↑/↓ or j/k  Move    Enter  Open    Ctrl+C  Quit")

    def on_mount(self) -> None:
        self.query_one("#main-menu", ListView).focus()

    def recent_summary(self) -> str:
        if getattr(self.app, "completion_summary", ""):
            return self.app.completion_summary
        records = read_session_records()
        if not records:
            return "No previous runs"
        row = records[0]
        return f"Latest: {row.get('status', 'unknown')} · {row.get('files', 0)} files · {row.get('errors', 0)} errors"

    def action_menu_down(self) -> None:
        self.query_one("#main-menu", ListView).action_cursor_down()

    def action_menu_up(self) -> None:
        self.query_one("#main-menu", ListView).action_cursor_up()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        actions = {"menu-new": self.action_new_batch, "menu-history": lambda: self.app.push_screen(HistoryScreen()), "menu-settings": lambda: self.app.push_screen(SettingsScreen())}
        action = actions.get(event.item.id)
        if action:
            action()

    def action_new_batch(self) -> None:
        self.app.output_selection = OutputSelection.for_input(self.app.launch_dir)
        self.app.push_screen(DirectoryPickerScreen(self.app.launch_dir))


class DirectoryBrowser(ResponsiveMixin, Screen):
    """Single-pane immediate-child browser shared by input/output flows."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("left,h", "parent", "Parent"), Binding("right,l", "open", "Open"), Binding("space", "choose", "Choose"), Binding("j", "down", "Down", show=False), Binding("k", "up", "Up", show=False)]
    purpose = "directory"
    command_text = "←/h Parent   →/l/Enter Open   Space Select current/highlighted folder   ? Help"

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.current_directory = root.expanduser().resolve()

    def compose(self) -> ComposeResult:
        yield Static("", id="path-title")
        yield ListView(id="directory-list")
        yield CommandStrip(self.command_text)

    def on_mount(self) -> None:
        self.show_directory(self.current_directory)
        self.query_one("#directory-list", ListView).focus()

    def show_directory(self, path: Path) -> None:
        path = path.expanduser().resolve()
        listing = self.query_one("#directory-list", ListView)
        listing.clear()
        self.current_directory = path
        self.query_one("#path-title", Static).update(f"{self.purpose.upper()}  {middle_ellipsis(str(path))}")
        try:
            children = sorted((p for p in path.iterdir() if not p.name.startswith(".")), key=lambda p: (not p.is_dir(), p.name.casefold()))
            for child in children:
                if child.is_dir():
                    listing.append(ListItem(Label(f"{child.name}/"), name=str(child)))
                else:
                    try:
                        size = fmt_bytes(child.stat().st_size)
                    except OSError:
                        size = "?"
                    listing.append(ListItem(Label(f"{child.name}  {size}"), name=str(child), classes="file-row"))
            if children:
                listing.index = 0
            else:
                listing.append(ListItem(Label("Empty directory"), disabled=True))
        except OSError as exc:
            listing.append(ListItem(Label(f"Cannot inspect: {exc}"), disabled=True))

    def highlighted_path(self) -> Path | None:
        item = self.query_one("#directory-list", ListView).highlighted_child
        return Path(item.name) if item and item.name else None

    def action_down(self) -> None:
        self.query_one("#directory-list", ListView).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#directory-list", ListView).action_cursor_up()

    def action_parent(self) -> None:
        if self.current_directory.parent != self.current_directory:
            self.show_directory(self.current_directory.parent)

    def action_open(self) -> None:
        path = self.highlighted_path()
        if path and path.is_dir():
            self.show_directory(path)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_open()

    def action_back(self) -> None:
        self.app.pop_screen()


class DirectoryPickerScreen(DirectoryBrowser):
    purpose = "Input"

    def action_choose(self) -> None:
        chosen = self.highlighted_path()
        if chosen and chosen.is_dir():
            self.current_directory = chosen.resolve()
        self.app.output_selection.change_input(self.current_directory)
        self.app.push_screen(ConfirmationModal())

    def action_select_directory(self) -> None:
        self.action_choose()


class ConfirmationModal(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        choice = self.app.output_selection
        with Vertical(classes="modal"):
            yield Static("PREPARE NEW BATCH RUN", classes="modal-title")
            yield Static(f"Input\n{middle_ellipsis(str(choice.input_dir), 58)}\n\nOutput base\n{middle_ellipsis(str(choice.output_base), 58)}", id="confirm-paths")
            yield ListView(ListItem(Label("Start"), id="confirm-start"), ListItem(Label("Change output"), id="confirm-output"), ListItem(Label("Cancel"), id="confirm-cancel"), id="confirm-menu")
        yield CommandStrip("↑/↓ Move   Enter Activate   Esc Cancel")

    def on_mount(self) -> None:
        self.query_one("#confirm-menu", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "confirm-start":
            choice = self.app.output_selection
            self.app.pop_screen()
            self.app.pop_screen()
            self.app.push_screen(ProgressScreen(choice.input_dir, choice.output_base))
        elif event.item.id == "confirm-output":
            self.app.push_screen(OutputChooserScreen(self.app.output_selection.output_base))
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class OutputChooserScreen(DirectoryBrowser):
    purpose = "Output base"
    command_text = "←/h Parent   →/l Open   Space Select   N New folder   A Automatic"
    BINDINGS = DirectoryBrowser.BINDINGS + [Binding("n", "new_directory", "New directory"), Binding("a", "automatic", "Automatic")]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Input(placeholder="New directory name", id="new-directory", classes="hidden")

    def action_choose(self) -> None:
        chosen = self.highlighted_path()
        if chosen and chosen.is_dir():
            self.current_directory = chosen.resolve()
        self.app.output_selection.choose_output(self.current_directory)
        self.app.pop_screen()
        self.app.screen.query_one("#confirm-paths", Static).update(f"Input\n{middle_ellipsis(str(self.app.output_selection.input_dir), 58)}\n\nOutput base\n{middle_ellipsis(str(self.current_directory), 58)}")

    def action_new_directory(self) -> None:
        field = self.query_one("#new-directory", Input)
        field.remove_class("hidden")
        field.focus()

    def action_automatic(self) -> None:
        self.app.output_selection.reset_automatic()
        self.app.pop_screen()
        choice = self.app.output_selection
        paths = self.app.screen.query("#confirm-paths")
        if paths:
            paths.first(Static).update(
                f"Input\n{middle_ellipsis(str(choice.input_dir), 58)}\n\nOutput base · automatic\n{middle_ellipsis(str(choice.output_base), 58)}"
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not name or name in {".", ".."} or Path(name).name != name or "\0" in name:
            self.notify("Invalid directory name", severity="error")
            return
        try:
            created = self.current_directory / name
            created.mkdir()
        except OSError as exc:
            self.notify(str(exc), severity="error")
            return
        event.input.value = ""
        event.input.add_class("hidden")
        self.show_directory(created)


class SettingsScreen(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static("SETTINGS", classes="modal-title")
            yield Label("motionphoto2 executable")
            yield Input(value=str(self.app.config.get("motionphoto2", "")), id="binary")
            yield Label("Batch limit (bytes)")
            yield Input(value=str(self.app.config.get("batch_limit", LIMIT)), id="batch-limit")
            yield ListView(ListItem(Label("Save"), id="settings-save"), ListItem(Label("Cancel"), id="settings-cancel"), id="settings-actions")
        yield CommandStrip("Tab Next field   Enter Edit/activate   Esc Cancel")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_save() if event.item.id == "settings-save" else self.action_cancel()

    def action_save(self) -> None:
        binary = Path(self.query_one("#binary", Input).value).expanduser()
        try:
            limit = int(self.query_one("#batch-limit", Input).value)
        except ValueError:
            self.notify("Batch limit must be integer bytes", severity="error")
            return
        if not binary.is_file() or not os.access(binary, os.X_OK):
            self.notify("Executable not found or not executable", severity="error")
            return
        if limit <= 0:
            self.notify("Batch limit must be greater than zero", severity="error")
            return
        self.app.config = {"motionphoto2": str(binary.resolve()), "batch_limit": limit}
        save_config(self.app.config)
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ProgressScreen(ResponsiveMixin, Screen):
    BINDINGS = [Binding("p", "toggle_pause", "Pause"), Binding("c", "cancel", "Cancel"), Binding("l", "log", "Log")]

    def __init__(self, input_dir: Path, output: Path) -> None:
        super().__init__()
        self.input_dir, self.output = input_dir, output
        self.control = Control()
        self.log_lines: list[str] = []
        self.error_records: list[dict] = []
        self.started_at = time.monotonic()
        self.phase = "Starting"

    def compose(self) -> ComposeResult:
        yield Static("PROCESSING ENGINE · RUNNING", id="path-title")
        yield Static("Starting  ·  elapsed 00:00", id="run-state")
        yield Static("Groups 0/0   Prepared 0   Output 0 B   Errors 0", id="metrics")
        yield Static("Batch  Waiting", id="batch-card")
        yield Static("Item   —", id="current-item")
        yield RichLog(markup=False, highlight=False, max_lines=40, id="live-files")
        yield CommandStrip("P Pause/resume   C Safe cancel   L Full log")

    def on_mount(self) -> None:
        self.set_interval(1, self.update_elapsed)
        threading.Thread(target=self.run_session, daemon=True).start()

    def update_elapsed(self) -> None:
        seconds = int(time.monotonic() - self.started_at)
        self.query_one("#run-state", Static).update(
            f"{self.phase}  ·  elapsed {seconds // 60:02d}:{seconds % 60:02d}"
        )

    def run_session(self) -> None:
        try:
            session = Session(self.input_dir, self.output, Path(self.app.config["motionphoto2"]), self.receive_event, self.control)
            session.limit = int(self.app.config.get("batch_limit", LIMIT))
            session.process()
        except Exception as exc:
            self.receive_event({"kind": "fatal", "message": str(exc)})

    def receive_event(self, payload: dict) -> None:
        self.app.call_from_thread(self.update_ui, payload)

    def update_ui(self, payload: dict) -> None:
        if payload["kind"] == "log":
            self.log_lines.append(payload["message"])
            self.query_one("#live-files", RichLog).write(payload["message"])
        elif payload["kind"] == "progress":
            self.phase = payload["phase"]
            self.query_one("#metrics", Static).update(
                f"Groups {payload['groups_done']}/{payload['groups_total']}   Prepared {payload['files']}   "
                f"Output {fmt_bytes(payload['bytes'])}   Errors {payload['errors']}"
            )
            self.query_one("#current-item", Static).update(f"Item   {middle_ellipsis(payload.get('current', '—'))}")
            self.query_one("#batch-card", Static).update(
                f"Batch  {payload.get('batch_name') or 'Waiting'} · {fmt_bytes(payload['batch_size'])} / {fmt_bytes(int(self.app.config.get('batch_limit', LIMIT)))}"
            )
        elif payload["kind"] == "manifest" and payload.get("status") == "error":
            self.error_records.append(payload)
        elif payload["kind"] == "done":
            stats = payload["stats"]
            if not stats.get("errors") and stats.get("status") == "completed":
                self.app.completion_summary = f"Completed: {stats['files']} files · {fmt_bytes(stats['bytes'])}"
                self.app.pop_screen()
            else:
                self.app.push_screen(ErrorScreen(self.log_lines, stats, self.error_records))
        elif payload["kind"] == "fatal":
            self.log_lines.append(f"FATAL: {payload['message']}")
            self.app.push_screen(ErrorScreen(self.log_lines, {"errors": 1, "status": "fatal"}, self.error_records))

    def action_toggle_pause(self) -> None:
        if self.control.resume.is_set():
            self.control.resume.clear()
            self.query_one("#path-title", Static).update("PROCESSING ENGINE · PAUSED")
        else:
            self.control.resume.set()
            self.query_one("#path-title", Static).update("PROCESSING ENGINE · RUNNING")

    def action_cancel(self) -> None:
        self.app.push_screen(CancelModal(self.control))

    def action_log(self) -> None:
        self.app.push_screen(LogScreen("\n".join(self.log_lines)))


class CancelModal(ResponsiveMixin, ModalScreen):
    def __init__(self, control: Control) -> None:
        super().__init__()
        self.control = control

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static("Finish current file, then cancel?")
            yield ListView(ListItem(Label("Keep processing"), id="cancel-no"), ListItem(Label("Cancel safely"), id="cancel-yes"), id="cancel-actions")

    def on_mount(self) -> None:
        actions = self.query_one("#cancel-actions", ListView)
        actions.index = 0
        actions.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "cancel-yes":
            self.control.cancel.set()
            self.control.resume.set()
        self.app.pop_screen()


class ErrorScreen(ResponsiveMixin, Screen):
    BINDINGS = [Binding("l", "log", "Full log"), Binding("h", "home", "Home")]

    def __init__(self, lines: list[str], stats: dict, records: list[dict] | None = None) -> None:
        super().__init__()
        self.lines, self.stats, self.records = lines, stats, records or []

    def compose(self) -> ComposeResult:
        yield Static(f"PROCESSING ERRORS · {self.stats.get('errors', 0)}", id="path-title")
        rows = self.records or [
            {"error": line} for line in self.lines if "ERROR" in line or "FATAL" in line
        ]
        yield ListView(*(ListItem(Label(f"{row.get('source', 'session')}: {row.get('error', 'error')}"), name=json.dumps(row)) for row in rows), id="error-list")
        yield CommandStrip("↑/↓ Navigate   Enter Detail   L Full log   H Home")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.app.push_screen(LogScreen(event.item.name or ""))

    def action_log(self) -> None:
        self.app.push_screen(LogScreen("\n".join(self.lines)))

    def action_home(self) -> None:
        while not isinstance(self.app.screen, HomeScreen):
            self.app.pop_screen()


class HistoryScreen(ResponsiveMixin, Screen):
    BINDINGS = [Binding("escape,left,h", "back", "Back"), Binding("slash", "search", "Search"), Binding("c", "cleanup", "Cleanup")]

    def compose(self) -> ComposeResult:
        yield Static("HISTORY", id="path-title")
        yield Input(placeholder="Search history", id="history-filter", classes="hidden")
        yield ListView(id="history-list")
        yield Static("", id="history-count")
        yield CommandStrip("/ Filter   ↑/↓ Navigate   Enter Details   ←/h Back   ? Help")

    def on_mount(self) -> None:
        self.records: list[dict] = []
        self.audits: dict[str, AuditResult] = {}
        threading.Thread(target=self.load_history, daemon=True).start()

    def load_history(self) -> None:
        records = read_session_records()
        audits = {str(row.get("session_id", "")): audit_session(row) for row in records}
        self.app.call_from_thread(self.set_history_records, records, audits)

    def set_history_records(self, records: list[dict], audits: dict[str, AuditResult]) -> None:
        self.records, self.audits = records, audits
        self.refresh_history()

    def refresh_history(self, query: str = "") -> None:
        listing = self.query_one("#history-list", ListView)
        listing.clear()
        records = self.records
        self.visible_records: list[dict] = []
        shown = 0
        for row in records:
            audit = self.audits.get(str(row.get("session_id", "")))
            recovery = f" · {', '.join(audit.issues)}" if audit and audit.issues else ""
            text = f"{row.get('status', '?'):22} {row.get('finished', row.get('started', '?')):25} {row.get('files', 0):5} {middle_ellipsis(row.get('output', ''), 40)}{recovery}"
            needle = query if any(char.isupper() for char in query) else query.casefold()
            haystack = text if any(char.isupper() for char in query) else text.casefold()
            if needle in haystack:
                listing.append(ListItem(Label(text), name=str(len(self.visible_records))))
                self.visible_records.append(row)
                shown += 1
        if not records:
            listing.append(ListItem(Label("No runs yet. Esc returns Home."), disabled=True))
        elif not shown:
            listing.append(ListItem(Label("No matching runs. Esc clears filter."), disabled=True))
        self.query_one("#history-count", Static).update(f"{shown}/{len(records)} runs")

    def action_search(self) -> None:
        field = self.query_one("#history-filter", Input)
        field.remove_class("hidden")
        field.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_history(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.open_current()

    def open_current(self) -> None:
        item = self.query_one("#history-list", ListView).highlighted_child
        if item and item.name:
            try:
                row = self.visible_records[int(item.name)]
            except (IndexError, ValueError):
                return
            self.app.push_screen(SessionDetailScreen(Path(row.get("summary", "")), row, self.audits.get(str(row.get("session_id", "")))))

    def action_cleanup(self) -> None:
        self.open_current()

    def action_back(self) -> None:
        field = self.query_one("#history-filter", Input)
        if not field.has_class("hidden"):
            field.value = ""
            field.add_class("hidden")
            self.query_one("#history-list", ListView).focus()
        else:
            self.app.pop_screen()


class SessionDetailScreen(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("l", "log", "Log"), Binding("c", "cleanup", "Cleanup")]

    def __init__(self, summary: Path, record: dict | None = None, audit: AuditResult | None = None) -> None:
        super().__init__()
        self.summary = summary
        self.record = record or self.read_record()
        self.audit = audit or audit_session(self.record)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal detail-modal"):
            yield Static("SESSION DETAILS", classes="modal-title")
            yield VerticalScroll(Static(self.details(), id="detail-content"))
        yield CommandStrip("C Cleanup   L Full log   Esc Back")

    def read_record(self) -> dict:
        try:
            return json.loads(self.summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def details(self) -> str:
        try:
            data = json.loads(self.summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = self.record
        recovery = ", ".join(self.audit.issues) or "ready"
        return "\n".join(f"{key}: {value}" for key, value in data.items()) + f"\nrecovery: {recovery}\nlog: {self.summary.parent / 'events.log'}"

    def action_log(self) -> None:
        try:
            content = (self.summary.parent / "events.log").read_text(encoding="utf-8")
        except OSError as exc:
            content = str(exc)
        self.app.push_screen(LogScreen(content))

    def action_cleanup(self) -> None:
        self.app.push_screen(CleanupChoiceModal(self))

    def action_back(self) -> None:
        self.app.pop_screen()


class CleanupChoiceModal(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(self, detail: SessionDetailScreen) -> None:
        super().__init__()
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static("CLEANUP", classes="modal-title")
            yield Static("Move exact recorded target to OS Trash. Metadata stays.")
            yield ListView(
                ListItem(Label("Move source to Trash"), id="cleanup-input"),
                ListItem(Label("Move prepared output to Trash"), id="cleanup-output"),
                ListItem(Label("Cancel"), id="cleanup-cancel"), id="cleanup-choice",
            )
        yield CommandStrip("↑/↓ Move   Enter Inspect target   Esc Back")

    def on_mount(self) -> None:
        self.query_one("#cleanup-choice", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "cleanup-cancel":
            self.action_cancel()
            return
        kind = "input" if event.item.id == "cleanup-input" else "output"
        threading.Thread(target=self.inspect_target, args=(kind,), daemon=True).start()

    def inspect_target(self, kind: str) -> None:
        try:
            preview = inspect_cleanup_target(kind, self.detail.record, self.app.launch_dir)
        except (OSError, ValueError) as exc:
            self.app.call_from_thread(self.notify, f"Cleanup unavailable: {exc}", severity="error")
            return
        self.app.call_from_thread(self.app.push_screen, CleanupConfirmModal(preview, self.detail))

    def action_cancel(self) -> None:
        self.app.pop_screen()


class CleanupConfirmModal(ResponsiveMixin, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, preview: CleanupPreview, detail: SessionDetailScreen) -> None:
        super().__init__()
        self.preview, self.detail = preview, detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            yield Static("MOVE TO TRASH?", classes="modal-title")
            yield Static(f"{middle_ellipsis(str(self.preview.target), 58)}\n{self.preview.file_count} files · {fmt_bytes(self.preview.bytes)}")
            yield ListView(
                ListItem(Label("Cancel"), id="trash-cancel"),
                ListItem(Label("Move to Trash"), id="trash-confirm"), id="trash-actions",
            )
        yield CommandStrip("Enter Activate   Esc Cancel")

    def on_mount(self) -> None:
        actions = self.query_one("#trash-actions", ListView)
        actions.index = 0
        actions.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id == "trash-cancel":
            self.action_cancel()
            return
        threading.Thread(target=self.move_target, daemon=True).start()

    def move_target(self) -> None:
        result = trash_cleanup_target(self.preview)
        self.app.call_from_thread(self.finish_move, result.success, result.error)

    def finish_move(self, success: bool, error: str) -> None:
        if success:
            self.detail.audit = audit_session(self.detail.record)
            self.app.pop_screen()
            self.app.pop_screen()
            self.detail.query_one("#detail-content", Static).update(self.detail.details())
            self.detail.notify("Moved to Trash. Recovery state refreshed.")
        else:
            self.notify(f"Could not move to Trash: {error}", severity="error")

    def action_cancel(self) -> None:
        self.app.pop_screen()


class LogScreen(ResponsiveMixin, Screen):
    BINDINGS = [Binding("escape,left,h", "back", "Back")]

    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        yield Static("LOG", id="path-title")
        yield RichLog(markup=False, highlight=False, id="full-log")
        yield CommandStrip("Esc/←/h Back")

    def on_mount(self) -> None:
        self.query_one("#full-log", RichLog).write(self.content)

    def action_back(self) -> None:
        self.app.pop_screen()


class MaphestoApp(App, inherit_bindings=False):
    TITLE = "Maphesto"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("question_mark", "help", "Help"),
    ]
    CSS = """
    * { color: #FFF6ED; }
    Screen { background: #0C0A09; }
    HomeScreen { align: center middle; }
    #path-title { height: 2; padding: 0 2; color: #B8A99D; background: #1C1917; }
    CommandStrip { dock: bottom; height: 1; padding: 0 2; color: #B8A99D; background: #1C1917; }
    #home-center { width: 58; height: auto; align: center middle; }
    #brand { height: 5; content-align: center middle; text-align: center; color: #F78335; text-style: bold; }
    #main-menu { height: 9; background: #1C1917; border-left: tall #E64F10; }
    ListItem { height: 3; padding: 1 2; }
    ListItem.--highlight { background: #7A2914; }
    ListItem:hover { background: #29211D; }
    #home-status { height: 3; padding: 1 2; color: #B8A99D; text-align: center; }
    #directory-list, #history-list, #error-list { height: 1fr; background: #0C0A09; }
    #directory-list ListItem, #history-list ListItem, #error-list ListItem { height: 1; padding: 0 2; }
    .file-row { color: #B8A99D; }
    .modal { width: 64; height: auto; max-height: 90%; padding: 1 2; background: #1C1917; border-left: tall #E64F10; }
    ModalScreen { align: center middle; background: #0C0A0990; }
    .modal-title { color: #F78335; text-style: bold; height: 2; }
    #confirm-paths { height: 7; color: #B8A99D; }
    #confirm-menu, #settings-actions, #cancel-actions { height: auto; }
    Input { margin: 1 0; background: #29211D; border: none; }
    .hidden { display: none; }
    #run-state { height: 3; padding: 1 2 0 2; color: #FAB477; }
    #metrics { height: 2; padding: 0 2; text-style: bold; }
    #batch-card, #current-item { height: 2; padding: 0 2; color: #B8A99D; }
    #live-files { height: 1fr; margin: 0 2; background: #1C1917; border-left: tall #E64F10; }
    #errors-card { color: #4ADE80; }
    #history-filter { margin: 0 2; }
    #history-count { height: 1; padding: 0 2; color: #B8A99D; text-align: right; }
    ErrorScreen #path-title { color: #F87171; text-style: bold; }
    .detail-modal { height: 80%; }
    .help-modal { height: 25; }
    #full-log { height: 1fr; background: #1C1917; }
    .resize-notice { display: none; }
    Screen.too-small > * { display: none; }
    Screen.too-small > .resize-notice {
        display: block;
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-align: center;
        color: #FAB477;
        text-style: bold;
        background: #0C0A09;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.launch_dir = Path.cwd().resolve()
        self.output_selection = OutputSelection.for_input(self.launch_dir)
        self.config = load_config()
        self.completion_summary = ""

    def on_mount(self) -> None:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        self.push_screen(HomeScreen())

    def action_help(self) -> None:
        if isinstance(self.screen, HelpModal):
            self.pop_screen()
        else:
            self.push_screen(HelpModal())


if __name__ == "__main__":
    MaphestoApp().run()
