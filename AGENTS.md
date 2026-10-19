# AGENTS.md

Self-contained multi-account YouTube Music liked-music downloader. The entire
application is a single script, `ytm-ll-dl-modern.py` (~9100 lines) at the repo
root, plus `install.sh`/`run.sh`, `requirements.txt`, `README.md`, and the
`web/` package (a self-hosted local player UI). Tests live in `tests/` (pytest,
run by CI). There is **no linter, formatter, or type checker**; verify changes
with pytest, `node --check` for JS, and a manual run.

## Web UI (`web/`)

- `./run.sh serve [--port N] [--token T] [--lan]` starts the SPA + JSON API on
  `http://127.0.0.1:8765`. `--token` adds a bearer-token check to every
  `/api/*` request (`hmac.compare_digest`); static assets stay open so the page
  can load.
- **LAN sharing / play on another device** — `--lan` (alias `--network`)
  binds `0.0.0.0` and, if no `--token` is given, auto-generates one
  (`secrets.token_urlsafe(12)` in `parse_serve_args`; a non-loopback `--host`
  gets the same treatment). `run_server` prints a `Local:` URL plus one
  `Network:` URL per address from `web/server.py:lan_addresses()` (a
  best-effort IPv4 list: the default-route interface via a UDP connect to
  `8.8.8.8`, then `getaddrinfo(gethostname())`; never raises), each with the
  token appended as `?token=`. The SPA's `captureUrlToken()` (app.js, called
  first in `init()`) reads that param, persists it to `localStorage["ls-token"]`,
  and strips it from the address bar (`history.replaceState`) — so a phone that
  opens the printed link signs in automatically. `api()` now prompts on **any**
  401 (not only when no token is set) so a stale link's token can be corrected.
  `lan_addresses()` is a public helper; keep it side-effect-free and
  non-raising.
- **"It keeps asking for the token" is a STALE-SPA bug** — a phone that cached
  an old `app.js` has no `captureUrlToken()`, so the `?token=` link is ignored.
  `_send_static` therefore serves everything `Cache-Control: no-store` (not
  `no-cache`, which a mobile cache can keep without validators) and rewrites
  `index.html` to stamp `/assets/*.js|css` with `?v=<APP_VERSION>`, so every
  version bump forces a fresh asset. `promptToken()`'s input is
  `autocomplete=off autocapitalize=none autocorrect=off spellcheck=false`
  (phone keyboards/password managers otherwise mangle a correct token), and
  `modal()` submits the primary action on Enter from a text input. `api()`
  reconnects the SSE stream via `connectEvents()` after a successful re-auth
  (it was opened before the token existed and 401s forever otherwise), and
  `connectEvents()` is re-entrant (closes `state._events` and clears
  `state._pollTimer`) — a token change is not a new dependency to add, it
  exists precisely so a phone can recover without a manual cache clear.
- `web/server.py` is a plain `ThreadingHTTPServer` handler (no framework).
  Route dispatch is a linear chain of `if head == ...` blocks in
  `_api_get`/`_api_post` (server.py:233, :909) — **order matters**. In
  particular `head == "duplicates"` must match `len(parts) == 1`, otherwise it
  swallows `duplicates/within` (server.py:294). New routes get matched only if
  added before the trailing `404`; mirror an existing entry's
  `len(parts) == N` guard.
- API surface (GET unless noted): `/api/health`, `/api/accounts`,
  `/api/browsers` (detected browsers + profiles for cookie import),
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
  `/api/search?q=`, `/api/stats`, `/api/history?days=` (per-account play
  history, `plays.history_stats`, server.py:430), `/api/verify` (POST,
  per-account file verification, `_verify_files`, server.py:1448),
  `/api/lastfm`, `/api/lastfm/flush` (POST), `/api/plays` (POST),
  `/api/tags` (POST), `/api/queue` (GET snapshot / POST add tracks),
  `/api/queue/start|remove|clear|auto` (POST; `start` runs
  `queue.start_queued_batches`, which the scheduler tick also calls when
  `auto_start` is on), `/api/export/library` (POST, `playlists.export_library`),
  `/api/sync` (POST, `sync.sync_library_to_folder`), `/api/scheduler`
  (status), `/api/scheduler/<task>`
  (POST run-now, e.g. `premium_retry`), `/api/playlists` +
  `/api/playlists/<id>/add|remove|update|export` (POST), `/api/playlists/<id>`
  (DELETE), `/api/events` (SSE stream), `/api/art/<slug>/<video_id>.jpg`
  (disk-cached in `files/<slug>/.art_cache/`), and
  `/api/media/<slug>/<video_id>/stream` (Range-served audio; FLAC/WAV/AIFF
  transcoded to Opus via ffmpeg when `?format=opus`; `?download=1` forces an
  attachment).
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
- **Player state persistence** — `persistPlayer()`/`restorePlayer()`
  (app.js:380-438) snapshot the queue to `localStorage["ls-player"]` as
  **identities only** (`{id, account}`), never token-baked URLs. `restorePlayer()`
  runs in `init()` **after** `loadLibrary()` so art/stream URLs carry the
  current token, never autoplays, drops tracks absent from the fresh library,
  and restores shuffle/repeat. `stopPlayback()` clears the snapshot, so a
  deliberate stop is not resurrected on the next refresh.
- **Live play reporting** — `reportPlay()` fires when playback passes 60%
  (app.js:560), POSTs `/api/plays`; the server dedupes a 90s window
  (plays.py:25-26) and broadcasts a `play_recorded` SSE event (server.py:1296).
  The SPA writes the server's authoritative total back into `state.library`
  (`hit.plays = res.total`) and re-renders the stats/home view. Root cause of
  the "stats showing 0" bug: client aggregates read a one-time
  `state.library`/`/api/stats` snapshot — the server is authoritative, so any
  new counter must be echoed back into client state (POST response or SSE).
- **Clicking a track to play** — every clickable row/card must hand
  `setQueue()` the *real* library track objects (resolved by `id`+`account`),
  never fabricated ones, and the row markup must carry `data-index` so a click
  maps to the clicked track. Root cause of the "click a song name, nothing
  plays" bug: the Stats "Most played tracks — last N days" table passed
  `stream: true` (a literal boolean) with no `data-index`, so `streamUrl()`
  produced the URL `"true"` → 404, and the index fell back to row 0. Two
  guards now protect every playback entry point: `setQueue()` toasts
  `"<title>" isn't downloaded yet` and returns **without mutating player
  state** when the clicked track has no `stream`, and `streamUrl()` applies
  `withToken()` at playback time (not only at `loadLibrary()`), so a token
  entered via the 401 prompt is honoured immediately.
- **Now-playing cosmetics** — `updatePlayerUI()` calls `updateNowPlayingMeta(track)`
  (app.js), which keeps the browser tab and OS in sync with playback:
  `updateDocumentTitle()` sets `document.title` to `"▶ <title> — <artist>"`
  while playing (marker dropped on pause, reset to `"LocalSpot"` on stop),
  `setFaviconFromArt()` draws the cover into a square canvas and swaps the
  `#favicon` link to a PNG data URL (restoring `/assets/favicon.svg` when
  idle), and `applyMediaSession()` feeds the Media Session API (metadata +
  artwork + `playbackState` + action handlers, installed once by
  `wireMediaSession()` in `wirePlayer()`). `updateMediaPositionState()` runs on
  `timeupdate` so the lock-screen scrubber has duration/position. All of it is
  feature-detected and wrapped in try/catch — never let a cosmetics failure
  break playback.
- **Responsive shell (v6.6.8 polish)** — `#app` uses `height: 100vh` with a
  `100dvh` override so the player bar clears the mobile URL bar, and the shell
  respects `env(safe-area-inset-*)`. Two layout bugs fixed here, both invisible
  in unit tests and only caught by measuring the live DOM: (1) `.topbar-left`
  had **no CSS** while `.icon-btn` is `display:grid`, so the menu/back/refresh
  buttons each became a block-level box and **stacked vertically** — a
  ~120px-tall topbar on every screen size (now `display:flex`); (2) the ≤900px
  `#player { grid-template-columns: auto minmax(0,1fr) auto }` sized the side
  tracks to max-content and let the center crush to ~10px, so the play/prev/
  next buttons squashed on tablets (now `minmax(0,1fr) auto minmax(0,1fr)` plus
  `flex-shrink:0` on the control buttons). The "Up next" drawer markup also
  shipped **with no CSS at all** — it was fixed-position and off-screen until
  6.6.8, so the queue button looked broken.
- **Expanded player + double-tap seek (v6.6.9)** — tapping the mini bar's
  `#player-track` (or its `#btn-expand` chevron, or Enter while it's focused)
  opens `#player-overlay`, a full-screen view with the large `#po-art`, the
  album/year line, metadata chips, a seek bar, the full control set, sleep
  timer / mute / volume, and a hint line. `syncExpandedPlayer()` is called from
  `updatePlayerUI()` (the single choke point), so the overlay can never drift
  from the bar. Both sliders are driven by one `syncSeekUI()` (called from
  `timeupdate`) and both volume sliders by `syncVolumeUI()`; the controls
  **share** `togglePlay`/`toggleShuffle`/`toggleRepeat`/`toggleMute` with the
  bar rather than forking the logic. Close via `#po-close`, `Esc`
  (checked before the queue/nav drawers in the global `keydown`), or a tap on
  the scrim. `z-index: 105` keeps it above the mini bar (20) and search (100)
  but below the queue (118/119) and nav (120/121) drawers so those still work
  on top of it.
  - **Gotchas worth keeping**: `bindVolumeSlider` must **not** call
    `updatePlayerUI()` — that rebuilds the artwork `<img>` on every `input`
    event and flickers mid-drag; use `syncVolumeUI()`. `syncExpandedPlayer()`
    only swaps the art when `artBox.dataset.trackId` changes for the same
    reason. `.player-overlay` needs `touch-action: manipulation`, otherwise
    mobile double-tap-to-zoom delays the seek gesture.
  - **Double-tap to skip 10s** — `attachDoubleTapSeek($("#po-art"))` uses
    `pointerdown` with a 320 ms inter-tap window: the right half does `+10`,
    the left half `-10`, clamped with `Math.max(0, Math.min(d, target))`, and
    `flashSeekFeedback()` animates a `» 10s` / `10s «` chip. A *single* tap
    must never seek (verified live), which is why the mini bar's tap-to-expand
    and the artwork's double-tap-to-skip live on different elements.

## Account sign-in (browser-cookie import)

- Automatic sign-in reads youtube.com cookies from the user's own browser
  and turns them into the same header-based auth the manual paste flow
  produces. `browsercookies.py` (repo root) does the detection/decryption:
  Firefox (plaintext sqlite) plus the Chromium family (Chrome, Chromium,
  Edge, Brave, Opera, Vivaldi) on Linux/macOS/Windows, incl. DPAPI
  (Windows), Keychain (macOS) and libsecret/peanuts (Linux) key sources.
  Needs `cryptography` (in requirements). Safari is intentionally
  unsupported (machine-bound encryption).
- **Chromium key model mirrors yt-dlp** (reference implementation, not an
  import — `yt_dlp` stays excluded as a module): `os_crypt.encrypted_key`
  from `Local State` is a **Windows-only** key source — modern Chrome on
  Linux/macOS has only `os_crypt.portal` there and keeps the key in the
  login keyring (Linux: `secretstorage`/`kwallet-query`, exact label
  like `"Chrome Safe Storage"` filtered by `application` attr, locked
  items skipped; macOS: Keychain PBKDF2). v10 = peanuts/empty-password
  PBKDF2 fallbacks, v11 = keyring-password PBKDF2, v20 = app-bound →
  skipped with a clear error. Cookies `meta` schema ≥ 24 → strip the
  32-byte SHA-256 hash prefix before decoding (both orders tried).
  Linux needs `secretstorage` (in `requirements.txt`; bundled via
  conditional hiddenimports in `packaging/ytmusic.spec`).
- **Profiles matter**: real machines have many browser profiles with
  different Google accounts, and an untouched `Default` holds stale
  cookies. `browsercookies.list_profiles(browser)` lists
  `{id, name, session, last_used}` (offline hint, session-bearing
  first); `browsercookies.auto_profile(browser)` validates candidates
  newest-first with a live `LOGGED_IN` probe against
  `music.youtube.com` and returns the first live one — a single
  candidate is trusted without probing (offline/hermetic tests), and
  Firefox keeps its classic default-profile behaviour (returns None).
- App entry points: `list_importable_browsers()` and
  `import_account_from_browser(name, browser, profile)` (main script),
  which resolve an auto profile (`profile=None`), build a DevTools-style
  headers block via `browsercookies.headers_raw_for()` and reuse
  `create_account(name, headers_raw, source=...)` — `source` names the
  browser/profile in failure messages. `POST /api/accounts` accepts
  `{name, browser, profile?}`; `GET /api/browsers` reports availability
  plus each browser's `profiles`. TUI: the add-account screen's
  `#browser`/`#profile` Selects (profile options rebuild on browser
  change) + "Import sign-in from browser" button;
  SPA: `#acc-browser`/`#acc-profile`/`#acc-import`.
- **Signed-out sessions fail fast with an actionable error**:
  `import_account_from_browser()` runs `browsercookies.header_is_live()`
  (a cheap `LOGGED_IN` HTML probe, shared with `auto_profile`'s live
  probe; None never blocks) on the extracted cookie header *before*
  `create_account` and raises when YouTube reports the session signed
  out — spelled out that **music plays while signed out** so the user
  checks the avatar on `music.youtube.com`. If the probe can't tell
  (or passes) but YouTube still serves a sign-in prompt instead of the
  liked playlist, it surfaces as a renderer `KeyError`;
  `account_creation_error()` (main script) maps that shape to the same
  guidance; other failures are truncated, never dumped raw. Both the
  paste and import paths share it.
- **Google OAuth sign-in was removed deliberately — do not re-add it.**
  YouTube's internal (youtubei) API rejects every third-party OAuth bearer
  token with HTTP 400 (verified live across 10 client contexts and all
  youtubei hosts; upstream: ytmusicapi issues #812/#813/#814), and the
  official Data API cannot expose liked music without a user-owned Google
  Cloud project (the historical shared client's project has the API
  disabled and cannot be enabled by us). The only working automatic path is
  browser cookies. `_read_auth_headers()` still recognizes OAuth-shaped
  token JSON and refuses to forward it to yt-dlp (defensive leftover).

## Running

- The app **requires** a virtualenv (`./venv`) and refuses to start outside
  one (`require_virtualenv()` at ytm-ll-dl-modern.py:75). `./run.sh` simply
  re-execs `venv/bin/python ytm-ll-dl-modern.py`; running plain `python3`
  prints setup instructions and exits.
- **Standalone (frozen) builds** bypass that guard: `IS_FROZEN` is set from
  `sys.frozen` (PyInstaller), and `resolve_data_dir()` relocates data to the
  user's home directory (`~/.local/share/yt-music-downloader`,
  `~/Library/Application Support/YTMusicDownloader`, or
  `%APPDATA%\YTMusicDownloader`) unless `YTM_DATA_DIR` overrides it. External
  binaries come from `runtime_bin_dir()` (== `VENV_BIN` in source runs,
  `DATA_DIR/bin` when frozen); always use the `yt_dlp_binary()` /
  `deno_binary()` helpers rather than hardcoding `VENV_BIN / "yt-dlp"`.
  `ensure_environment()` provisions Deno (first from the copy bundled into the
  exe via `install_bundled_deno()`) and, when frozen, `install_yt_dlp()`
  downloads the official standalone yt-dlp binary so its own `--update`
  keeps working.
- `main()` (ytm-ll-dl-modern.py:9520) dispatches: `--version`/`--help`,
  `serve`/`--web` (web UI, browser optional), `--tui`, else no arguments →
  source runs go straight to the TUI; frozen runs show `launch_menu()` when a
  TTY is present, or auto-start the web UI + browser when there is none
  (double-click). `has_interactive_console()` is the TTY check.
- `./install.sh` creates `./venv`, pip-installs `requirements.txt`
  (pinned: textual, ytmusicapi, yt-dlp[default], imageio-ffmpeg,
  cryptography), then runs
  `--version` which downloads a Deno binary into `venv/bin/deno` via
  `ensure_environment()`/`install_deno()` (ytm-ll-dl-modern.py:119, :189).
- **Packaging**: `packaging/ytmusic.spec` (PyInstaller onefile, console,
  bundles `web/static` + optional vendored Deno) and `packaging/build.py`
  (vendors Deno into `packaging/.vendor/`, then builds). Build deps are in
  `packaging/requirements-build.txt`. `.github/workflows/release.yml` builds
  Windows/Linux/macOS-x64/macOS-arm64 on `v*` tags and attaches them to the
  release. yt-dlp is intentionally **not** bundled as a module.
- For day-to-day dev use `venv/bin/python ytm-ll-dl-modern.py` to skip the
  launcher. `--version` (or `-V`) prints version info without starting the
  TUI; `--help` prints usage. FFmpeg comes from `imageio-ffmpeg`; no system
  install is needed.
- Tests: `venv/bin/python -m pytest tests/ -q` (pytest only — install via
  `requirements-dev.txt`). `tests/test_spa.py` is a static structural guard on
  the SPA (no JS test harness); `tests/test_web.py` spins up the real server
  and drives HTTP flows (settings roundtrip, media streaming, token gate);
  `tests/test_standalone.py` covers frozen-mode data-dir resolution, the venv
  guard bypass, binary-path helpers, and `main()` launch dispatch.
  CI (`.github/workflows/ci.yml`) runs this plus compile, help/version, the
  venv guard, and a secret scan for cookie patterns.
- **CI secret scan matches credential *values*, not cookie *names***: the scan
  (ci.yml) looks for real leaked headers — an actual `<cookie-name>=<value>`
  assignment or an unfilled paste-your-cookie placeholder — **not** bare cookie
  names or `SAPISIDHASH` (a signature *algorithm*, not a secret). Bare names
  must stay out of the pattern because `browsercookies.py` cannot work without
  referencing them, so scanning for them produced permanent false failures.
  In tests, build the name from fragments (e.g. concatenating the
  `__Secure-` prefix with the suffix) rather than writing a literal
  `name=value`, which is indistinguishable from a real leak to the scan.
  The workflow still excludes itself via
  `:(exclude).github/workflows/ci.yml` — keep that pathspec.
- **Build pipeline / Deno rate limit**: `packaging/build.py` fetches Deno from
  the denoland/deno GitHub release, and all four matrix jobs run concurrently
  from one egress IP — which tripped `HTTP 403: rate limit exceeded` and broke
  the macOS builds. `_download()` now retries 403/429/5xx with backoff (but
  fails fast on 404), `vendor_deno()` **degrades gracefully** instead of failing
  the build, and both workflows cache `packaging/.vendor` via `actions/cache`.
  A bundled Deno is nice-to-have, not required: the app fetches it on first run.
  Keep `build.yml` and `release.yml` matrices/steps in sync.
- **Deno setup must never abort startup** (v6.7.0): the CI smoke test revealed
  a second, deeper instance of the same root cause. `ensure_environment()`
  (ytm-ll-dl-modern.py:203, called at **import time** on line 634) used to call
  `install_deno()` bare, so a 403 raised during import — the frozen binary
  could not even print `--version`. The app's own `github_json()`/`download_file()`
  now share `_urlopen_retrying()` (same retry/backoff/404-fast-fail policy as
  `packaging/build.py`), and both `install_deno` call sites go through
  `_try_install_deno()`, which warns and continues. Deno improves yt-dlp's
  challenge solving but is **not** required to start, browse, or play
  (`install_yt_dlp` already degraded this way — Deno was the outlier). Tests:
  `tests/test_standalone.py` regression cases for the warning, the quiet happy
  path, and the retry policy.
- **`YTM_FORCE_DENO_FETCH=1`** makes `install_deno`/`install_bundled_deno`
  ignore any existing/bundled Deno so the download path is exercised
  deterministically. The CI smoke test sets it with an unroutable
  `HTTPS_PROXY` and asserts the binary **exits 0** and still prints a version —
  the exact scenario that failed the macOS job. Without the hook the smoke test
  is vacuous (a bundled Deno short-circuits the fetch).
- **SPA test extraction**: `tests/test_spa.py`'s naive `extract_function()` regex
  stops at the first standalone `}`, so it can't parse functions with nested
  blocks (try/catch, e.g. `restorePlayer`). Use `extract_function_balanced()`
  (test_spa.py:101) for those.

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

## Data layout (project-local in source runs, home dir when frozen)

- The paths below are relative to `DATA_DIR`. Source runs use the repo root
  (project-local, as before); standalone builds use the per-user home location
  from `resolve_data_dir()`. `YTM_DATA_DIR` overrides both.
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
- `accounts/<slug>/.headers_auth.json` — browser request headers used as
  YouTube Music auth, produced either by the manual paste flow or by the
  automatic browser-cookie import (`browsercookies.py`). Contains cookies
  (file is mode 0600); treat as secret, never log or print.
- `files/<slug>/.inventory.json` — the authoritative per-track index
  (`{"version": 2, "tracks": {<video_id>: {video_id, qualities: {<quality>:
  {status, reason, file}}, title, artist, album, last_seen_liked,
  currently_liked}}}` plus `last_library_sync`/`liked_count`). Read it instead
  of scraping download dirs.
- `files/<slug>/.status.json` — snapshot of the last refresh scan, written by
  `save_status_snapshot()` after every library refresh. Convenience copy of
  the live scan data; recompute rather than trusting it blindly.
- App-managed state written by the web package: `files/playlists.json`
  (custom playlists), `files/.queue.json` (persistent download queue —
  `web/queue.py`, items are consumed when their batch starts), `files/<slug>/.playlists.json` (synced YTM playlists),
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

- Bump `APP_VERSION` and `APP_BUILD` (ytm-ll-dl-modern.py:65) together on any
  code change; the running app prints both with `--version`. Do not change one
  without the other.
- Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the
  four standalone executables and attaches them to the GitHub Release. The
  regular `ci.yml` still runs on push to `main`.
- `.github/workflows/build.yml` builds the same four executables on **every
  push to `main`/`master`**: a test-gate job first (install.sh + pytest),
  then the matrix (steps mirrored from release.yml — keep the two in sync),
  then a publish job that force-moves the rolling `latest-build` tag to the
  pushed commit and republishes a `latest-build` **prerelease** with
  `gh release upload --clobber` (stable download links, assets replaced in
  place). It matches only branches — never `v*` tags — so it cannot
  re-trigger release.yml.

## Caveats

- The app may be actively downloading/syncing in another terminal, writing to
  `files/` and `accounts/` concurrently. Don't assume those trees are static.
- `venv/`, `accounts/`, `files/`, `config/`, `__pycache__/`, `*.zip`,
  `dist/`, `build/`, and `packaging/.vendor/` are gitignored; account headers
  (cookies) and downloads are never committed.
- The app is intentionally a self-contained single-script project with a
  project-local `./venv`; it is **not** a PyPI package (that was considered and
  explicitly rejected). Don't restructure it into a pip-installable layout.
- The GitHub remote is `justaman045/ytmusic-liked-player` (renamed from
  `multi-music-downloader`); `gh` is
  authenticated, and CI runs on push to `main` (including the secret scan).
- After code changes: run `venv/bin/python -m pytest tests/ -q`, then
  `node --check web/static/app.js` if JS changed, then
  `venv/bin/python ytm-ll-dl-modern.py --version` and a manual smoke test
  (TUI and/or `./run.sh serve`).
