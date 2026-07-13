# Usage notes

## Overlay controls

- Left-drag: move the window.
- Click a period: switch between today, week, month, and cumulative usage.
- Click the bottom-left button: lock the text color to black or white for the current run.
- Right-click: open the period/report/exit menu.

## Refresh behavior

The overlay updates every 0.5 seconds. At startup, the data engine checks the available Codex, Claude Code, and Cline history once. During runtime it uses native Windows directory-change notifications to discover created or modified logs, tails changed files from their previous byte offsets, and keeps only recently active files in the polling fallback. Older cold files are not periodically enumerated or checked. Codex token events are deduplicated by their cumulative usage snapshot, so copied history from continued or forked tasks is not added again. Claude/Cline summary JSON is only reparsed when its file metadata changes. When a model's token count or request count increases, the previous value rolls upward and the new value briefly appears in green. The corresponding model name also flashes green when its token total increases.

TokenWatcher persists Codex fingerprints, per-file offsets, session lineage, and parser state in `~/.tokenwatcher/codex_fingerprint_cache.json`. On later starts, unchanged JSONL files are verified by size and modification time but are not opened. New or appended files are read only from the cached byte offset. If the cache is missing, invalid, or from an incompatible version, it is rebuilt automatically from local history.

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
