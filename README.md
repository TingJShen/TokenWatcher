# TokenWatcher

TokenWatcher is a lightweight Windows desktop overlay that displays the three most-used AI models and their exact token counts. It reads local Codex, Claude Code, and Cline usage data and refreshes the overlay every 0.5 seconds while using native Windows filesystem notifications for local log changes.

## Features

- Top-three models for today, this week, this month, or all time
- Exact token totals without K/M/B abbreviation
- Per-model request counts
- Event-driven log discovery, incremental tailing, and cached summary/task files
- Codex cumulative-snapshot deduplication across continued or forked tasks
- Persistent Codex fingerprint/offset cache for fast restarts
- Immediate startup display from the last verified aggregate snapshot; live sources reconcile it in the background
- Per-glyph-pixel background inversion, allowing one character to be partly black and partly white while the overlay remains transparent
- Green rolling animation when a value increases
- Transparent, always-on-top, draggable window
- WCAG-relative-luminance contrast selection for saturated and multicolored backgrounds
- Manual black/white text toggle
- Single-instance protection
- No telemetry or data upload

## Supported local data sources

- Codex: `~/.codex/sessions` and `~/.codex/archived_sessions`
- Claude Code: `~/.claude/stats-cache.json` and `~/.claude/projects`
- Cline: VS Code's `saoudrizwan.claude-dev` global storage

TokenWatcher only reads these files locally. It does not send usage records anywhere.

## Run from source

Requires Python 3.10 or later on Windows.

```powershell
python -m pip install -r requirements.txt
python src/token_watcher.py
```

Right-click the overlay to switch the time period, open an optional full report, or exit. Drag the overlay with the left mouse button.

## Optional baseline report

TokenWatcher can run without a generated report. If a compatible report already exists, point the app to the directory containing `summary.json`, `model_total.csv`, and `daily_by_platform_model.csv`:

```powershell
$env:AI_USAGE_REPORT_DIR = 'D:\path\to\report'
python src/token_watcher.py
```

Or pass it directly:

```powershell
python src/token_watcher.py --report-dir 'D:\path\to\report'
```

## Build the Windows executable

```powershell
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

The fast-start package is written to `TokenWatcher.exe` plus the adjacent
`TokenWatcher.runtime` directory. Keep them together when moving or distributing
the app. The small root executable launches the reusable runtime without unpacking
the Python application again on every start.

## Diagnostics

Print a JSON snapshot:

```powershell
python src/token_watcher.py --snapshot-json
```

Run the built-in data-source check:

```powershell
python src/token_watcher.py --self-test
```

## Privacy

The repository intentionally excludes local usage reports, logs, generated screenshots, build output, and executables. Review the source before running it if your local AI usage data is sensitive.
