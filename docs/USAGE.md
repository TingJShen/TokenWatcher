# Usage notes

## Overlay controls

- Left-drag: move the window.
- Click a period: switch between today, week, month, and cumulative usage.
- Click the bottom-left button: lock the text color to black or white for the current run.
- Right-click: open the period/report/exit menu.

## Refresh behavior

The overlay updates every 0.5 seconds. The data engine tails changed Codex and Claude Code log files from their previous byte offsets, keeps recently active files hot, checks cold files less often, and only reparses Claude/Cline summary JSON when file metadata changes. When a model's token count or request count increases, the previous value rolls upward and the new value briefly appears in green. The corresponding model name also flashes green when its token total increases.

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
