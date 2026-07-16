# Usage notes

## Overlay controls

- Left-drag: move the window.
- Click a period: switch between today, week, month, and cumulative usage.
- Click the bottom-left button: lock the text color to black or white for the current run.
- Right-click: open the period/report/exit menu.

## Refresh behavior

The overlay updates every 0.5 seconds. At startup, the data engine checks the available Codex, Claude Code, and Cline history once. During runtime it uses native Windows directory-change notifications to discover created or modified logs, tails changed files from their previous byte offsets, and keeps only recently active files in the polling fallback. Older cold files are not periodically enumerated or checked. Codex token events are deduplicated by their cumulative usage snapshot, so copied history from continued or forked tasks is not added again. Claude/Cline summary JSON is only reparsed when its file metadata changes. When a model's token count or request count increases, the previous value rolls upward and the new value briefly appears in green. The corresponding model name also flashes green when its token total increases.

The overlay keeps every non-badge area transparent. Normal text is rendered through a glyph mask: each visible text pixel samples the desktop directly beneath it and becomes black or white independently using linear-RGB relative luminance. One character can therefore be partly black and partly white across a light/dark boundary, including on saturated multicolored backgrounds. Positive `+token` deltas and the rolling value/model animation remain green. Click the `自动` indicator only when a temporary manual black override is needed; click it again to resume automatic mode.

The overlay is intentionally borderless. Any line, color band, wallpaper, or window visible between the text fields belongs to the desktop underneath the transparent overlay; only the text and colored platform badges are drawn by TokenWatcher.

TokenWatcher persists Codex fingerprints, per-file offsets, session lineage, and parser state in `~/.tokenwatcher/codex_fingerprint_cache.json`. On later starts, unchanged JSONL files are verified by size and modification time but are not opened. New or appended files are read only from the cached byte offset. If the cache is missing, invalid, or from an incompatible version, it is rebuilt automatically from local history.

Claude token deltas and request counts share one parser pass. Its offsets, fingerprints, and aggregate deltas are persisted in `~/.tokenwatcher/claude_tail_cache.json`, so unchanged project logs are metadata-checked but not reopened on normal restarts.

The last verified aggregate values are stored separately in `~/.tokenwatcher/usage_snapshot_cache.json`. This small file is tied to the resolved baseline-report directory and loaded before the overlay is shown, so token totals appear immediately while the incremental trackers reconcile newer events in the background. It is written after the first completed background refresh and on a clean exit only when totals changed; the 0.5-second UI loop does not write it.

The overlay remains hidden until cached totals have been painted. If the snapshot cache is missing or invalid, TokenWatcher uses the small baseline report as an immediate preview instead of showing a visible waiting state while local log trackers initialize.

## Data lookup order

An optional generated baseline report is resolved in this order:

1. The `--report-dir` command-line option.
2. The `AI_USAGE_REPORT_DIR` environment variable.
3. `outputs/codex_claude_usage_since_2026-02` beside the source or executable.
4. `~/.tokenwatcher/codex_claude_usage_since_2026-02`.

If no report exists, TokenWatcher starts with an empty baseline and reconstructs available usage from local Codex, Claude Code, and Cline files.

## Limitations

- Only locally available logs are counted.
- Deleted, remote-only, or unsynchronized sessions cannot be recovered.
- Claude Code cumulative totals follow its local `stats-cache.json` input-plus-output token convention.
- Cline paths currently follow the standard VS Code extension storage location on Windows.
