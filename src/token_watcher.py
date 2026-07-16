from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path


APP_TITLE = "TokenWatcher"
INSTANCE_MUTEX_NAME = "Local\\TokenWatcher.Singleton"
START_DATE = date(2026, 2, 1)
REFRESH_SECONDS = 0.5
STARTUP_HOT_SECONDS = 300.0
SHANGHAI = timezone(timedelta(hours=8))
PERIODS = ("today", "week", "month", "cumulative")
PERIOD_LABELS = {
    "today": "本日",
    "week": "本周",
    "month": "本月",
    "cumulative": "累计",
}
PLATFORM_COLORS = {
    "Codex": "#4C8DFF",
    "Claude Code": "#F59E42",
    "Cline": "#26C6A2",
}
REPORT_FOLDER_NAME = "codex_claude_usage_since_2026-02"
CLINE_HISTORY = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "Code"
    / "User"
    / "globalStorage"
    / "saoudrizwan.claude-dev"
    / "state"
    / "taskHistory.json"
)
CLINE_TASKS = CLINE_HISTORY.parent.parent / "tasks"
CODEX_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
CODEX_CACHE_VERSION = 2
CLAUDE_CACHE_VERSION = 1
USAGE_SNAPSHOT_CACHE_VERSION = 2
WINDOWS_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
CASCADIA_MONO_FONT = str(WINDOWS_FONTS / "CascadiaMono.ttf")
YAHEI_FONT = str(WINDOWS_FONTS / "msyh.ttc")
YAHEI_BOLD_FONT = str(WINDOWS_FONTS / "msyhbd.ttc")
WINDOW_WIDTH = 860
WINDOW_HEIGHT = 260
ROW_HEIGHT = 56
ROW_MIDDLE = ROW_HEIGHT // 2
BODY_FONT_SIZE = 26
TITLE_FONT_SIZE = 28


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def _relative_luminance(pixel: tuple[int, int, int]) -> float:
    channels = []
    for value in pixel:
        normalized = value / 255.0
        channels.append(
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def choose_text_foreground(
    pixels: list[tuple[int, int, int]],
    previous_foreground: str = "#FFFFFF",
) -> str:
    if not pixels:
        return previous_foreground
    luminances = [_relative_luminance(pixel) for pixel in pixels]
    black_contrasts = [(value + 0.05) / 0.05 for value in luminances]
    white_contrasts = [1.05 / (value + 0.05) for value in luminances]
    black_score = _percentile(black_contrasts, 0.2)
    white_score = _percentile(white_contrasts, 0.2)
    if abs(black_score - white_score) < 0.15:
        return previous_foreground
    return "#000000" if black_score > white_score else "#FFFFFF"


def contrast_color(pixel: tuple[int, int, int]) -> tuple[int, int, int, int]:
    red, green, blue = pixel
    return 255 - red, 255 - green, 255 - blue, 255


def pixel_contrast_colors(background):
    from PIL import ImageOps

    return ImageOps.invert(background.convert("RGB")).convert("RGBA")


def render_pixel_contrast_text(
    background,
    text: str,
    font_name: str,
    font_size: int,
    position: tuple[int, int],
    anchor: str,
    solid_color: str | None = None,
):
    from PIL import Image, ImageColor, ImageDraw, ImageFont

    width, height = background.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    font = _load_image_font(font_name, font_size)
    draw.text(position, text, font=font, fill=255, anchor=anchor)
    if solid_color is not None:
        red, green, blue = ImageColor.getrgb(solid_color)
        color = Image.new("RGBA", (width, height), (red, green, blue, 255))
    else:
        # Pillow applies the sRGB linearization and luminance operations in
        # native code, preserving WCAG contrast behavior on saturated colors.
        color = pixel_contrast_colors(background)
    color.putalpha(mask)
    return color


@lru_cache(maxsize=32)
def _load_image_font(font_name: str, font_size: int):
    from PIL import ImageFont

    return ImageFont.truetype(font_name, font_size)


class AdaptiveCanvasText:
    def __init__(
        self,
        canvas: tk.Canvas,
        *,
        text: str,
        font_name: str,
        font_size: int,
        position: tuple[int, int],
        anchor: str,
    ):
        self.canvas = canvas
        self.text = text
        self.font_name = font_name
        self.font_size = font_size
        self.position = position
        self.anchor = anchor
        self.solid_color: str | None = None
        self.photo = None
        self.last_image = None
        self.image_id = canvas.create_image(0, 0, anchor="nw")
        self.last_background = None
        self.last_root = None

    def set_text(self, text: str) -> None:
        self.text = text
        self.render_cached()

    def hide(self) -> None:
        self.canvas.itemconfigure(self.image_id, state="hidden")

    def prepare(self, background, root: tk.Tk):
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        x = self.canvas.winfo_rootx() - root.winfo_rootx()
        y = self.canvas.winfo_rooty() - root.winfo_rooty()
        crop = background.crop((x, y, x + width, y + height))
        return render_pixel_contrast_text(
            crop,
            self.text,
            self.font_name,
            self.font_size,
            self.position,
            self.anchor,
            self.solid_color,
        )

    def apply_prepared(self, image, background, root: tk.Tk) -> None:
        from PIL import ImageTk

        new_photo = ImageTk.PhotoImage(image)
        previous_photo = self.photo
        self.last_image = image
        self.canvas.itemconfigure(
            self.image_id,
            image=new_photo,
            state="normal",
        )
        self.photo = new_photo
        self.last_background = background
        self.last_root = root
        del previous_photo

    def prepare_photo(self, image):
        from PIL import ImageTk

        return ImageTk.PhotoImage(image)

    def apply_buffered(self, image, photo, background, root: tk.Tk) -> None:
        previous_photo = self.photo
        self.last_image = image
        self.canvas.itemconfigure(self.image_id, image=photo, state="normal")
        self.photo = photo
        self.last_background = background
        self.last_root = root
        del previous_photo

    def render(self, background, root: tk.Tk) -> None:
        self.apply_prepared(self.prepare(background, root), background, root)

    def render_cached(self) -> None:
        if self.last_background is not None and self.last_root is not None:
            self.render(self.last_background, self.last_root)


def codex_usage_fingerprint(
    session_id: str,
    event_timestamp: object,
    info: dict,
) -> tuple:
    last_usage = info.get("last_token_usage") or {}
    total_usage = info.get("total_token_usage") or {}
    last_values = tuple(int(last_usage.get(key) or 0) for key in CODEX_USAGE_KEYS)
    total_values = tuple(int(total_usage.get(key) or 0) for key in CODEX_USAGE_KEYS)
    if any(total_values):
        return (session_id or "<unknown>", "cumulative", *total_values, *last_values)
    return (
        session_id or "<unknown>",
        "timestamp",
        str(event_timestamp or ""),
        *last_values,
    )


def codex_fingerprint_digest(fingerprint: tuple) -> str:
    payload = json.dumps(
        fingerprint,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


class DirectoryChangeWatcher:
    """Collect recursive Windows directory changes without rescanning the tree."""

    def __init__(self, root: Path):
        self.root = root
        self.available = False
        self._changes: queue.SimpleQueue[Path] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._kernel32 = None
        self._handle = None
        self._thread: threading.Thread | None = None
        if os.name == "nt" and root.exists():
            self._start_windows()

    def _start_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        file_list_directory = 0x0001
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        backup_semantics = 0x02000000
        invalid_handle = ctypes.c_void_p(-1).value
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(self.root),
            file_list_directory,
            share_all,
            None,
            open_existing,
            backup_semantics,
            None,
        )
        if handle == invalid_handle:
            return
        self._kernel32 = kernel32
        self._handle = handle
        self.available = True
        self._thread = threading.Thread(
            target=self._run_windows,
            daemon=True,
            name=f"watch-{self.root.name}",
        )
        self._thread.start()

    def _run_windows(self) -> None:
        import ctypes
        import struct
        from ctypes import wintypes

        notify_filter = (
            0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
            | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
            | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
            | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
            | 0x00000040  # FILE_NOTIFY_CHANGE_CREATION
        )
        kernel32 = self._kernel32
        if kernel32 is None:
            self.available = False
            return
        kernel32.ReadDirectoryChangesW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(64 * 1024)
        bytes_returned = wintypes.DWORD()
        while not self._closed.is_set() and self._handle is not None:
            ok = kernel32.ReadDirectoryChangesW(
                self._handle,
                buffer,
                len(buffer),
                True,
                notify_filter,
                ctypes.byref(bytes_returned),
                None,
                None,
            )
            if not ok:
                break
            length = int(bytes_returned.value)
            if not length:
                continue
            offset = 0
            while offset < length:
                next_offset, _action, name_bytes = struct.unpack_from(
                    "<III", buffer.raw, offset
                )
                name_start = offset + 12
                relative_name = buffer.raw[
                    name_start : name_start + name_bytes
                ].decode("utf-16-le", errors="replace")
                self._changes.put(self.root / relative_name)
                if not next_offset:
                    break
                offset += next_offset
        self.available = False

    def drain(self) -> set[Path]:
        changes: set[Path] = set()
        while True:
            try:
                changes.add(self._changes.get_nowait())
            except queue.Empty:
                return changes

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        handle = self._handle
        self._handle = None
        self.available = False
        if handle is not None and os.name == "nt":
            from ctypes import wintypes

            kernel32 = self._kernel32
            if kernel32 is not None:
                kernel32.CancelIoEx.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
                kernel32.CancelIoEx.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CancelIoEx(handle, None)
                kernel32.CloseHandle(handle)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def acquire_single_instance_mutex():
    if os.name != "nt":
        return True
    import ctypes

    handle = ctypes.windll.kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        return None
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance_mutex(handle) -> None:
    if os.name == "nt" and handle not in (None, True):
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def format_tokens(value: int) -> str:
    return f"{int(value):,}"


def compact_model_name(model: str) -> str:
    name = model.strip()
    lower = name.lower()
    if lower.startswith("gpt-"):
        name = lower.removeprefix("gpt-").replace("codex", "cdx")
    elif lower.startswith("claude-"):
        name = lower.removeprefix("claude-")
        name = name.replace("sonnet", "son").replace("deepseek", "ds")
        if name.endswith(tuple(f"-{year}" for year in range(2020, 2031))):
            name = name.rsplit("-", 1)[0]
        parts = name.split("-")
        if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
            name = "-".join(parts[:-2]) + f"-{parts[-2]}.{parts[-1]}"
    elif lower.startswith("deepseek-"):
        name = "ds-" + lower.removeprefix("deepseek-")
    elif lower.startswith("glm-"):
        name = name.upper()
    if len(name) > 9:
        return f"{name[:8]}…"
    return name


def empty_periods() -> dict[str, Counter]:
    return {period: Counter() for period in PERIODS}


def active_periods(event_date: date, now_date: date | None = None) -> tuple[str, ...]:
    now_date = now_date or datetime.now(SHANGHAI).date()
    periods = ["cumulative"]
    if event_date.year == now_date.year and event_date.month == now_date.month:
        periods.append("month")
    week_start = now_date - timedelta(days=now_date.weekday())
    if week_start <= event_date <= now_date:
        periods.append("week")
    if event_date == now_date:
        periods.append("today")
    return tuple(periods)


def add_period_usage(
    periods: dict[str, Counter],
    key: tuple[str, str],
    tokens: int,
    event_date: date,
) -> None:
    for period in active_periods(event_date):
        periods[period][key] += int(tokens)


def find_report_dir() -> Path:
    configured = os.environ.get("AI_USAGE_REPORT_DIR")
    candidates = [Path(configured).expanduser()] if configured else []

    def add_ancestor_candidates(root: Path) -> None:
        for directory in (root, *tuple(root.parents)[:3]):
            candidate = directory / "outputs" / REPORT_FOLDER_NAME
            if candidate not in candidates:
                candidates.append(candidate)

    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        add_ancestor_candidates(executable_root)
    else:
        root = Path(__file__).resolve().parents[1]
        add_ancestor_candidates(root)
    add_ancestor_candidates(Path.cwd().resolve())
    candidates.append(Path.home() / ".tokenwatcher" / REPORT_FOLDER_NAME)
    for candidate in candidates:
        if (candidate / "model_total.csv").exists():
            return candidate
    return candidates[0]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass
class Baseline:
    report_dir: Path
    refreshed_at: datetime
    periods: dict[str, Counter] = field(default_factory=empty_periods)
    call_periods: dict[str, Counter] = field(default_factory=empty_periods)
    report_mtime: float = 0.0


def load_baseline(report_dir: Path) -> Baseline:
    summary_path = report_dir / "summary.json"
    if not summary_path.exists():
        return Baseline(
            report_dir=report_dir,
            refreshed_at=datetime.combine(
                START_DATE,
                datetime.min.time(),
                tzinfo=SHANGHAI,
            ),
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    baseline = Baseline(
        report_dir=report_dir,
        refreshed_at=parse_time(summary.get("refreshed_at_shanghai")),
        report_mtime=summary_path.stat().st_mtime,
    )
    model_total_path = report_dir / "model_total.csv"
    if model_total_path.exists():
        for row in read_csv(model_total_path):
            baseline.periods["cumulative"][(row["platform"], row["model"])] += int(
                row["total_tokens"]
            )
    now_date = datetime.now(SHANGHAI).date()
    daily_path = report_dir / "daily_by_platform_model.csv"
    if daily_path.exists():
        for row in read_csv(daily_path):
            row_date = date.fromisoformat(row["date"])
            key = (row["platform"], row["model"])
            tokens = int(row["total_tokens"])
            calls = int(row.get("responses") or 0)
            for period in active_periods(row_date, now_date):
                baseline.call_periods[period][key] += calls
                if period != "cumulative":
                    baseline.periods[period][key] += tokens
    return baseline


@dataclass
class CodexFileState:
    offset: int = 0
    remainder: bytes = b""
    model: str = "<unknown>"
    session_id: str = ""
    last_mtime: float = 0.0
    last_size: int = 0
    next_check: float = 0.0
    watching: bool = True
    stop_after_initial: bool = False


class CodexTailTracker:
    def __init__(
        self,
        since: datetime,
        watcher: DirectoryChangeWatcher | None = None,
        cache_path: Path | None = None,
    ):
        self.since = since.astimezone(timezone.utc)
        self.root = Path.home() / ".codex"
        self.cache_path = cache_path or (
            Path.home() / ".tokenwatcher" / "codex_fingerprint_cache.json"
        )
        self._owns_watcher = watcher is None
        self.watcher = watcher or DirectoryChangeWatcher(self.root)
        self.states: dict[Path, CodexFileState] = {}
        self.periods = empty_periods()
        self.call_periods = empty_periods()
        self.seen: set[str] = set()
        self.cached_files: dict[str, dict] = {}
        self.cache_dirty = False
        self.last_event: datetime | None = None
        self.errors = 0
        self.cache_errors = 0
        self._load_cache()
        self._discover_startup()

    @staticmethod
    def _cache_key(path: Path) -> str:
        return path.name

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if int(payload.get("version") or 0) != CODEX_CACHE_VERSION:
                return
            fingerprints = payload.get("fingerprints") or []
            files = payload.get("files") or {}
            if not isinstance(fingerprints, list) or not isinstance(files, dict):
                return
            self.seen = {
                value
                for value in fingerprints
                if isinstance(value, str) and len(value) == 32
            }
            self.cached_files = {
                str(key): value
                for key, value in files.items()
                if isinstance(value, dict)
            }
            if str(payload.get("since") or "") == self.since.isoformat():
                self.periods = ClaudeTailTracker._decode_periods(
                    payload.get("periods")
                )
                self.call_periods = ClaudeTailTracker._decode_periods(
                    payload.get("call_periods")
                )
                last_event = payload.get("last_event")
                self.last_event = (
                    parse_time(last_event).astimezone(timezone.utc)
                    if last_event
                    else None
                )
            else:
                self.cache_dirty = True
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self.cache_errors += 1

    def _save_cache(self) -> None:
        if not self.cache_dirty:
            return
        payload = {
            "version": CODEX_CACHE_VERSION,
            "updated_at": datetime.now(SHANGHAI).isoformat(),
            "since": self.since.isoformat(),
            "fingerprints": sorted(self.seen),
            "files": self.cached_files,
            "periods": ClaudeTailTracker._encode_periods(self.periods),
            "call_periods": ClaudeTailTracker._encode_periods(self.call_periods),
            "last_event": self.last_event.isoformat() if self.last_event else None,
        }
        temporary_path = self.cache_path.with_suffix(".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.cache_path)
            self.cache_dirty = False
        except OSError:
            self.cache_errors += 1
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _state_from_cache(
        self,
        path: Path,
        stat: os.stat_result,
        startup_cold: bool,
    ) -> tuple[CodexFileState, bool]:
        record = self.cached_files.get(self._cache_key(path)) or {}
        cached_size = int(record.get("size") or 0)
        cached_mtime_ns = int(record.get("mtime_ns") or 0)
        cached_offset = int(record.get("offset") or 0)
        exact = (
            cached_size == stat.st_size
            and cached_mtime_ns == stat.st_mtime_ns
            and cached_offset == stat.st_size
            and bool(record)
        )
        offset = cached_offset
        if offset < 0 or offset > stat.st_size:
            offset = 0
        if not exact and stat.st_size == cached_size:
            offset = 0
        remainder = b""
        encoded_remainder = record.get("remainder")
        if offset and isinstance(encoded_remainder, str):
            try:
                remainder = base64.b64decode(encoded_remainder.encode("ascii"))
            except (ValueError, UnicodeEncodeError):
                remainder = b""
        state = CodexFileState(
            offset=offset,
            remainder=remainder,
            model=str(record.get("model") or "<unknown>"),
            session_id=str(record.get("session_id") or ""),
            last_mtime=stat.st_mtime,
            last_size=stat.st_size if exact else offset,
            watching=not startup_cold,
            stop_after_initial=startup_cold,
        )
        return state, exact

    def _remember_file(
        self,
        path: Path,
        state: CodexFileState,
        stat: os.stat_result,
    ) -> None:
        record = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "offset": state.offset,
            "session_id": state.session_id,
            "model": state.model,
            "remainder": base64.b64encode(state.remainder).decode("ascii")
            if state.remainder
            else "",
        }
        key = self._cache_key(path)
        if self.cached_files.get(key) != record:
            self.cached_files[key] = record
            self.cache_dirty = True

    def _paths(self) -> list[Path]:
        paths = []
        for folder_name in ("sessions", "archived_sessions"):
            folder = self.root / folder_name
            if not folder.exists():
                continue
            for path in folder.rglob("*.jsonl"):
                paths.append(path)
        return sorted(paths, key=lambda path: path.name)

    def _discover_startup(self) -> None:
        for path in self._paths():
            try:
                stat = path.stat()
            except OSError:
                self.errors += 1
                continue
            startup_cold = stat.st_mtime < time.time() - STARTUP_HOT_SECONDS
            state, exact_cache_hit = self._state_from_cache(
                path,
                stat,
                startup_cold,
            )
            self.states[path] = state
            if exact_cache_hit:
                state.next_check = time.monotonic() + REFRESH_SECONDS
                continue
            self._read_path(path, state, initial=True)
        self._save_cache()

    def _discover_changes(self) -> None:
        for path in self.watcher.drain():
            if path.suffix.lower() != ".jsonl":
                continue
            state = self.states.get(path)
            if state is None:
                try:
                    stat = path.stat()
                    state, _exact_cache_hit = self._state_from_cache(
                        path,
                        stat,
                        startup_cold=False,
                    )
                except OSError:
                    state = CodexFileState()
                self.states[path] = state
            state.watching = True
            state.stop_after_initial = False
            state.next_check = 0.0

    def _consume(self, data: bytes, state: CodexFileState, initial: bool) -> None:
        data = state.remainder + data
        lines = data.split(b"\n")
        state.remainder = lines.pop() if data and not data.endswith(b"\n") else b""
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (TypeError, json.JSONDecodeError):
                self.errors += 1
                continue
            payload = event.get("payload") or {}
            if event.get("type") == "session_meta":
                state.session_id = str(payload.get("id") or state.session_id)
                continue
            if event.get("type") == "turn_context":
                state.model = str(payload.get("model") or state.model)
                continue
            if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            total_tokens = int(usage.get("total_tokens") or 0)
            if not total_tokens:
                total_tokens = int(usage.get("input_tokens") or 0) + int(
                    usage.get("output_tokens") or 0
                )
            if total_tokens <= 0:
                continue
            model = state.model or "<unknown>"
            fingerprint = codex_fingerprint_digest(
                codex_usage_fingerprint(
                    state.session_id,
                    event.get("timestamp"),
                    info,
                )
            )
            if fingerprint in self.seen:
                continue
            self.seen.add(fingerprint)
            self.cache_dirty = True
            try:
                event_time = parse_time(event.get("timestamp")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                self.errors += 1
                continue
            if event_time <= self.since:
                continue
            key = ("Codex", model)
            add_period_usage(
                self.periods,
                key,
                total_tokens,
                event_time.astimezone(SHANGHAI).date(),
            )
            add_period_usage(
                self.call_periods,
                key,
                1,
                event_time.astimezone(SHANGHAI).date(),
            )
            self.last_event = max(self.last_event, event_time) if self.last_event else event_time

    def _read_path(
        self,
        path: Path,
        state: CodexFileState,
        initial: bool = False,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        if not initial and not state.watching:
            return
        if not initial and now < state.next_check:
            return
        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
            if size < state.offset:
                state.offset = 0
                state.remainder = b""
                state.model = "<unknown>"
                state.session_id = ""
            state.last_size = size
            state.last_mtime = mtime
            if size == state.offset:
                if initial and state.stop_after_initial:
                    state.watching = False
                state.next_check = now + REFRESH_SECONDS
                self._remember_file(path, state, stat)
                return
            with path.open("rb") as handle:
                handle.seek(state.offset)
                data = handle.read()
            state.offset = size
            state.next_check = now + REFRESH_SECONDS
            self._consume(data, state, initial)
            self._remember_file(path, state, stat)
            if initial and state.stop_after_initial:
                state.watching = False
        except FileNotFoundError:
            if state.offset:
                state.watching = False
            else:
                state.next_check = now + REFRESH_SECONDS
        except OSError:
            self.errors += 1

    def poll(self) -> None:
        self._discover_changes()
        now = time.monotonic()
        for path, state in list(self.states.items()):
            self._read_path(path, state, now=now)

    def close(self) -> None:
        self._save_cache()
        if self._owns_watcher:
            self.watcher.close()


def _parse_claude_usage(stats_path: Path) -> tuple[dict[str, Counter], str, datetime]:
    periods = empty_periods()
    if not stats_path.exists():
        boundary = datetime.now(SHANGHAI).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return periods, "Claude stats-cache 不存在", boundary
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    for model, usage in stats.get("modelUsage", {}).items():
        periods["cumulative"][("Claude Code", model)] = int(usage.get("inputTokens") or 0) + int(
            usage.get("outputTokens") or 0
        )
    for row in stats.get("dailyModelTokens", []):
        try:
            row_date = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        for model, tokens in (row.get("tokensByModel") or {}).items():
            key = ("Claude Code", model)
            for period in active_periods(row_date):
                if period != "cumulative":
                    periods[period][key] += int(tokens or 0)
    last_date_text = stats.get("lastComputedDate")
    if last_date_text:
        boundary = datetime.combine(
            date.fromisoformat(last_date_text) + timedelta(days=1),
            datetime.min.time(),
            tzinfo=SHANGHAI,
        )
    else:
        boundary = datetime.now(SHANGHAI).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return (
        periods,
        f"Claude /status：{last_date_text or '未知日期'}",
        boundary,
    )


class ClaudeUsageCache:
    def __init__(self):
        self.stats_path = Path.home() / ".claude" / "stats-cache.json"
        self.last_signature: tuple[float, int] | None = None
        self.cached: tuple[dict[str, Counter], str, datetime] | None = None

    def read(self) -> tuple[dict[str, Counter], str, datetime]:
        try:
            stat = self.stats_path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            signature = (0.0, 0)
        if self.cached is None or signature != self.last_signature:
            self.cached = _parse_claude_usage(self.stats_path)
            self.last_signature = signature
        return self.cached


@dataclass
class ClaudeFileState:
    offset: int = 0
    remainder: bytes = b""
    last_mtime: float = 0.0
    last_size: int = 0
    next_check: float = 0.0
    watching: bool = True
    stop_after_initial: bool = False


class ClaudeTailTracker:
    def __init__(
        self,
        since: datetime,
        track_tokens: bool = True,
        watcher: DirectoryChangeWatcher | None = None,
        call_since: datetime | None = None,
        cache_path: Path | None = None,
    ):
        self.since = since.astimezone(timezone.utc)
        self.call_since = (call_since or since).astimezone(timezone.utc)
        self.scan_since = min(self.since, self.call_since)
        self.track_tokens = track_tokens
        self.root = Path.home() / ".claude" / "projects"
        self.cache_path = cache_path or (
            Path.home() / ".tokenwatcher" / "claude_tail_cache.json"
        )
        self._owns_watcher = watcher is None
        self.watcher = watcher or DirectoryChangeWatcher(self.root)
        self.states: dict[Path, ClaudeFileState] = {}
        self.periods = empty_periods()
        self.call_periods = empty_periods()
        self.seen: set[tuple] = set()
        self.cached_files: dict[str, dict] = {}
        self.cache_dirty = False
        self.last_event: datetime | None = None
        self.errors = 0
        self.cache_errors = 0
        self._load_cache()
        self._discover_startup()
        self._save_cache()

    @staticmethod
    def _encode_periods(periods: dict[str, Counter]) -> dict[str, list[dict]]:
        return {
            period: [
                {"platform": key[0], "model": key[1], "value": int(value)}
                for key, value in sorted(periods[period].items())
            ]
            for period in PERIODS
        }

    @staticmethod
    def _decode_periods(payload: object) -> dict[str, Counter]:
        periods = empty_periods()
        if not isinstance(payload, dict):
            return periods
        for period in PERIODS:
            for row in payload.get(period) or []:
                if not isinstance(row, dict):
                    continue
                key = (str(row.get("platform") or "<unknown>"), str(row.get("model") or "<unknown>"))
                periods[period][key] = int(row.get("value") or 0)
        return periods

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if int(payload.get("version") or 0) != CLAUDE_CACHE_VERSION:
                return
            if str(payload.get("token_since") or "") != self.since.isoformat():
                return
            if str(payload.get("call_since") or "") != self.call_since.isoformat():
                return
            if bool(payload.get("track_tokens", True)) != self.track_tokens:
                return
            self.periods = self._decode_periods(payload.get("periods"))
            self.call_periods = self._decode_periods(payload.get("call_periods"))
            self.seen = {
                tuple(str(value) for value in row)
                for row in payload.get("seen") or []
                if isinstance(row, list) and len(row) == 3
            }
            self.cached_files = {
                str(path): value
                for path, value in (payload.get("files") or {}).items()
                if isinstance(value, dict)
            }
            last_event = payload.get("last_event")
            self.last_event = parse_time(last_event).astimezone(timezone.utc) if last_event else None
        except (OSError, ValueError, TypeError, KeyError):
            self.cache_errors += 1

    def _save_cache(self) -> None:
        if not self.cache_dirty:
            return
        payload = {
            "version": CLAUDE_CACHE_VERSION,
            "token_since": self.since.isoformat(),
            "call_since": self.call_since.isoformat(),
            "track_tokens": self.track_tokens,
            "periods": self._encode_periods(self.periods),
            "call_periods": self._encode_periods(self.call_periods),
            "seen": [list(row) for row in sorted(self.seen)],
            "last_event": self.last_event.isoformat() if self.last_event else None,
            "files": self.cached_files,
        }
        temporary_path = self.cache_path.with_name(
            f"{self.cache_path.name}.{os.getpid()}.tmp"
        )
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.cache_path)
            self.cache_dirty = False
        except OSError:
            self.cache_errors += 1
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _state_from_cache(self, cached: dict, startup_cold: bool) -> ClaudeFileState:
        try:
            remainder = base64.b64decode(str(cached.get("remainder") or ""))
        except (ValueError, TypeError):
            remainder = b""
        return ClaudeFileState(
            offset=int(cached.get("offset") or 0),
            remainder=remainder,
            last_mtime=float(cached.get("mtime") or 0.0),
            last_size=int(cached.get("size") or 0),
            watching=not startup_cold,
            stop_after_initial=startup_cold,
        )

    def _remember_file(self, path: Path, state: ClaudeFileState, stat) -> None:
        entry = {
            "offset": state.offset,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtime_ns": stat.st_mtime_ns,
            "remainder": base64.b64encode(state.remainder).decode("ascii"),
        }
        key = str(path)
        if self.cached_files.get(key) != entry:
            self.cached_files[key] = entry
            self.cache_dirty = True

    def _discover_startup(self) -> None:
        if not self.root.exists():
            return
        threshold = self.scan_since.timestamp() - 5
        for path in self.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
                if stat.st_mtime < threshold or path in self.states:
                    continue
            except OSError:
                continue
            startup_cold = stat.st_mtime < time.time() - STARTUP_HOT_SECONDS
            cached = self.cached_files.get(str(path))
            state = (
                self._state_from_cache(cached, startup_cold)
                if cached is not None
                else ClaudeFileState(stop_after_initial=startup_cold)
            )
            self.states[path] = state
            if cached is not None:
                exact = (
                    int(cached.get("size") or -1) == stat.st_size
                    and int(cached.get("mtime_ns") or -1) == stat.st_mtime_ns
                    and state.offset == stat.st_size
                )
                if exact:
                    state.last_size = stat.st_size
                    state.last_mtime = stat.st_mtime
                    continue
                if stat.st_size <= state.offset:
                    state.offset = 0
                    state.remainder = b""
            self._read_path(path, state, initial=True)

    def _discover_changes(self) -> None:
        for path in self.watcher.drain():
            if path.suffix.lower() != ".jsonl":
                continue
            state = self.states.get(path)
            if state is None:
                state = ClaudeFileState()
                self.states[path] = state
            state.watching = True
            state.stop_after_initial = False
            state.next_check = 0.0

    def _consume(self, data: bytes, state: ClaudeFileState) -> None:
        data = state.remainder + data
        lines = data.split(b"\n")
        state.remainder = lines.pop() if data and not data.endswith(b"\n") else b""
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (TypeError, json.JSONDecodeError):
                self.errors += 1
                continue
            message = event.get("message") or {}
            usage = message.get("usage")
            if event.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            model = str(message.get("model") or "<unknown>")
            if model == "<synthetic>":
                continue
            try:
                event_time = parse_time(event.get("timestamp")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                self.errors += 1
                continue
            if event_time < self.scan_since:
                continue
            total_tokens = int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )
            fingerprint = (
                str(event.get("sessionId") or "<unknown>"),
                str(message.get("id") or event.get("uuid") or "<unknown>"),
                model,
            )
            if fingerprint in self.seen:
                continue
            self.seen.add(fingerprint)
            key = ("Claude Code", model)
            event_date = event_time.astimezone(SHANGHAI).date()
            if self.track_tokens and event_time >= self.since:
                add_period_usage(self.periods, key, total_tokens, event_date)
            if event_time >= self.call_since:
                add_period_usage(self.call_periods, key, 1, event_date)
            self.last_event = max(self.last_event, event_time) if self.last_event else event_time
            self.cache_dirty = True

    def _read_path(
        self,
        path: Path,
        state: ClaudeFileState,
        initial: bool = False,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        if not initial and not state.watching:
            return
        if now < state.next_check:
            return
        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
            if size < state.offset:
                state.offset = 0
                state.remainder = b""
            state.last_size = size
            state.last_mtime = mtime
            if size == state.offset:
                if initial and state.stop_after_initial:
                    state.watching = False
                state.next_check = now + REFRESH_SECONDS
                self._remember_file(path, state, stat)
                return
            with path.open("rb") as handle:
                handle.seek(state.offset)
                data = handle.read()
            state.offset = size
            state.next_check = now + REFRESH_SECONDS
            self._consume(data, state)
            self._remember_file(path, state, stat)
            if initial and state.stop_after_initial:
                state.watching = False
        except FileNotFoundError:
            if state.offset:
                state.watching = False
            else:
                state.next_check = now + REFRESH_SECONDS
        except OSError:
            self.errors += 1

    def poll(self) -> None:
        self._discover_changes()
        now = time.monotonic()
        for path, state in list(self.states.items()):
            self._read_path(path, state, now=now)

    def close(self) -> None:
        self._save_cache()
        if self._owns_watcher:
            self.watcher.close()


def load_cline_tasks() -> dict[str, tuple[str, int, datetime]]:
    if not CLINE_HISTORY.exists():
        return {}
    data = json.loads(CLINE_HISTORY.read_text(encoding="utf-8"))
    history = data if isinstance(data, list) else data.get("taskHistory") or []
    tasks = {}
    for task in history:
        task_id = str(task.get("id") or task.get("ulid") or "<unknown>")
        model = str(task.get("modelId") or "<unknown>")
        total = int(task.get("tokensIn") or 0) + int(task.get("tokensOut") or 0)
        timestamp = datetime.fromtimestamp(
            int(task.get("ts") or 0) / 1000, timezone.utc
        ).astimezone(SHANGHAI)
        tasks[task_id] = (model, total, timestamp)
    return tasks


class ClineTaskCache:
    def __init__(self):
        self.last_signature: tuple[float, int] | None = None
        self.cached: dict[str, tuple[str, int, datetime]] = {}

    def read(self) -> dict[str, tuple[str, int, datetime]]:
        try:
            stat = CLINE_HISTORY.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            signature = (0.0, 0)
        if signature != self.last_signature:
            self.cached = load_cline_tasks()
            self.last_signature = signature
        return dict(self.cached)


class ClineRequestCounter:
    def __init__(
        self,
        since: datetime,
        watcher: DirectoryChangeWatcher | None = None,
    ):
        self.since = since.astimezone(SHANGHAI)
        self._owns_watcher = watcher is None
        self.watcher = watcher or DirectoryChangeWatcher(CLINE_HISTORY.parent.parent)
        self.states: dict[Path, tuple[tuple[float, int], dict[str, Counter]]] = {}
        self.known_paths: set[Path] = set()
        self.initialized = False
        self.periods = empty_periods()

    def _parse_path(self, path: Path, fallback_model: str) -> dict[str, Counter]:
        periods = empty_periods()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return periods
        messages = data if isinstance(data, list) else data.get("messages") or data.get("uiMessages") or []
        for event in messages:
            if event.get("type") != "say" or event.get("say") != "api_req_started":
                continue
            try:
                payload = json.loads(event.get("text") or "{}")
                if not any(metric in payload for metric in ("tokensIn", "tokensOut", "cacheReads", "cacheWrites")):
                    continue
                event_date = datetime.fromtimestamp(
                    int(event["ts"]) / 1000, timezone.utc
                ).astimezone(SHANGHAI)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            if event_date <= self.since:
                continue
            model = str((event.get("modelInfo") or {}).get("modelId") or fallback_model)
            add_period_usage(periods, ("Cline", model), 1, event_date.date())
        return periods

    def _refresh_path(self, path: Path, fallback_model: str) -> None:
        try:
            stat = path.stat()
        except OSError:
            self.states.pop(path, None)
            return
        if stat.st_mtime <= self.since.timestamp():
            return
        signature = (stat.st_mtime, stat.st_size)
        previous = self.states.get(path)
        if previous is None or previous[0] != signature:
            self.states[path] = (signature, self._parse_path(path, fallback_model))

    def poll(self, tasks: dict[str, tuple[str, int, datetime]]) -> None:
        models_by_path: dict[Path, str] = {}
        timestamps_by_path: dict[Path, datetime] = {}
        for task_id, (model, _total, timestamp) in tasks.items():
            path = CLINE_TASKS / task_id / "ui_messages.json"
            models_by_path[path] = model
            timestamps_by_path[path] = timestamp
        active_paths = set(models_by_path)
        if not self.initialized:
            candidates = set(active_paths)
            self.initialized = True
        else:
            candidates = {
                path
                for path in self.watcher.drain()
                if path.name.lower() == "ui_messages.json" and path in active_paths
            }
            candidates.update(active_paths - self.known_paths)
            if not self.watcher.available:
                hot_boundary = datetime.now(SHANGHAI) - timedelta(
                    seconds=STARTUP_HOT_SECONDS
                )
                candidates.update(
                    path
                    for path, timestamp in timestamps_by_path.items()
                    if timestamp >= hot_boundary
                )
        for path in candidates:
            self._refresh_path(path, models_by_path[path])
        for path in set(self.states) - active_paths:
            del self.states[path]
        self.known_paths = active_paths
        combined = empty_periods()
        for _signature, path_periods in self.states.values():
            for period in PERIODS:
                combined[period].update(path_periods[period])
        self.periods = combined

    def close(self) -> None:
        if self._owns_watcher:
            self.watcher.close()


class ClinePoller:
    def __init__(self, baseline: Baseline):
        self.baseline_periods = {
            period: Counter(
                {
                    key: value
                    for key, value in baseline.periods[period].items()
                    if key[0] == "Cline"
                }
            )
            for period in PERIODS
        }
        self.baseline_call_periods = {
            period: Counter(
                {
                    key: value
                    for key, value in baseline.call_periods[period].items()
                    if key[0] == "Cline"
                }
            )
            for period in PERIODS
        }
        self.report_time = baseline.refreshed_at.astimezone(SHANGHAI)
        self.task_cache = ClineTaskCache()
        self.initial_tasks = self.task_cache.read()
        initial_by_model = Counter()
        for model, total, _ in self.initial_tasks.values():
            initial_by_model[("Cline", model)] += total
        self.offsets = Counter()
        for key in set(initial_by_model) | set(self.baseline_periods["cumulative"]):
            self.offsets[key] = max(
                self.baseline_periods["cumulative"][key], initial_by_model[key]
            ) - initial_by_model[key]
        self.periods = {
            period: Counter(values)
            for period, values in self.baseline_periods.items()
        }
        self.request_counter = ClineRequestCounter(self.report_time)
        self.call_periods = {
            period: Counter(values)
            for period, values in self.baseline_call_periods.items()
        }
        self.status = "Cline taskHistory 已载入"
        self.poll()

    def poll(self) -> None:
        current_tasks = self.task_cache.read()
        self.request_counter.poll(current_tasks)
        current_by_model = Counter()
        delta_periods = empty_periods()
        for task_id, (model, total, timestamp) in current_tasks.items():
            key = ("Cline", model)
            current_by_model[key] += total
            initial_total = self.initial_tasks.get(task_id, (model, 0, timestamp))[1]
            delta = max(0, total - initial_total)
            if delta:
                add_period_usage(delta_periods, key, delta, timestamp.date())
            if task_id not in self.initial_tasks and timestamp > self.report_time:
                add_period_usage(
                    delta_periods,
                    key,
                    max(0, total - delta),
                    timestamp.date(),
                )
        cumulative = Counter()
        for key in set(current_by_model) | set(self.baseline_periods["cumulative"]):
            cumulative[key] = current_by_model[key] + self.offsets[key]
        self.periods = {
            "cumulative": cumulative,
            "today": self.baseline_periods["today"] + delta_periods["today"],
            "week": self.baseline_periods["week"] + delta_periods["week"],
            "month": self.baseline_periods["month"] + delta_periods["month"],
        }
        self.call_periods = {
            period: self.baseline_call_periods[period]
            + self.request_counter.periods[period]
            for period in PERIODS
        }
        self.status = f"Cline 任务：{len(current_tasks)}"


    def close(self) -> None:
        self.request_counter.close()


@dataclass(frozen=True)
class UsageSnapshot:
    periods: dict[str, dict[tuple[str, str], int]]
    call_periods: dict[str, dict[tuple[str, str], int]]
    updated_at: datetime
    report_time: datetime
    source_status: tuple[str, ...]
    error: str = ""

    def top(self, period: str) -> list[tuple[tuple[str, str], int]]:
        values = self.periods.get(period, {})
        return sorted(values.items(), key=lambda item: item[1], reverse=True)[:3]

    def call_count(self, period: str) -> int:
        return sum(self.call_periods.get(period, {}).values())


def usage_snapshot_signature(snapshot: UsageSnapshot) -> tuple:
    def normalized(values: dict[str, dict[tuple[str, str], int]]) -> tuple:
        return tuple(
            (
                period,
                tuple(
                    sorted(
                        (platform, model, int(value))
                        for (platform, model), value in values.get(period, {}).items()
                    )
                ),
            )
            for period in PERIODS
        )

    return (
        normalized(snapshot.periods),
        normalized(snapshot.call_periods),
        snapshot.report_time.isoformat(),
    )


def usage_snapshot_to_json(snapshot: UsageSnapshot) -> dict:
    def encode(values: dict[str, dict[tuple[str, str], int]]) -> dict[str, list[dict]]:
        return {
            period: [
                {
                    "platform": platform,
                    "model": model,
                    "value": int(value),
                }
                for (platform, model), value in sorted(values.get(period, {}).items())
            ]
            for period in PERIODS
        }

    return {
        "periods": encode(snapshot.periods),
        "call_periods": encode(snapshot.call_periods),
        "updated_at": snapshot.updated_at.isoformat(),
        "report_time": snapshot.report_time.isoformat(),
        "source_status": list(snapshot.source_status),
    }


def usage_snapshot_from_json(payload: dict) -> UsageSnapshot:
    def decode(name: str) -> dict[str, dict[tuple[str, str], int]]:
        decoded = {period: {} for period in PERIODS}
        source = payload.get(name) or {}
        for period in PERIODS:
            for row in source.get(period) or []:
                key = (str(row["platform"]), str(row["model"]))
                decoded[period][key] = int(row["value"])
        return decoded

    return UsageSnapshot(
        periods=decode("periods"),
        call_periods=decode("call_periods"),
        updated_at=parse_time(payload.get("updated_at")),
        report_time=parse_time(payload.get("report_time")),
        source_status=tuple(str(value) for value in payload.get("source_status") or ()),
    )


class UsageEngine:
    def __init__(
        self,
        report_dir: Path | None = None,
        snapshot_cache_path: Path | None = None,
    ):
        self.report_dir = report_dir or find_report_dir()
        self.snapshot_cache_path = snapshot_cache_path or (
            Path.home() / ".tokenwatcher" / "usage_snapshot_cache.json"
        )
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot: UsageSnapshot | None = None
        self.snapshot_cache_signature: tuple | None = None
        self.thread: threading.Thread | None = None
        self.baseline: Baseline | None = None
        self.codex: CodexTailTracker | None = None
        self.claude: ClaudeTailTracker | None = None
        self.claude_usage = ClaudeUsageCache()
        self.claude_boundary: datetime | None = None
        self.cline: ClinePoller | None = None
        self._load_snapshot_cache()
        if self.snapshot is None:
            self._load_report_preview()

    def _load_snapshot_cache(self) -> None:
        try:
            payload = json.loads(self.snapshot_cache_path.read_text(encoding="utf-8"))
            if int(payload.get("version") or 0) != USAGE_SNAPSHOT_CACHE_VERSION:
                return
            cached_report_dir = Path(str(payload["report_dir"])).resolve()
            if cached_report_dir != self.report_dir.resolve():
                return
            snapshot = usage_snapshot_from_json(payload["snapshot"])
        except (OSError, ValueError, TypeError, KeyError):
            return
        self.snapshot = snapshot
        self.snapshot_cache_signature = usage_snapshot_signature(snapshot)

    def _load_report_preview(self) -> None:
        try:
            baseline = load_baseline(self.report_dir)
        except (OSError, ValueError, TypeError, KeyError):
            return
        if not any(baseline.periods[period] for period in PERIODS):
            return
        self.snapshot = UsageSnapshot(
            periods={
                period: dict(baseline.periods[period]) for period in PERIODS
            },
            call_periods={
                period: dict(baseline.call_periods[period]) for period in PERIODS
            },
            updated_at=baseline.refreshed_at.astimezone(SHANGHAI),
            report_time=baseline.refreshed_at.astimezone(SHANGHAI),
            source_status=("Baseline report preview; live sources are loading",),
        )

    def _save_snapshot_cache(self) -> None:
        snapshot = self.get_snapshot()
        if snapshot is None or snapshot.error:
            return
        signature = usage_snapshot_signature(snapshot)
        if signature == self.snapshot_cache_signature:
            return
        payload = {
            "version": USAGE_SNAPSHOT_CACHE_VERSION,
            "saved_at": datetime.now(SHANGHAI).isoformat(),
            "report_dir": str(self.report_dir.resolve()),
            "snapshot": usage_snapshot_to_json(snapshot),
        }
        temporary_path = self.snapshot_cache_path.with_name(
            f"{self.snapshot_cache_path.name}.{os.getpid()}.tmp"
        )
        try:
            self.snapshot_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.snapshot_cache_path)
            self.snapshot_cache_signature = signature
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _close_trackers(self) -> None:
        if self.codex is not None:
            self.codex.close()
        if self.claude is not None:
            self.claude.close()
        if self.cline is not None:
            self.cline.close()

    def _reload(self) -> None:
        baseline = load_baseline(self.report_dir)
        self._close_trackers()
        self.baseline = baseline
        self.codex = CodexTailTracker(self.baseline.refreshed_at)
        _, _, claude_boundary = self.claude_usage.read()
        self.claude = ClaudeTailTracker(
            claude_boundary,
            call_since=self.baseline.refreshed_at,
        )
        self.claude_boundary = claude_boundary
        self.cline = ClinePoller(self.baseline)

    def refresh_once(self) -> UsageSnapshot:
        try:
            summary_path = self.report_dir / "summary.json"
            current_mtime = summary_path.stat().st_mtime if summary_path.exists() else 0.0
            if self.baseline is None or current_mtime != self.baseline.report_mtime:
                self._reload()
            assert self.baseline is not None
            assert self.codex is not None
            assert self.claude is not None
            assert self.cline is not None
            self.codex.poll()
            self.cline.poll()
            claude_periods, claude_status, claude_boundary = self.claude_usage.read()
            if self.claude is None or self.claude_boundary != claude_boundary:
                if self.claude is not None:
                    self.claude.close()
                self.claude = ClaudeTailTracker(
                    claude_boundary,
                    call_since=self.baseline.refreshed_at,
                )
                self.claude_boundary = claude_boundary
            self.claude.poll()
            combined_periods = {}
            combined_call_periods = {}
            for period in PERIODS:
                values = Counter(self.baseline.periods[period])
                values.update(self.codex.periods[period])
                for key in [key for key in values if key[0] == "Claude Code"]:
                    del values[key]
                values.update(claude_periods[period] + self.claude.periods[period])
                for key in [key for key in values if key[0] == "Cline"]:
                    del values[key]
                values.update(self.cline.periods[period])
                combined_periods[period] = dict(values)
                calls = Counter(self.baseline.call_periods[period])
                calls.update(self.codex.call_periods[period])
                calls.update(self.claude.call_periods[period])
                for key in [key for key in calls if key[0] == "Cline"]:
                    del calls[key]
                calls.update(self.cline.call_periods[period])
                combined_call_periods[period] = dict(calls)
            last_event = (
                self.codex.last_event.astimezone(SHANGHAI).strftime("%H:%M:%S")
                if self.codex.last_event
                else "无新增"
            )
            claude_last_event = (
                self.claude.last_event.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
                if self.claude.last_event
                else "无新增"
            )
            snapshot = UsageSnapshot(
                periods=combined_periods,
                call_periods=combined_call_periods,
                updated_at=datetime.now(SHANGHAI),
                report_time=self.baseline.refreshed_at.astimezone(SHANGHAI),
                source_status=(
                    f"Codex 增量事件：{len(self.codex.seen)}，最新 {last_event}",
                    f"{claude_status}，尾读 {len(self.claude.seen)} 条，最新 {claude_last_event}",
                    self.cline.status,
                ),
            )
        except Exception as exc:
            previous = self.get_snapshot()
            snapshot = UsageSnapshot(
                periods=previous.periods if previous else {period: {} for period in PERIODS},
                call_periods=previous.call_periods
                if previous
                else {period: {} for period in PERIODS},
                updated_at=datetime.now(SHANGHAI),
                report_time=previous.report_time
                if previous
                else datetime(1970, 1, 1, tzinfo=SHANGHAI),
                source_status=previous.source_status if previous else (),
                error=f"{type(exc).__name__}: {exc}",
            )
        with self.lock:
            self.snapshot = snapshot
        return snapshot

    def _run(self) -> None:
        initial_cache_saved = False
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.refresh_once()
            if not initial_cache_saved:
                self._save_snapshot_cache()
                initial_cache_saved = True
            remaining = REFRESH_SECONDS - (time.monotonic() - started)
            self.stop_event.wait(max(0.05, remaining))

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="usage-live")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.5)
        self._save_snapshot_cache()
        self._close_trackers()

    def get_snapshot(self) -> UsageSnapshot | None:
        with self.lock:
            return self.snapshot


class FloatingRankRow:
    def __init__(self, parent: tk.Widget, rank: int, transparent: str):
        self.frame = tk.Frame(parent, bg=transparent)
        self.frame.pack(fill="x", pady=1)
        self.frame.grid_columnconfigure(4, minsize=140)
        self.frame.grid_columnconfigure(5, minsize=250, weight=1)
        self.rank_canvas = tk.Canvas(
            self.frame,
            bg=transparent,
            width=34,
            height=ROW_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.rank_canvas.grid(row=0, column=0, padx=(0, 2), sticky="w")
        self.rank_text = AdaptiveCanvasText(
            self.rank_canvas,
            text=str(rank),
            font_name=CASCADIA_MONO_FONT,
            font_size=TITLE_FONT_SIZE,
            position=(2, ROW_MIDDLE),
            anchor="lm",
        )
        self.platform_canvas = tk.Canvas(
            self.frame,
            bg="#4C8DFF",
            width=92,
            height=44,
            highlightthickness=0,
            borderwidth=0,
        )
        self.platform_canvas.grid(row=0, column=1, sticky="w")
        self.platform_text_id = self.platform_canvas.create_text(
            46,
            22,
            text="平台",
            font=("Microsoft YaHei UI", 11, "bold"),
            fill="#FFFFFF",
            anchor="center",
        )
        self.model_canvas = tk.Canvas(
            self.frame,
            bg=transparent,
            width=155,
            height=ROW_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.model_canvas.grid(row=0, column=2, sticky="w", padx=(4, 3))
        self.model_text = AdaptiveCanvasText(
            self.model_canvas,
            text="等待数据",
            font_name=YAHEI_BOLD_FONT,
            font_size=BODY_FONT_SIZE,
            position=(2, ROW_MIDDLE),
            anchor="lm",
        )
        self.call_canvas = tk.Canvas(
            self.frame,
            width=120,
            height=ROW_HEIGHT,
            bg=transparent,
            highlightthickness=0,
            borderwidth=0,
        )
        self.call_canvas.grid(row=0, column=3, sticky="w", padx=(0, 3))
        self.call_text = AdaptiveCanvasText(
            self.call_canvas,
            text="0",
            font_name=CASCADIA_MONO_FONT,
            font_size=BODY_FONT_SIZE,
            position=(2, ROW_MIDDLE),
            anchor="lm",
        )
        self.delta_canvas = tk.Canvas(
            self.frame,
            bg=transparent,
            width=140,
            height=ROW_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.delta_canvas.grid(row=0, column=4, sticky="e", padx=(0, 4))
        self.delta_text_id = self.delta_canvas.create_text(
            138,
            ROW_MIDDLE,
            text="",
            font=("Cascadia Mono", 11, "bold"),
            fill="#20D878",
            anchor="e",
        )

        self.token_canvas = tk.Canvas(
            self.frame,
            bg=transparent,
            width=250,
            height=ROW_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.token_canvas.grid(row=0, column=5, sticky="e")
        self.token_text = AdaptiveCanvasText(
            self.token_canvas,
            text="0",
            font_name=CASCADIA_MONO_FONT,
            font_size=BODY_FONT_SIZE,
            position=(248, ROW_MIDDLE),
            anchor="rm",
        )
        self.delta_hide_job = None
        self.call_color_job = None
        self.call_animation_jobs = []
        self.call_incoming_text_id = None
        self.call_value = 0
        self.call_foreground = None
        self.model_foreground = None
        self.token_color_job = None
        self.token_animation_jobs = []
        self.token_incoming_text_id = None
        self.token_value = 0
        self.token_foreground = None
        self.current_key: tuple[str, str] | None = None

    def update(
        self,
        key: tuple[str, str] | None,
        value: int,
        delta: int,
        call_value: int,
        call_delta: int,
    ) -> None:
        platform, model = key if key else ("—", "暂无数据")
        key_changed = key != self.current_key
        if key_changed and delta <= 0:
            self._clear_delta()
        self.current_key = key
        display_platform = "Claude" if platform == "Claude Code" else platform
        self.platform_canvas.configure(
            bg=PLATFORM_COLORS.get(platform, "#8B7CF6")
        )
        self.platform_canvas.itemconfigure(
            self.platform_text_id, text=display_platform
        )
        self.model_text.set_text(compact_model_name(model))
        self._update_token_value(
            value,
            animate=not key_changed and delta > 0,
            foreground=self.token_foreground,
            force=key_changed,
        )
        self._update_call_value(
            call_value,
            animate=not key_changed and call_delta > 0,
            foreground=self.call_foreground,
            force=key_changed,
        )
        if delta > 0:
            self._show_delta(delta)

    def _cancel_token_animation(self) -> None:
        for job in self.token_animation_jobs:
            try:
                self.frame.after_cancel(job)
            except tk.TclError:
                pass
        self.token_animation_jobs.clear()
        if self.token_incoming_text_id is not None:
            self.token_canvas.delete(self.token_incoming_text_id)
            self.token_incoming_text_id = None
        self.token_canvas.coords(self.token_text.image_id, 0, 0)
        self.token_text.render_cached()

    def _update_token_value(
        self,
        value: int,
        animate: bool,
        foreground: str | None,
        force: bool = False,
    ) -> None:
        value = int(value)
        self.token_foreground = foreground
        if value == self.token_value and not force:
            return
        self._cancel_token_animation()
        if self.token_color_job is not None:
            self.frame.after_cancel(self.token_color_job)
            self.token_color_job = None
        if not animate:
            self.token_value = value
            self.token_text.solid_color = foreground
            self.token_text.set_text(format_tokens(value))
            self.model_text.solid_color = self.model_foreground
            self.model_text.render_cached()
            return

        new_text_id = self.token_canvas.create_text(
            248,
            ROW_MIDDLE + ROW_HEIGHT,
            text=format_tokens(value),
            font=("Cascadia Mono", 13, "bold"),
            fill="#20D878",
            anchor="e",
        )
        self.token_incoming_text_id = new_text_id
        self.model_text.solid_color = "#20D878"
        self.model_text.render_cached()
        steps = 9

        def animate_step(step: int) -> None:
            progress = step / steps
            self.token_canvas.coords(
                self.token_text.image_id, 0, -round(ROW_HEIGHT * progress)
            )
            self.token_canvas.coords(
                new_text_id,
                248,
                ROW_MIDDLE + ROW_HEIGHT - round(ROW_HEIGHT * progress),
            )
            if step < steps:
                job = self.frame.after(24, animate_step, step + 1)
                self.token_animation_jobs.append(job)
                return
            self.token_canvas.coords(self.token_text.image_id, 0, 0)
            self.token_canvas.delete(new_text_id)
            self.token_incoming_text_id = None
            self.token_value = value
            self.token_animation_jobs.clear()
            self.token_text.solid_color = "#20D878"
            self.token_text.set_text(format_tokens(value))
            self.token_color_job = self.frame.after(1100, self._restore_token_color)

        animate_step(1)

    def _restore_token_color(self) -> None:
        if self.token_incoming_text_id is not None:
            self.token_canvas.delete(self.token_incoming_text_id)
            self.token_incoming_text_id = None
        self.token_text.solid_color = self.token_foreground
        self.token_text.set_text(format_tokens(self.token_value))
        self.model_text.solid_color = self.model_foreground
        self.model_text.render_cached()
        self.token_color_job = None

    def _cancel_call_animation(self) -> None:
        for job in self.call_animation_jobs:
            try:
                self.frame.after_cancel(job)
            except tk.TclError:
                pass
        self.call_animation_jobs.clear()
        if self.call_incoming_text_id is not None:
            self.call_canvas.delete(self.call_incoming_text_id)
            self.call_incoming_text_id = None
        self.call_canvas.coords(self.call_text.image_id, 0, 0)
        self.call_text.render_cached()

    def _update_call_value(
        self,
        value: int,
        animate: bool,
        foreground: str | None,
        force: bool = False,
    ) -> None:
        value = int(value)
        self.call_foreground = foreground
        if value == self.call_value and not force:
            return
        self._cancel_call_animation()
        if self.call_color_job is not None:
            self.frame.after_cancel(self.call_color_job)
            self.call_color_job = None
        if not animate:
            self.call_value = value
            self.call_text.solid_color = foreground
            self.call_text.set_text(format_tokens(value))
            return

        new_text_id = self.call_canvas.create_text(
            2,
            ROW_MIDDLE + ROW_HEIGHT,
            text=format_tokens(value),
            font=("Cascadia Mono", 13, "bold"),
            fill="#20D878",
            anchor="w",
        )
        self.call_incoming_text_id = new_text_id
        steps = 9

        def animate_step(step: int) -> None:
            progress = step / steps
            self.call_canvas.coords(
                self.call_text.image_id, 0, -round(ROW_HEIGHT * progress)
            )
            self.call_canvas.coords(
                new_text_id,
                2,
                ROW_MIDDLE + ROW_HEIGHT - round(ROW_HEIGHT * progress),
            )
            if step < steps:
                job = self.frame.after(24, animate_step, step + 1)
                self.call_animation_jobs.append(job)
                return
            self.call_canvas.coords(self.call_text.image_id, 0, 0)
            self.call_canvas.delete(new_text_id)
            self.call_incoming_text_id = None
            self.call_value = value
            self.call_animation_jobs.clear()
            self.call_text.solid_color = "#20D878"
            self.call_text.set_text(format_tokens(value))
            self.call_color_job = self.frame.after(1100, self._restore_call_color)

        animate_step(1)

    def _restore_call_color(self) -> None:
        if self.call_incoming_text_id is not None:
            self.call_canvas.delete(self.call_incoming_text_id)
            self.call_incoming_text_id = None
        self.call_text.solid_color = self.call_foreground
        self.call_text.set_text(format_tokens(self.call_value))
        self.call_color_job = None

    def _show_delta(self, delta: int) -> None:
        if self.delta_hide_job is not None:
            self.frame.after_cancel(self.delta_hide_job)
        self.delta_canvas.itemconfigure(
            self.delta_text_id,
            text=f"+{delta:,}",
            fill="#20D878",
        )
        self.delta_hide_job = self.frame.after(1600, self._clear_delta)

    def _clear_delta(self) -> None:
        self.delta_canvas.itemconfigure(self.delta_text_id, text="")
        self.delta_hide_job = None

    def set_foreground(self, foreground: str) -> None:
        self.set_solid_color(foreground)

    def set_solid_color(self, color: str | None) -> None:
        self.model_foreground = color
        self.call_foreground = color
        self.token_foreground = color
        for text in (self.rank_text, self.model_text, self.call_text, self.token_text):
            text.solid_color = color
            text.render_cached()
        self.delta_canvas.itemconfigure(self.delta_text_id, fill="#20D878")

    def adaptive_texts(self) -> tuple[AdaptiveCanvasText, ...]:
        return self.rank_text, self.model_text, self.call_text, self.token_text

class LiveUsageApp:
    def __init__(self, engine: UsageEngine, screenshot_path: Path | None = None):
        self.engine = engine
        self.period = "cumulative"
        self.transparent = "#010101"
        self.drag_origin: tuple[int, int] | None = None
        self.previous_values = {period: {} for period in PERIODS}
        self.previous_calls = {period: {} for period in PERIODS}
        self.period_changed = False
        self.last_background_check = 0.0
        self.manual_foreground = False
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.configure(bg=self.transparent)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.attributes("-topmost", True)
        if os.name == "nt":
            self.root.wm_attributes("-transparentcolor", self.transparent)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.screenshot_path = screenshot_path
        self._build()
        self._update_period_styles()
        if self.engine.get_snapshot() is not None:
            self.manual_foreground = True
            self._refresh_ui(schedule=False)
            self.manual_foreground = False
        self._position_top_right()
        self.root.update_idletasks()
        self._apply_adaptive_foregrounds()
        self.root.deiconify()
        self.root.lift()
        self.engine.start()
        self.root.after(100, self._refresh_ui)
        if screenshot_path:
            self.root.after(3500, self._save_screenshot)

    def _build(self) -> None:
        self.shell = tk.Frame(self.root, bg=self.transparent)
        self.shell.pack(fill="both", expand=True, padx=3, pady=3)
        self.header = tk.Frame(self.shell, bg=self.transparent)
        header = self.header
        header.pack(fill="x", pady=(0, 2))
        self.title_canvas = tk.Canvas(
            header,
            bg=self.transparent,
            width=230,
            height=40,
            highlightthickness=0,
            borderwidth=0,
        )
        self.title_canvas.pack(side="left")
        self.title_text = AdaptiveCanvasText(
            self.title_canvas,
            text="AI TOKEN TOP 3",
            font_name=CASCADIA_MONO_FONT,
            font_size=TITLE_FONT_SIZE,
            position=(1, 20),
            anchor="lm",
        )
        self.live_canvas = tk.Canvas(
            header,
            bg=self.transparent,
            width=98,
            height=40,
            highlightthickness=0,
            borderwidth=0,
        )
        self.live_canvas.pack(side="right", padx=(5, 0))
        self.live_text = AdaptiveCanvasText(
            self.live_canvas,
            text="AUTO ●",
            font_name=YAHEI_BOLD_FONT,
            font_size=24,
            position=(96, 20),
            anchor="rm",
        )
        self.period_frame = tk.Frame(header, bg=self.transparent)
        self.period_frame.pack(side="right", padx=(12, 5))
        self.period_texts = {}
        for period in PERIODS:
            canvas = tk.Canvas(
                self.period_frame,
                bg=self.transparent,
                width=68,
                height=40,
                highlightthickness=0,
                borderwidth=0,
                cursor="hand2",
            )
            canvas.pack(side="left")
            text = AdaptiveCanvasText(
                canvas,
                text=PERIOD_LABELS[period],
                font_name=YAHEI_BOLD_FONT,
                font_size=24,
                position=(34, 20),
                anchor="mm",
            )
            canvas.bind(
                "<Button-1>",
                lambda _event, value=period: self._set_period(value),
            )
            self.period_texts[period] = text
        self.rows_container = tk.Frame(self.shell, bg=self.transparent)
        self.rows_container.pack(fill="x")
        self.cards = [
            FloatingRankRow(self.rows_container, rank, self.transparent)
            for rank in (1, 2, 3)
        ]
        self.footer_frame = tk.Frame(self.shell, bg=self.transparent)
        self.footer_frame.pack(fill="x", pady=(2, 0))
        self.color_toggle_canvas = tk.Canvas(
            self.footer_frame,
            bg=self.transparent,
            width=68,
            height=32,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.color_toggle_canvas.pack(side="left")
        self.color_toggle_text = AdaptiveCanvasText(
            self.color_toggle_canvas,
            text="自动",
            font_name=YAHEI_BOLD_FONT,
            font_size=20,
            position=(2, 16),
            anchor="lm",
        )
        self.color_toggle_canvas.bind("<Button-1>", self._toggle_foreground)
        self.color_toggle_canvas.bind("<Button-3>", self._show_menu)
        self.footer_canvas = tk.Canvas(
            self.footer_frame,
            bg=self.transparent,
            width=760,
            height=32,
            highlightthickness=0,
            borderwidth=0,
        )
        self.footer_canvas.pack(side="right")
        self.footer_text = AdaptiveCanvasText(
            self.footer_canvas,
            text="0.5s",
            font_name=YAHEI_FONT,
            font_size=20,
            position=(758, 16),
            anchor="rm",
        )
        for widget in (
            self.shell,
            self.header,
            self.title_canvas,
            self.live_canvas,
            self.rows_container,
            self.footer_frame,
            self.footer_canvas,
        ):
            self._bind_window_actions(widget)
        for card in self.cards:
            for widget in card.frame.winfo_children():
                self._bind_window_actions(widget)
        self.menu = tk.Menu(self.root, tearoff=0)
        for period in PERIODS:
            self.menu.add_command(
                label=PERIOD_LABELS[period],
                command=lambda value=period: self._set_period(value),
            )
        self.menu.add_command(label="打开完整报告", command=self._open_report)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.close)

    def _bind_window_actions(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._drag)
        widget.bind("<Button-3>", self._show_menu)

    def _position_top_right(self) -> None:
        self.root.update_idletasks()
        requested_geometry = os.environ.get("TOKENWATCHER_WINDOW_GEOMETRY", "").strip()
        if re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", requested_geometry):
            self.root.geometry(requested_geometry)
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, self.root.winfo_screenwidth() - width - 22)
        y = 44
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _set_period(self, period: str) -> None:
        if period == self.period:
            return
        self.period = period
        self.period_changed = True
        self._update_period_styles()

    def _update_period_styles(self) -> None:
        for period, text in self.period_texts.items():
            text.set_text(
                f"• {PERIOD_LABELS[period]}"
                if period == self.period
                else PERIOD_LABELS[period]
            )

    def _capture_background(self):
        try:
            from PIL import ImageGrab

            width = self.root.winfo_width()
            height = self.root.winfo_height()
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            right = x + width
            bottom = y + height
            if os.name == "nt":
                import ctypes
                import ctypes.wintypes

                hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)
                rect = ctypes.wintypes.RECT()
                if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    get_dpi_for_window = getattr(
                        ctypes.windll.user32,
                        "GetDpiForWindow",
                        None,
                    )
                    dpi_scale = (
                        max(1.0, get_dpi_for_window(hwnd) / 96.0)
                        if get_dpi_for_window is not None
                        else 1.0
                    )
                    x, y, right, bottom = (
                        round(rect.left / dpi_scale),
                        round(rect.top / dpi_scale),
                        round(rect.right / dpi_scale),
                        round(rect.bottom / dpi_scale),
                    )
            image = ImageGrab.grab(
                (x, y, right, bottom),
                all_screens=True,
            ).convert("RGB")
            if image.size != (width, height):
                image = image.resize((width, height))
            return image
        except Exception:
            return None

    def _adaptive_texts(self) -> tuple[AdaptiveCanvasText, ...]:
        texts = [
            self.title_text,
            self.live_text,
            *self.period_texts.values(),
            self.color_toggle_text,
            self.footer_text,
        ]
        for card in self.cards:
            texts.extend(card.adaptive_texts())
        return tuple(texts)

    def _apply_adaptive_foregrounds(self) -> None:
        texts = self._adaptive_texts()
        self.root.update_idletasks()
        image = self._capture_background()
        if image is None:
            return
        prepared = [(text, text.prepare(image, self.root)) for text in texts]
        buffered = [
            (text, rendered, text.prepare_photo(rendered))
            for text, rendered in prepared
        ]
        for text, rendered, photo in buffered:
            text.apply_buffered(rendered, photo, image, self.root)
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()

    def _toggle_foreground(self, _event=None) -> None:
        self.manual_foreground = not self.manual_foreground
        if self.manual_foreground:
            target = "#000000"
            for text in self._adaptive_texts():
                text.solid_color = target
                text.render_cached()
            for card in self.cards:
                card.set_foreground(target)
            self.color_toggle_text.set_text("手动黑")
        else:
            for text in self._adaptive_texts():
                text.solid_color = None
            for card in self.cards:
                card.set_solid_color(None)
            self.color_toggle_text.set_text("自动")
            self._apply_adaptive_foregrounds()

    def _start_drag(self, event) -> None:
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag(self, event) -> None:
        if not self.drag_origin:
            return
        x = event.x_root - self.drag_origin[0]
        y = event.y_root - self.drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _show_menu(self, event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _open_report(self) -> None:
        path = self.engine.report_dir / "REPORT.html"
        if path.exists() and os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]

    def _refresh_ui(self, schedule: bool = True) -> None:
        snapshot = self.engine.get_snapshot()
        if snapshot:
            top = snapshot.top(self.period)
            previous = self.previous_values[self.period]
            previous_calls = self.previous_calls[self.period]
            for index, card in enumerate(self.cards):
                if index < len(top):
                    key, value = top[index]
                    delta = value - previous.get(key, value)
                    call_value = snapshot.call_periods.get(self.period, {}).get(key, 0)
                    call_delta = call_value - previous_calls.get(key, call_value)
                    card.update(
                        key,
                        value,
                        delta,
                        call_value,
                        0 if self.period_changed else call_delta,
                    )
                else:
                    card.update(None, 0, 0, 0, 0)
            for period in PERIODS:
                self.previous_values[period] = dict(snapshot.periods.get(period, {}))
                self.previous_calls[period] = dict(
                    snapshot.call_periods.get(period, {})
                )
            self.period_changed = False
            top_total = sum(value for _, value in top)
            source_text = (
                f"{PERIOD_LABELS[self.period]}前三 Σ {format_tokens(top_total)}  ·  "
                f"{snapshot.updated_at.strftime('%H:%M:%S.%f')[:-3]}  ·  0.5 秒"
            )
            if snapshot.error:
                self.live_text.set_text("AUTO ×")
            else:
                self.live_text.set_text("AUTO ●")
            self.footer_text.set_text(source_text)
            if (
                not self.manual_foreground
                and time.monotonic() - self.last_background_check >= 1.0
            ):
                self.last_background_check = time.monotonic()
                self._apply_adaptive_foregrounds()
        if schedule:
            self.root.after(500, self._refresh_ui)

    def _save_screenshot(self) -> None:
        if not self.screenshot_path:
            return
        try:
            image = self._compose_visual_snapshot()
            self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(self.screenshot_path)
        finally:
            self.close()

    def _compose_visual_snapshot(self):
        from PIL import Image, ImageColor, ImageDraw

        width = self.root.winfo_width()
        height = self.root.winfo_height()
        background = next(
            (
                text.last_background
                for text in self._adaptive_texts()
                if text.last_background is not None
            ),
            None,
        )
        if background is None:
            background = Image.new("RGB", (width, height), "white")
        image = background.resize((width, height)).convert("RGBA")
        for text in self._adaptive_texts():
            if text.last_image is None:
                continue
            x = text.canvas.winfo_rootx() - self.root.winfo_rootx()
            y = text.canvas.winfo_rooty() - self.root.winfo_rooty()
            image.alpha_composite(text.last_image, (x, y))

        draw = ImageDraw.Draw(image)
        badge_font = _load_image_font(YAHEI_BOLD_FONT, 22)
        delta_font = _load_image_font(CASCADIA_MONO_FONT, 22)
        for card in self.cards:
            canvas = card.platform_canvas
            x = canvas.winfo_rootx() - self.root.winfo_rootx()
            y = canvas.winfo_rooty() - self.root.winfo_rooty()
            badge_width = canvas.winfo_width()
            badge_height = canvas.winfo_height()
            draw.rectangle(
                (x, y, x + badge_width - 1, y + badge_height - 1),
                fill=ImageColor.getrgb(str(canvas.cget("bg"))),
            )
            draw.text(
                (x + badge_width // 2, y + badge_height // 2),
                str(canvas.itemcget(card.platform_text_id, "text")),
                font=badge_font,
                fill="#FFFFFF",
                anchor="mm",
            )

            delta = str(card.delta_canvas.itemcget(card.delta_text_id, "text"))
            if delta:
                delta_x = (
                    card.delta_canvas.winfo_rootx() - self.root.winfo_rootx()
                )
                delta_y = (
                    card.delta_canvas.winfo_rooty() - self.root.winfo_rooty()
                )
                draw.text(
                    (delta_x + 138, delta_y + ROW_MIDDLE),
                    delta,
                    font=delta_font,
                    fill="#20D878",
                    anchor="rm",
                )
        return image.convert("RGB")

    def close(self) -> None:
        self.engine.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def snapshot_payload(snapshot: UsageSnapshot) -> dict:
    def rows(period: str):
        return [
            {
                "platform": key[0],
                "model": key[1],
                "calls": snapshot.call_periods.get(period, {}).get(key, 0),
                "total_tokens": value,
            }
            for key, value in snapshot.top(period)
        ]

    return {
        "updated_at_shanghai": snapshot.updated_at.isoformat(),
        "report_time_shanghai": snapshot.report_time.isoformat(),
        "call_counts": {
            period: snapshot.call_count(period) for period in PERIODS
        },
        **{f"top3_{period}": rows(period) for period in PERIODS},
        "source_status": list(snapshot.source_status),
        "error": snapshot.error,
    }


def main() -> int:
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--snapshot-json", nargs="?", const="-")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="Optional directory containing summary.json and report CSV files.",
    )
    args = parser.parse_args()
    engine = UsageEngine(args.report_dir)
    if args.self_test or args.snapshot_json is not None:
        try:
            snapshot = engine.refresh_once()
            payload = snapshot_payload(snapshot)
            output = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.snapshot_json and args.snapshot_json != "-":
                Path(args.snapshot_json).write_text(output, encoding="utf-8")
            elif not getattr(sys, "frozen", False):
                print(output)
            return 0 if not snapshot.error and len(snapshot.top("cumulative")) == 3 else 1
        finally:
            engine.stop()
    instance_mutex = acquire_single_instance_mutex()
    if instance_mutex is None:
        return 0
    try:
        LiveUsageApp(engine, screenshot_path=args.screenshot).run()
    finally:
        release_single_instance_mutex(instance_mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
