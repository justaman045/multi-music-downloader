# AGENTS.md

Self-contained multi-account YouTube Music liked-music downloader. The entire
application is a single script, `ytm-ll-dl-modern.py` (~8900 lines) at the repo
root, plus `install.sh`/`run.sh`, `requirements.txt`, `README.md`, and the
`web/` package (a self-hosted local player UI). Tests live in `tests/` (pytest,
run by CI). There is **no linter, formatter, or type checker**; verify changes
with pytest, `node --check` for JS, and a manual run.

## Web UI (`web/`)

- `./run.sh serve [--port N] [--token T]` starts the SPA + JSON API on
  `http://127.0.0.1:8765`. `--token` adds a bearer-token check to every
  `/api/*` request (`hmac.compare_digest`); static assets stay open so the page
  can load.
- `web/server.py` is a plain `ThreadingHTTPServer` handler (no framework).
  Route dispatch is a linear chain of `if head == ...` blocks in
  `_api_get`/`_api_post` (server.py:233, :811) — **order matters**. In
  particular `head == "duplicates"` must match `len(parts) == 1`, otherwise it
  swallows `duplicates/within` (server.py:294). New routes get matched only if
  added before the trailing `404`; mirror an existing entry's
  `len(parts) == N` guard.
- API surface (GET unless noted): `/api/health`, `/api/accounts`,
  `/api/accounts/<slug>/refresh` (POST), `/api/accounts/<slug>/unliked`
  (GET preview / POST purge), `/api/accounts/<slug>` (DELETE), `/api/library`,
  `/api/settings` (GET/POST), `/api/downloads` (POST start / GET snapshot),
  `/api/downloads/<slug>/stop|cancel`, `/api/downloads/cancel-all`,
  `/api/most-played/<slug>[/<count>]`, `/api/status/<slug>`,
  `/api/duplicates`, `/api/duplicates/export|remove` (POST),
  `/api/duplicates/within` (per-account `[{slug, name, groups:[{title,
  artist, tracks:[{video_id, title, artist, album, qualities}]}]}]`),
  `/api/duplicates/within/remove` (POST),
  `/api/upgrade-preview/<slug>?quality=` (list of tracks eligible for
  re-download at a higher quality), `/api/yt-dlp-update`, `/api/auth-health`,
  `/api/backup` (POST), `/api/backups` (list), `/api/backups/prune` (POST),
  `/api/search?q=`, `/api/stats`, `/api/lastfm`,
  `/api/lastfm/flush` (POST), `/api/plays` (POST), `/api/tags` (POST),
  `/api/scheduler` (status), `/api/scheduler/<task>` (POST run-now, e.g.
  `premium_retry`), `/api/playlists` + `/api/playlists/<id>/add|remove|
  update|export` (POST), `/api/playlists/<id>` (DELETE), `/api/events` (SSE
  stream), `/api/art/<slug>/<video_id>.jpg` (disk-cached in
  `files/<slug>/.art_cache/`), and `/api/media/<slug>/<video_id>/stream`
  (Range-served audio; FLAC/WAV/AIFF transcoded to Opus via ffmpeg when
  `?format=opus`; `?download=1` forces an attachment).
- `web/downloads.py` runs the same `DownloadWorker` the TUI uses, routing its
  events into a shared `EventBus` (`web/bus.py`) consumed via SSE. Session
  snapshots live in `DownloadManager`; a session ends with
  `finished=true`/`finished_info` (cancelled flag). Cancel is cooperative —
  it stops new tracks/retries but doesn't kill a running yt-dlp subprocess.
- `web/refresh.py` wraps the app's library-refresh path the same way.
- `web/scheduler.py` runs background tasks on timers read from settings
  (`auto_refresh_interval_hours`, `premium_retry_interval_days`,
  `backup_interval_hours`, yt-dlp check, last.fm flush). `premium_retry` and
  `flush_scrobbles` start real work, so exercising them via the UI/API will
  kick off downloads or network calls.
- `web/playlists.py` has two kinds: custom playlists stored app-wide in
  `files/playlists.json` (only these accept added tracks), and per-account
  YouTube Music playlists snapshotted to `files/<slug>/.playlists.json` by
  `sync_library_playlists` (read-only, only offered while the snapshot says
  `online: true`).
- `web/plays.py` records play counts (per account, deduped per 90s window) in
  `files/<slug>/.plays.json` and, when last.fm settings are enabled, queues
  scrobbles to `files/<slug>/.lastfm_queue.json` for a worker flush.
- `web/metadata.py` aggregates `/api/library` on demand (guarded by a lock;
  in-process tag cache + on-disk art cache — not a TTL cache, edits to
  inventory show up on the next request).
- The SPA (`web/static/index.html`, `styles.css`, `app.js`) is vanilla JS, no
  build step; edit then reload the page. Verify JS syntax with
  `node --check web/static/app.js`. The SPA stores the token in
  `localStorage` (`ls-token`), sends `Authorization: Bearer`, prompts on the
  first 401, and appends `?token=` to art/stream URLs (`withToken()` in
  app.js).

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
  launcher. `--version` (or `-V`) prints version info without starting the
  TUI; `--help` prints usage. FFmpeg comes from `imageio-ffmpeg`; no system
  install is needed.
- Tests: `venv/bin/python -m pytest tests/ -q` (pytest only — install via
  `requirements-dev.txt`). `tests/test_spa.py` is a static structural guard on
  the SPA (no JS test harness); `tests/test_web.py` spins up the real server
  and drives HTTP flows (settings roundtrip, media streaming, token gate).
  CI (`.github/workflows/ci.yml`) runs this plus compile, help/version, the
  venv guard, and a secret scan for cookie patterns.

## Downloads

- Each track is downloaded by shelling out to `venv/bin/yt-dlp` directly
  (command built at ytm-ll-dl-modern.py:4245), with `--ffmpeg-location`
  pointing at the imageio-ffmpeg binary and `--js-runtimes deno:<venv>/bin/deno`
  + `--remote-components ejs:github`. Deno is yt-dlp's JS runtime for solving
  YouTube's challenge/POT, not something the app calls itself — deleting
  `venv/bin/deno` breaks downloads.
- Downloads land as `.ytm_<video_id>.*` temp files inside the quality dir, then
  are renamed to their final name. Settings and inventory files are also
  written atomically via a sibling `<name>.tmp`. Stray `.ytm_*`/`.tmp_*`/
  `*.tmp` files (e.g. `files/.tmp_*.jpg`) are leftover temp state — safe to
  ignore and safe to delete.

## Data layout (all project-local, no repo state)

- `config/settings.json` — persisted settings. Missing keys fall back to
  `DEFAULT_SETTINGS` (ytm-ll-dl-modern.py:397; note defaults are
  `best`/`skip`, not the example values in the checked-in file). Keys now span
  download defaults (`default_quality`, `default_existing_policy`,
  `default_session`, `default_limit`, `retry_attempts`, `retry_delay`) plus
  automation/behaviour keys (`auto_refresh_interval_hours`,
  `auto_download_new_likes`, `premium_retry_interval_days`,
  `max_parallel_downloads`, `backup_interval_hours`,
  `notifications_enabled`) and last.fm (`lastfm_*`). The web server validates
  this shape on POST (`_validate_settings`), so don't add settings keys only in
  the SPA.
- `accounts/<slug>/account.json` — display metadata (name, slug, track_count).
- `accounts/<slug>/.headers_auth.json` — pasted browser request headers used as
  YouTube Music auth. Contains cookies (file is mode 0600); treat as secret,
  never log or print.
- `files/<slug>/.inventory.json` — the authoritative per-track index
  (`{"version": 2, "tracks": {<video_id>: {video_id, qualities: {<quality>:
  {status, reason, file}}, title, artist, album, last_seen_liked,
  currently_liked}}}` plus `last_library_sync`/`liked_count`). Read it instead
  of scraping download dirs.
- `files/<slug>/.status.json` — snapshot of the last refresh scan, written by
  `save_status_snapshot()` after every library refresh. Convenience copy of
  the live scan data; recompute rather than trusting it blindly.
- App-managed state written by the web package: `files/playlists.json`
  (custom playlists), `files/<slug>/.playlists.json` (synced YTM playlists),
  `files/<slug>/.plays.json` (play counts), `files/<slug>/.lastfm_queue.json`
  (pending scrobbles), `config/lastfm_session.json` (last.fm session auth).
  Dotfiles under `files/`/`accounts/` are app-managed state; do not edit by
  hand.
- Legacy `files/.ytm_index.json`, `files/<slug>/.ytm_index.json`, and
  `files/.index` (a SQLite file) are stale artifacts — the current script never
  references them.
- Download directories live under `files/<slug>/`, named by quality via
  `names_for_quality()` (ytm-ll-dl-modern.py:2164): `best`→`Best_Source`,
  `mp3_320`→`MP3_320kbps`, `mp3_v0`→`MP3_V0`, `mp3_256`→`MP3_256kbps`,
  `mp3_192`→`MP3_192kbps`, `mp3_128`→`MP3_128kbps`, `flac`→`FLAC`,
  `m4a_256`→`M4A_AAC_256kbps`.

## Versioning / releases

- Bump `APP_VERSION` and `APP_BUILD` (ytm-ll-dl-modern.py:63) together on any
  code change; the running app prints both with `--version`. Do not change one
  without the other.

## Caveats

- The app may be actively downloading/syncing in another terminal, writing to
  `files/` and `accounts/` concurrently. Don't assume those trees are static.
- `venv/`, `accounts/`, `files/`, `config/`, `__pycache__/`, and `*.zip` are
  gitignored; account headers (cookies) and downloads are never committed.
- After code changes: run `venv/bin/python -m pytest tests/ -q`, then
  `node --check web/static/app.js` if JS changed, then
  `venv/bin/python ytm-ll-dl-modern.py --version` and a manual smoke test
  (TUI and/or `./run.sh serve`).
