# AGENTS.md

Self-contained multi-account YouTube Music liked-music downloader. The entire
application is a single script, `ytm-ll-dl-modern.py` (~7300 lines) at the repo
root, plus `install.sh`/`run.sh`, `requirements.txt`, `README.md`, and the
`web/` package (a self-hosted local player UI). There are tests in `tests/`
(pytest) run via CI, but no linter. Verify changes by running the TUI or the
web server.

## Web UI (`web/`)

- `./run.sh serve [--port N] [--token T]` starts the SPA + JSON API on
  `http://127.0.0.1:8765`. `--token` adds a bearer-token check to every
  `/api/*` request (constant-time compare).
- `web/server.py` is a plain `ThreadingHTTPServer` handler (no framework).
  Routes: `/` + `/assets/*` (static SPA), `/api/health`, `/api/accounts`,
  `/api/accounts/<slug>/refresh` (POST), `/api/library` (aggregated metadata
  via `web/metadata.py`, cached), `/api/library/<slug>`, `/api/settings`
  (GET/POST), `/api/downloads` (POST start / GET snapshot),
  `/api/downloads/<slug>/stop|cancel`, `/api/downloads/cancel-all`,
  `/api/most-played/<slug>[/<count>]`, `/api/duplicates`, `/api/backup`,
  `/api/events` (SSE bus stream), `/api/art/<slug>/<video_id>.jpg` (album art,
  cached in `files/<slug>/.art_cache/`), and
  `/api/media/<slug>/<video_id>/stream` (Range-served audio; FLAC/WAV/AIFF are
  transcoded to Opus via ffmpeg when `?format=opus`, and `?download=1` forces
  a Content-Disposition attachment).
- `web/downloads.py` runs the same `DownloadWorker` the TUI uses, routing its
  events into a shared `EventBus` (`web/bus.py`) consumed via SSE. Session
  snapshots live in `DownloadManager`; a session ends with
  `finished=true`/`finished_info` (cancelled flag). Cancel is cooperative —
  it stops new tracks/retries but doesn't kill a running yt-dlp subprocess.
- `web/refresh.py` wraps the app's library-refresh path the same way.
- The SPA (`web/static/index.html`, `styles.css`, `app.js`) is vanilla JS, no
  build step; edit then reload the page. Verify JS syntax with
  `node --check web/static/app.js`.
- With `--token`, the gate only protects `/api/*`; static assets stay open so
  the page can load. The SPA stores the token in `localStorage` (`ls-token`),
  sends it as `Authorization: Bearer`, prompts on first 401, and appends
  `?token=` to art/stream URLs (`withToken()` in app.js, applied in
  `loadLibrary`).

## Running

- The app **requires** a virtualenv (`./venv`) and refuses to start outside
  one (`require_virtualenv()` at ytm-ll-dl-modern.py:73). `./run.sh` simply
  re-execs `venv/bin/python ytm-ll-dl-modern.py`; running plain `python3`
  prints setup instructions and exits.
- `./install.sh` creates `./venv`, pip-installs `requirements.txt`
  (pinned: textual, ytmusicapi, yt-dlp[default], imageio-ffmpeg), then runs
  `--version` which downloads a Deno binary into `venv/bin/deno` via
  `ensure_environment()`/`install_deno()` (ytm-ll-dl-modern.py:119, :189).
- For day-to-day dev use `venv/bin/python ytm-ll-dl-modern.py` to skip the
  launcher. `venv/bin/python ytm-ll-dl-modern.py --version` (or `-V`) prints
  version info without starting the TUI; `--help` prints usage.
- FFmpeg comes from `imageio-ffmpeg`; no system install is needed.

## Downloads

- Each track is downloaded by shelling out to `venv/bin/yt-dlp` directly
  (ytm-ll-dl-modern.py:3019), with `--ffmpeg-location` pointing at the
  imageio-ffmpeg binary and `--js-runtimes deno:<venv>/bin/deno` +
  `--remote-components ejs:github`. Deno is yt-dlp's JS runtime for solving
  YouTube's challenge/POT, not something the app calls itself — deleting
  `venv/bin/deno` breaks downloads.
- Downloads land as `.ytm_<video_id>.*` temp files inside the quality dir, then
  are renamed to their final name. Settings and inventory files are also
  written atomically via a sibling `<name>.tmp`. Stray `.ytm_*`/`.tmp_*`/
  `*.tmp` files (e.g. `files/.tmp_*.jpg`) are leftover temp state — safe to
  ignore and safe to delete.

## Data layout (all project-local, no repo state)

- `config/settings.json` — defaults `default_quality`, `default_existing_policy`,
  `retry_attempts`, `retry_delay`. Missing keys fall back to `DEFAULT_SETTINGS`
  (ytm-ll-dl-modern.py:397; note defaults are `best`/`skip`, not the example
  values in the checked-in file).
- `accounts/<slug>/account.json` — display metadata (name, slug, track_count).
- `accounts/<slug>/.headers_auth.json` — pasted browser request headers used as
  YouTube Music auth. Contains cookies (file is mode 0600); treat as secret,
  never log or print.
- `files/<slug>/.inventory.json` — the authoritative per-track index
  (`{"version": 2, "tracks": {<video_id>: {video_id, qualities: {<quality>:
  {status, reason, file}}, title, artist, album, last_seen_liked,
  currently_liked}}}` plus `last_library_sync`/`liked_count`). Read it instead
  of scraping download dirs.
- `files/<slug>/.status.json` — snapshot of the last refresh scan
  (`{version, quality, timestamp, counts, missing, not_downloaded}`), written
  by `save_status_snapshot()` after every library refresh. Convenience copy of
  the live scan data; recompute rather than trusting it blindly.
- Legacy `files/.ytm_index.json`, `files/<slug>/.ytm_index.json`, and
  `files/.index` (a SQLite file) are stale artifacts — the current script never
  references them. Dotfiles under `files/`/`accounts/` are app-managed state; do
  not edit by hand.
- Download directories live under `files/<slug>/`, named by quality
  (ytm-ll-dl-modern.py:419): `best`→`Best_Source`, `mp3_320`→`MP3_320kbps`,
  `mp3_v0`→`MP3_V0`, `mp3_256`→`MP3_256kbps`, `mp3_192`→`MP3_192kbps`,
  `mp3_128`→`MP3_128kbps`, `flac`→`FLAC`, `m4a_256`→`M4A_AAC_256kbps`.

## Versioning / releases

- Bump `APP_VERSION` and `APP_BUILD` (ytm-ll-dl-modern.py:63) together on any
  change; the running app also reads these.
- A release is packaged as `ytm-ll-dl-modern-v<VER>.zip` containing the script
  plus `VERSION.txt` and `SHA256SUMS.txt` (sha256 of the script). Old release
  snapshots get pruned, so the root `.py` is the single source of truth.

## Caveats

- The app may be actively downloading/syncing in another terminal, writing to
  `files/` and `accounts/` concurrently. Don't assume those trees are static.
- `venv/`, `accounts/`, `files/`, `config/`, `__pycache__/`, and `*.zip` are
  gitignored; account headers (cookies) and downloads are never committed.
- No automated checks exist; after code changes, at minimum run
  `venv/bin/python ytm-ll-dl-modern.py --version` and a manual TUI smoke test.
