# AGENTS.md

Self-contained multi-account YouTube Music liked-music downloader. The entire
application is a single script, `ytm-ll-dl-modern.py` (~7300 lines) at the repo
root, plus `install.sh`/`run.sh`, `requirements.txt`, and `README.md`. There
are no tests, linter, or CI. Verify changes by running the TUI.

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
