"""Conversion and batching engine. No TUI dependencies."""


from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LIMIT = 1024**3
HEIC_SUFFIXES = {".heic", ".heif"}
APP_HOME = Path.home() / ".maphesto"
CONFIG_PATH = APP_HOME / "config.json"
SOUL_NAMES = [
    "Doctor_Strange",
    "Silver_Surfer",
    "Ghost_Rider",
    "Blackheart",
    "Scarlet_Witch",
    "Thor",
    "Loki",
    "Spider_Man",
    "Doctor_Doom",
    "Thanos",
    "Hulk",
    "Iron_Man",
    "Captain_Marvel",
    "Deadpool",
    "Wolverine",
    "Magneto",
    "Storm",
    "Jean_Grey",
    "Clea",
    "Mephisto",

    # Souls sold/given/taken by Mephisto
    "Achebe",
    "Algernon_Crowe",
    "Benedicto_de_Vica_Severtes",
    "Thing",
    "Ben_Reilly",
    "Brendan_Rathcoole",
    "Bronwen_Gryffn",
    "Carter_Slade",
    "Centurious",
    "Christopher_Daniels",
    "Craig_Simpson",
    "Cynthia_von_Doom",
    "Daniel_Lyons",
    "Dude_Jensen",
    "Eadmund_Porlock",
    "Elmar_Radd",
    "Elspeth_Cromwell",
    "Everett_Ross",
    "Francis_Castle",
    "Franklin_Richards",
    "Gudrun_Tyburn",
    "H_rpra",
    "Haasen",
    "Harry_Osborn",
    "Howard_Stark",
    "James_Mandarin",
    "Jartran_Radd",
    "Jonah_Marlo",
    "Joost_van_Straaten",
    "Judas_Traveller",
    "Karl_Mordo",
    "Linda_Littletrees",
    "Martha_Franklin",
    "Master_Pandemonium",
    "Daredevil",
    "Vengeance",
    "Miranda_Rand",
    "Naomi_Kale",
    "Norman_Osborn",
    "Ormond_Wychwood",
    "Orson_Randall",
    "Hood",
    "Phil_Coulson",
    "Ranulph_Haldane",
    "Ray_Carrigan",
    "Mister_Fantastic",
    "Ricky_London",
    "Robert_Blackthorne",
    "Roxanne_Simpson",
    "Shalla_Bal",
    "Sister_Sorrow",
    "Jack_O_Lantern",
    "Invisible_Woman",
    "Black_Panther",
    "Thunderbolt_Ross",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number):,} B"
        number /= 1024
    return f"{value:,} B"


def load_config() -> dict:
    defaults = {
        "motionphoto2": str(Path(__file__).with_name("motionphoto2")),
        "batch_limit": LIMIT,
    }
    try:
        defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


@dataclass
class OutputSelection:
    """Input/output choice with automatic-follow semantics."""

    input_dir: Path
    output_base: Path
    automatic: bool = True

    @classmethod
    def for_input(cls, input_dir: Path) -> "OutputSelection":
        input_dir = input_dir.expanduser().resolve()
        return cls(input_dir, input_dir.parent)

    def change_input(self, input_dir: Path) -> None:
        self.input_dir = input_dir.expanduser().resolve()
        if self.automatic:
            self.output_base = self.input_dir.parent

    def choose_output(self, output_base: Path) -> None:
        self.output_base = output_base.expanduser().resolve()
        self.automatic = False

    def reset_automatic(self) -> None:
        self.automatic = True
        self.output_base = self.input_dir.parent


class UnsafeTrashTarget(ValueError):
    pass


@dataclass(frozen=True)
class AuditResult:
    """Current recovery state for one recorded session; never mutates media."""

    session_id: str
    status: str
    input_exists: bool
    output_exists: bool
    missing_input: bool
    missing_output: bool
    incomplete_work: bool
    stale: bool
    fatal: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class CleanupPreview:
    """Validated exact Trash target plus user-facing size information."""

    kind: str
    target: Path
    recorded: Path
    launch_dir: Path
    output_base: Path
    file_count: int
    bytes: int


@dataclass(frozen=True)
class CleanupResult:
    target: Path
    success: bool
    error: str = ""


def audit_session(record: dict) -> AuditResult:
    """Inspect recorded paths without changing metadata, work, or media."""
    input_dir = Path(record.get("input", "")).expanduser()
    output = Path(record.get("output", "")).expanduser()
    input_exists = input_dir.is_dir()
    output_exists = output.is_dir()
    incomplete_work = output_exists and ((output / ".work").exists() or (output / "INCOMPLETE").exists())
    status = str(record.get("status", "unknown"))
    fatal = status == "fatal"
    issues = []
    if not input_exists:
        issues.append("missing input")
    if not output_exists:
        issues.append("missing output")
    if incomplete_work:
        issues.append("incomplete work")
    if fatal:
        issues.append("fatal")
    stale = status == "completed" and bool(issues)
    if stale:
        issues.insert(0, "stale")
    return AuditResult(
        session_id=str(record.get("session_id", "")), status="fatal" if fatal else ("stale" if stale else status),
        input_exists=input_exists, output_exists=output_exists,
        missing_input=not input_exists, missing_output=not output_exists,
        incomplete_work=incomplete_work, stale=stale, fatal=fatal, issues=tuple(issues),
    )


def _tree_size(target: Path) -> tuple[int, int]:
    """Count regular files and bytes without following symlinked directories."""
    if target.is_file():
        return 1, target.stat().st_size
    count, total = 0, 0
    for root, _dirs, files in os.walk(target, followlinks=False):
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
    return count, total


def inspect_cleanup_target(kind: str, record: dict, launch_dir: Path) -> CleanupPreview:
    """Validate one recorded cleanup target and calculate its Trash confirmation data."""
    if kind not in {"input", "output"}:
        raise ValueError("Cleanup kind must be input or output")
    input_dir = Path(record.get("input", "")).expanduser()
    output = Path(record.get("output", "")).expanduser()
    target = input_dir if kind == "input" else output
    output_base = output.parent
    safe = validate_trash_target(target, target, launch_dir, output_base)
    file_count, bytes_total = _tree_size(safe)
    return CleanupPreview(kind, safe, target, launch_dir, output_base, file_count, bytes_total)


def trash_cleanup_target(preview: CleanupPreview,
                         trash: Callable[[str], None] | None = None) -> CleanupResult:
    """Revalidate then move exactly one previewed path to OS Trash."""
    try:
        target = move_recorded_target_to_trash(
            preview.target, preview.recorded, preview.launch_dir, preview.output_base, trash,
        )
    except (OSError, UnsafeTrashTarget, ImportError, RuntimeError) as exc:
        return CleanupResult(preview.target, False, str(exc))
    return CleanupResult(target, True)


def validate_trash_target(target: Path, recorded: Path, launch_dir: Path,
                          output_base: Path) -> Path:
    """Return resolved target or reject targets unsafe for OS Trash."""
    target = target.expanduser()
    recorded = recorded.expanduser()
    if not target.exists() or target.is_symlink() or recorded.is_symlink():
        raise UnsafeTrashTarget("Target missing or symlinked")
    resolved = target.resolve()
    if resolved != recorded.resolve():
        raise UnsafeTrashTarget("Target differs from recorded path")
    protected = {Path("/"), Path.home().resolve(), launch_dir.resolve(), output_base.resolve()}
    if resolved in protected:
        raise UnsafeTrashTarget("Protected path cannot be moved to Trash")
    # Input must not contain output base; trashing it would also destroy output.
    try:
        output_base.resolve().relative_to(resolved)
    except ValueError:
        pass
    else:
        raise UnsafeTrashTarget("Target contains output base")
    return resolved


def move_recorded_target_to_trash(target: Path, recorded: Path, launch_dir: Path,
                                  output_base: Path,
                                  trash: Callable[[str], None] | None = None) -> Path:
    """Validate exact recorded path, then move it through OS Trash adapter."""
    safe = validate_trash_target(target, recorded, launch_dir, output_base)
    if trash is None:
        from send2trash import send2trash
        trash = send2trash
    trash(str(safe))
    return safe


def save_config(config: dict) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def read_session_records() -> list[dict]:
    registry = APP_HOME / "sessions.jsonl"
    if not registry.exists():
        return []
    records = []
    try:
        for line in registry.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return list(reversed(records))


class Control:
    def __init__(self) -> None:
        self.resume = threading.Event()
        self.resume.set()
        self.cancel = threading.Event()

    def wait(self) -> bool:
        while not self.resume.wait(0.2):
            if self.cancel.is_set():
                return False
        return not self.cancel.is_set()


class Session:
    """Conversion/batching engine. UI receives structured events through callback."""

    def __init__(self, input_dir: Path, output_parent: Path, binary: Path,
                 event: Callable[[dict], None], control: Control):
        self.input_dir = input_dir.expanduser().resolve()
        self.output_parent = output_parent.expanduser().resolve()
        self.binary = binary.expanduser().resolve()
        self.event, self.control = event, control
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.root = self.output_parent / f"maphesto-{self.session_id}"
        self.work = self.root / ".work"
        self.stage = self.work / "source"
        self.motion_out = self.work / "converted"
        self.deferred = self.work / "deferred"
        self.metadata_dir = APP_HOME / "sessions" / self.session_id
        self.events = self.metadata_dir / "events.log"
        self.manifest = self.metadata_dir / "manifest.jsonl"
        self.batch_no, self.batch_dir, self.batch_size = 0, None, 0
        self.stats = {"files": 0, "bytes": 0, "errors": 0, "started": now()}
        self.groups_total = 0
        self.groups_done = 0
        self.limit = int(load_config().get("batch_limit", LIMIT))

    def notify(self, message: str, level: str = "INFO", **fields) -> None:
        line = f"{now()} [{level}] {message}"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        with self.events.open("a", encoding="utf-8") as fh:
            fh.write(line + (" " + json.dumps(fields, ensure_ascii=False) if fields else "") + "\n")
        self.event({"kind": "log", "message": line, "level": level, **fields})

    def record(self, **fields) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        with self.manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": now(), **fields}, ensure_ascii=False) + "\n")
        self.event({"kind": "manifest", **fields})

    def progress(self, phase: str, current: str = "") -> None:
        self.event({"kind": "progress", "phase": phase, "current": current,
                    "groups_done": self.groups_done, "groups_total": self.groups_total,
                    "files": self.stats["files"], "bytes": self.stats["bytes"],
                    "errors": self.stats["errors"], "batch_size": self.batch_size,
                    "batch_name": self.batch_dir.name if self.batch_dir else ""})

    @staticmethod
    def checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def new_batch(self) -> None:
        self.batch_no += 1
        name = SOUL_NAMES[(self.batch_no - 1) % len(SOUL_NAMES)]
        cycle = (self.batch_no - 1) // len(SOUL_NAMES) + 1
        suffix = f"_{cycle:02d}" if cycle > 1 else ""
        self.batch_dir = self.root / f"{name}{suffix}"
        self.batch_size = 0
        self.notify(f"Started batch {self.batch_dir.name}")

    def archive_file(self, source: Path, relative_source: str, mode: str = "move") -> None:
        size = source.stat().st_size
        if self.batch_dir is None or (self.batch_size and self.batch_size + size > self.limit):
            self.new_batch()
        assert self.batch_dir
        destination = self.batch_dir / source.name
        if destination.exists():
            destination = self.batch_dir / f"{source.stem}_{uuid.uuid4().hex[:8]}{source.suffix}"
        digest = self.checksum(source)
        pending_dir = self.work / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending = pending_dir / f"{uuid.uuid4().hex}-{destination.name}"
        created_batch = False
        try:
            shutil.copy2(source, pending)
            if pending.stat().st_size != size or self.checksum(pending) != digest:
                raise OSError(f"Verification failed: {relative_source}")
            if not self.batch_dir.exists():
                self.batch_dir.mkdir(parents=True)
                created_batch = True
            os.replace(pending, destination)
        except Exception:
            pending.unlink(missing_ok=True)
            if created_batch:
                try:
                    self.batch_dir.rmdir()
                except OSError:
                    pass
            raise
        self.batch_size += size
        self.stats["files"] += 1
        self.stats["bytes"] += size
        self.record(status="archived", source=relative_source,
                    destination=str(destination.relative_to(self.root)), bytes=size,
                    sha256=digest, operation=mode)
        self.notify(f"Archived {relative_source} -> {destination.parent.name}/{destination.name}",
                    bytes=size, source=relative_source)
        self.progress("Archiving", relative_source)
        if size > self.limit:
            self.notify(f"File exceeds batch limit; kept alone in batch: {relative_source}", "WARN")

    def defer_file(self, source: Path, relative_source: str, group: str) -> None:
        """Stage non-motion output until motionphoto2 has finished."""
        target = self.deferred / group / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.record(status="deferred", source=relative_source, group=group)

    def run_motionphoto(self) -> None:
        if not any(self.stage.rglob("*")):
            self.notify("No two-file motion-photo groups found")
            return
        if not self.control.wait():
            return
        motion_out = self.motion_out
        self.notify("Running motionphoto2")
        self.progress("Converting", "motionphoto2")
        result = subprocess.run([
            str(self.binary), "--recursive", "--exif-match",
            "--input-directory", str(self.stage), "--output-directory", str(motion_out),
        ], text=True, capture_output=True)
        with self.events.open("a", encoding="utf-8") as fh:
            fh.write(result.stdout or "")
            fh.write(result.stderr or "")
        if result.returncode:
            raise RuntimeError(f"motionphoto2 exited with code {result.returncode}")
        if self.control.cancel.is_set():
            self.notify("Cancellation requested after motionphoto2; preserving converted work", "WARN")
            return
        converted = 0
        for file in sorted(motion_out.rglob("*")):
            if file.is_file() and not file.name.startswith("."):
                self.archive_file(file, str(file.relative_to(motion_out)))
                converted += 1
        if converted:
            shutil.rmtree(self.stage, ignore_errors=True)
        else:
            self.notify("motionphoto2 produced no files; preserving staged source", "ERROR")

    def process(self) -> dict:
        try:
            return self._process()
        except Exception as exc:
            self.stats["errors"] += 1
            self.stats["finished"] = now()
            self.stats["status"] = "fatal"
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self.work.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            try:
                self.record(status="error", source="session", error=str(exc))
                self.notify(f"Fatal session error: {exc}", "ERROR")
            except OSError:
                pass
            return self._finish_session()

    def _process(self) -> dict:
        self.output_parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir()
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.work.mkdir()
        self.stage.mkdir()
        groups = [p for p in sorted(self.input_dir.iterdir()) if p.is_dir() and not p.name.startswith(".")]
        self.groups_total = len(groups)
        self.record(status="session_started", input=str(self.input_dir), output=str(self.root))
        self.notify(f"Session {self.session_id} started", input=str(self.input_dir), output=str(self.root))
        self.progress("Scanning")
        for group in groups:
            if not self.control.wait():
                self.notify("Cancellation requested; stopping safely", "WARN")
                break
            files = [p for p in sorted(group.iterdir()) if p.is_file() and not p.name.startswith(".")]
            handled_for_delete = False
            self.progress("Processing", group.name)
            try:
                if len(files) == 1:
                    self.defer_file(files[0], str(files[0].relative_to(self.input_dir)), group.name)
                    handled_for_delete = True
                elif len(files) == 2:
                    target = self.stage / group.name
                    target.mkdir(parents=True, exist_ok=True)
                    for file in files:
                        shutil.copy2(file, target / file.name)
                        self.record(status="staged", source=str(file.relative_to(self.input_dir)), group=group.name)
                    self.notify(f"Staged motion-photo pair: {group.name}")
                    handled_for_delete = True
                else:
                    heics = [p for p in files if p.suffix.lower() in HEIC_SUFFIXES]
                    for file in heics:
                        self.defer_file(file, str(file.relative_to(self.input_dir)), group.name)
                    handled_for_delete = bool(heics)
                    if not heics:
                        self.notify(f"Skipped group with {len(files)} files and no HEIC: {group.name}", "WARN")
                    else:
                        self.notify(f"Copied {len(heics)} HEIC file(s): {group.name}")
            except Exception as exc:
                self.stats["errors"] += 1
                self.record(status="error", source=str(group.relative_to(self.input_dir)), error=str(exc))
                self.notify(f"Failed group {group.name}: {exc}", "ERROR")
            self.groups_done += 1
            self.progress("Processing", group.name)
        try:
            self.run_motionphoto()
        except Exception as exc:
            self.stats["errors"] += 1
            self.record(status="error", source="motionphoto2", error=str(exc))
            self.notify(str(exc), "ERROR")
        if not self.control.cancel.is_set():
            for file in sorted(self.deferred.rglob("*")):
                if file.is_file():
                    self.archive_file(file, str(file.relative_to(self.deferred)), mode="copy")
            for file in sorted(self.stage.rglob("*")):
                if file.is_file():
                    self.archive_file(file, str(file.relative_to(self.stage)))
        self.stats["finished"] = now()
        self.stats["status"] = "cancelled" if self.control.cancel.is_set() else ("completed_with_errors" if self.stats["errors"] else "completed")
        return self._finish_session()

    def _finish_session(self) -> dict:
        try:
            self.record(record_type="session_finished", **self.stats)
        except OSError:
            pass
        summary = self.metadata_dir / "summary.json"
        try:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            summary.write_text(json.dumps({"session_id": self.session_id, "input": str(self.input_dir),
                                           "output": str(self.root), **self.stats}, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        try:
            if self.stats["status"] == "completed":
                shutil.rmtree(self.work, ignore_errors=True)
            else:
                self.root.mkdir(parents=True, exist_ok=True)
                (self.root / "INCOMPLETE").write_text(self.stats["status"] + "\n", encoding="utf-8")
        except OSError:
            pass
        try:
            APP_HOME.mkdir(parents=True, exist_ok=True)
            with (APP_HOME / "sessions.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"session_id": self.session_id, "summary": str(summary),
                                     "input": str(self.input_dir), "output": str(self.root), **self.stats}) + "\n")
        except OSError:
            pass
        try:
            self.notify(f"Finished: {self.stats['files']} files, {self.stats['errors']} errors", self.stats["status"].upper())
        except OSError:
            pass
        self.event({"kind": "done", "stats": self.stats, "root": str(self.root)})
        return self.stats
