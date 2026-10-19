# YT Music Liked Player

> **Download your YouTube Music liked library, then play it like Spotify — locally.**

A self-contained, multi-account **YouTube Music liked-music downloader** with
a terminal UI (TUI) and a local Spotify-style web player. Add one or more
YouTube Music accounts, refresh your liked library, and download every liked
track at your chosen quality — then browse, search and stream the whole
collection from a player that runs on your own machine and never phones home.

Everything runs inside a project-local virtualenv (`./venv`): Python
dependencies, a `yt-dlp` binary, a Deno binary (used by yt-dlp for YouTube
challenges), and FFmpeg. No system-wide installs are required.

---

## Features

- **Multiple accounts** — one sidebar, switch between them freely.
- **Full liked library** — fetches the *complete* Liked Music playlist, not
  just the first page.
- **Authentication from your own browser, automatically *or* pasted
  headers** — the app reads your signed-in youtube.com cookies straight
  out of Chrome/Firefox/Edge/Brave/Opera/Vivaldi (**Import sign-in**,
  no pasting or Google setup needed), or paste a request copied from
  your browser's Developer Tools.
- **8 quality options**, chosen per download session:
  `best`, `MP3 320`, `MP3 V0`, `MP3 256`, `MP3 192`, `MP3 128`, `FLAC`, `M4A 256`.
- **Immutable session settings** — the quality/policy you pick stays fixed for
  the whole session once downloading starts.
- **Skip or replace** existing files at the selected quality.
- **Persistent per-track inventory** — knows what was downloaded, at which
  quality, and with which result, across runs.
- **Smart scan** — detects missing files, liked-but-never-downloaded tracks,
  unliked tracks, and failed/skipped tracks with reasons.
- **Most Played** — music-stats summary of your liked library.
- **Cross-account duplicate detection** with one-click removal.
- **Repair downloads** — re-download any currently-liked track that is
  missing or failed at the chosen quality.
- **Purge no-longer-liked** — remove local files and inventory records for
  songs that were unliked on YouTube Music.
- **Session results** — every finished download session reports per-track
  outcomes with one-click retry of the failed ones.
- **Persistent JSON reports** for everything.
- **Self-contained runtime** — project-local yt-dlp, Deno, and FFmpeg.
- **Premium-lock detection** — tracks behind the YouTube Music Premium wall
  are classified separately from ordinary failures and reported in status.
- **Local web player** — a browser UI (served by the app itself) that can
  stream any downloaded file: browse by artist/album/year/genre, search
  across all accounts, and trigger refreshes/downloads.
- **Play tracking + last.fm** — play counts are recorded locally per account
  and, if you opt in, scrobbled to last.fm in the background.
- **Playlists** — custom playlists stored app-wide plus a snapshot of each
  account's YouTube Music playlists.
- **Scheduler** — optional background jobs for auto library refresh, retrying
  premium-gated downloads, and periodic backups.
- **Backups** — one-click zip backups of settings, accounts, and inventory,
  with on-demand restore and pruning.
- **Smart views** — saveable filter rules for the library (download status,
  account, play-count ranges, recently added, sort + limit), with built-in
  views like "Missing", "Premium", and "Most played"; stored in your browser.
- **Tag editor** — edit title/artist/album/artwork on any downloaded file
  from the song list (`✎`), written straight into the audio tags.
- **Download queue & scheduling** — line up tracks with the **+Q** row
  button, then start the whole queue in one batch with your chosen
  quality/policy; an auto-start toggle picks up the next batch whenever a
  session slot frees up. The Downloads view also shows every scheduled
  background task with one-click **Run now**.
- **Export & sync** — export the library as M3U/CSV/JSON, or mirror
  downloaded files into any target folder (hardlinks when possible, copy
  otherwise — idempotent and safe to re-run).
- **Listening insights** — play streaks, week-over-week comparison,
  hour-of-day and per-day charts, top tracks/artists, and a last.fm panel
  with manual flush, all computed from your local play log.
- **Works on phones** — the web UI is responsive: off-canvas navigation,
  simplified tables, and touch-friendly controls below 900px.
- **Expandable player** — tap the now-playing bar (or its chevron) for a
  full-screen view with large album art, album/year, library chips, a seek
  bar, the full control set, sleep timer, mute and volume — all mirrored
  with the compact bar.
- **Double-tap to skip 10 seconds** — double-tap the album art: right half
  jumps forward, left half back, clamped to the track and confirmed with an
  on-screen flash. A single tap never seeks.
- **OS-level now-playing** — while a track plays, the tab title becomes
  `▶ <title> — <artist>`, the album art becomes the favicon, and the
  lock-screen / media-key controls (play, pause, next, previous, seek)
  drive the player.
- **Play on another device** — `./run.sh serve --lan` shares the player on
  your local network with an auto-generated token; open the printed link on
  your phone and it signs in automatically (works on your TV, tablet, or a
  second computer on the same Wi-Fi).

---

## Requirements

- Linux, macOS, or Windows
- Python **3.10+** (`python3`)
- A **YouTube Music account** and the browser you use to log into it
- Network access (PyPI, GitHub for the Deno download, YouTube Music)

No global FFmpeg, Deno, or system packages are required.

---

## Quick start

### Standalone (no install required)

Download the build for your OS from the **Releases** page and double-click it:

| Platform | File |
|---|---|
| Windows | `YTMusicDownloader-windows-x64.exe` |
| Linux | `YTMusicDownloader-linux-x64` |
| macOS (Apple Silicon) | `YTMusicDownloader-macos-arm64` |
| macOS (Intel) | `YTMusicDownloader-macos-x64` |

The [Releases page](../../releases) carries two kinds of builds — **tagged
releases** (`v6.x.y`, stable) and the
[`latest-build` prerelease](../../releases/tag/latest-build), which CI
rebuilds and republishes on **every push to `main`** with the same four
files. Download links never change, so you can always grab the newest build
from one place.

- **Windows:** double-click the `.exe` → a console window opens with a menu. If Windows SmartScreen shows "Microsoft Defender SmartScreen prevented an unrecognized app from starting", click **More info** → **Run anyway** (this is normal for unsigned builds).
- **Linux / macOS:** double-clicking a bare binary opens no terminal, so it
  starts the **web UI** and opens your browser automatically. If your file manager doesn't allow double-click to execute, run it from a terminal: `./YTMusicDownloader-linux-x64` or `./YTMusicDownloader-macos-*`. 
- **macOS Gatekeeper:** unsigned builds will show "cannot be opened because it is from an unidentified developer" on first launch. Right-click the app/binary → **Open**, then confirm (or run `xattr -d com.apple.quarantine <path>` in Terminal). 
- To get the TUI/menu explicitly, run it from a terminal: `./YTMusicDownloader-linux-x64`
  (on macOS, right-click → Open the first time, or
  `xattr -d com.apple.quarantine <file>`).
- To play on another device from a standalone build, run it from a
  terminal with the LAN flag: `./YTMusicDownloader-linux-x64 serve --lan`
  (on macOS, `./YTMusicDownloader-macos-arm64 serve --lan`). It prints a
  `Network:` URL to open on your phone — see
  [Play on another device](#play-on-another-device-phone-tablet-another-computer).

Everything—Python, textual, FFmpeg, and Deno—is inside the one file. On first
run it provisions the yt-dlp binary (and Deno if not bundled) into the data
directory, so that first start needs a network connection. Subsequent starts
are offline-ready.

Your data lives in the home directory (not next to the file):

| Platform | Location |
|---|---|
| Windows | `%APPDATA%\YTMusicDownloader\` |
| macOS | `~/Library/Application Support/YTMusicDownloader/` |
| Linux | `~/.local/share/yt-music-downloader/` |

Set `YTM_DATA_DIR` to override the location (useful for a USB stick or to keep
multiple profiles side by side).

### From source (developers)

```bash
git clone <repo-url>
cd <repo-name>
./install.sh     # one-time setup: creates ./venv and installs everything
./run.sh         # start the app
```

`install.sh`:
1. Creates a virtualenv in `./venv`.
2. Installs the pinned dependencies from `requirements.txt`.
3. Downloads a project-local Deno binary (used by yt-dlp).
4. Verifies the runtime and prints the version.

### Manual setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python ytm-ll-dl-modern.py
```

The app **requires** a virtualenv and refuses to run outside one — the
virtualenv holds the project-local yt-dlp/Deno/FFmpeg toolchain. Use
`./run.sh` or `venv/bin/python ytm-ll-dl-modern.py`.

### Command-line flags

```bash
./run.sh --version   # print version info and exit
./run.sh --help      # show usage
./run.sh --tui       # start the terminal UI
./run.sh --web       # start the web UI and open a browser
```

### Building the standalone executable

```bash
venv/bin/pip install -r packaging/requirements-build.txt
venv/bin/python packaging/build.py          # -> dist/YTMusicDownloader
venv/bin/python packaging/build.py --no-deno  # smaller build
```

The `Release` GitHub Actions workflow builds all four targets automatically
when a `v*` tag is pushed, and attaches them to the release. The
`Build executables` workflow builds the same four targets on **every push
to `main`/`master`** (gated on the test suite) and refreshes the rolling
`latest-build` prerelease, so there is always a fresh downloadable binary.

### Web UI (local Spotify-style player)

Run the browser UI alongside or instead of the TUI:

```bash
./run.sh serve                 # http://127.0.0.1:8765
./run.sh serve --port 9000     # custom port
./run.sh serve --token <tok>   # require a bearer token in every API call
./run.sh serve --open          # also open your default browser
```

The web UI is a self-hosted local player for your downloaded music: browse
by artist, album, year, or genre; stream any downloaded file (FLAC is
transcoded to Opus for browsers that can't play it); search across all
accounts; edit tags, build smart views of the library, queue downloads,
export or sync the library to a folder, and read listening insights — all
without sending anything off-machine. It works on desktop and phone
alike. When the server runs with `--token`, the UI prompts
for the token on first load and remembers it.

#### The player

The bar along the bottom is the compact player. Tap (or click) the track
info — or the chevron on its right — to **expand it into a full-screen
now-playing view** with the large album art, album/year, library chips
(account, quality, genre, play count), a seek bar, the full
shuffle/previous/play/next/repeat set, a sleep timer, mute and volume.

**Double-tap the album art to skip 10 seconds**: the right half jumps
forward, the left half jumps back (`» 10s` / `10s «` flashes on screen to
confirm). It is clamped to the track, and a single tap never seeks. On
phones this replaces pinch-zooming on the artwork, so it behaves like the
streaming apps people are used to.

Close the expanded view with the chevron, `Esc`, or a tap outside the
album art. Everything you change there (shuffle, repeat, volume, seek) is
mirrored in the compact bar, and vice versa.

While a track plays, the browser tab and your OS stay in sync: the tab
title becomes `▶ <title> — <artist>`, the album art replaces the favicon,
and the lock-screen / media-key controls (play, pause, next, previous,
seek) drive the player.

#### Play on another device (phone, tablet, another computer)

By default the server listens on `127.0.0.1` only, so other devices can't
reach it. To share it on your local network:

```bash
./run.sh serve --lan           # listen on all interfaces + auto token
```

It prints a **Local** URL plus one **Network** URL per address, for example:

```
  Local:   http://127.0.0.1:8765/?token=u67hxy9URxFptdBn
  Network: http://192.168.1.42:8765/?token=u67hxy9URxFptdBn
```

Open a **Network** URL on any device connected to the same Wi-Fi/Ethernet.
The token is embedded in the link, so the page signs in automatically (it
is then stored in that device's browser and stripped from the address
bar). Nothing is exposed to the internet — only your LAN — and the token
still gates every `/api/*` call, so keep the link to yourself.

Notes:

- Sharing on the network **requires a token**; `--lan` generates a random
  one for you. Pass `--token <yours>` to pick your own, or `--host 0.0.0.0`
  if you'd rather manage the bind yourself (a non-loopback bind without a
  token auto-generates one too).
- Different port: `./run.sh serve --lan --port 8766` (useful when the
  default is already taken).
- If a phone can't connect, check your firewall allows the port and that
  both devices are on the same network (guest Wi-Fi and VPNs often
  isolate clients from each other).
- If the page keeps asking for the token even though the link contains
  one, do a one-time hard refresh (or clear the site's cache) on that
  device: it was holding an older copy of the player. The server now
  version-stamps its assets and sends `no-store`, so this can't happen
  again after a normal reload.
- `--network` is accepted as an alias for `--lan`.

API endpoints are prefixed `/api/`: `/api/health`, `/api/accounts`,
`/api/browsers`, `/api/accounts/<slug>/refresh`, `/api/accounts/<slug>/unliked`,
`/api/library`, `/api/settings`, `/api/downloads`, `/api/most-played/<slug>`,
`/api/status/<slug>`, `/api/duplicates` (+ `within`, `export`, `remove`),
`/api/upgrade-preview/<slug>`, `/api/yt-dlp-update`, `/api/auth-health`,
`/api/backup` + `/api/backups` (+ `prune`), `/api/search?q=`, `/api/stats`,
`/api/history`, `/api/lastfm` (+ `flush`), `/api/plays`, `/api/tags`,
`/api/queue` (+ `start`/`remove`/`clear`/`auto`),
`/api/export/library`, `/api/sync`, `/api/scheduler` (+ run-now),
`/api/playlists` (+ add/remove/update/export/
import/sync), `/api/media/<slug>/<video_id>/stream` (Range-served audio),
`/api/art/<slug>/<video_id>.jpg`, `/api/verify`, and `/api/events`
(Server-Sent Events for live download/refresh progress).

---

## First run: add an account

**Option A — import your browser's sign-in (easiest):**

1. Sign into YouTube Music in the browser you normally use.
2. Launch the app (`./run.sh`) and press **+ Add Account** (or `a`).
3. Type a display name, pick that browser in the list — and pick the
   **profile** signed into YouTube Music, or leave the profile on
   **Auto-detect (recommended)**: the app then checks each signed-in
   profile against YouTube and uses the one with a live session (so an
   untouched `Default` profile is never mistaken for your current one).
   Then press **Import sign-in from browser** (in the web UI:
   **Import sign-in** on the Accounts page).
4. The app reads your youtube.com cookies from the browser's local
   cookie store — nothing leaves your machine — and fetches your
   liked library.

   *Notes:* on Linux the cookie key lives in your login keyring
   (GNOME Keyring/KWallet) — if the import complains it can't be read,
   unlock the keyring, close the browser completely, and retry. On
   Windows/macOS, close the browser completely first if the import
   reports a locked cookie store; Chrome 127+ can encrypt cookies in a
   way no outside app can read (app-bound/"v20") — Firefox, Edge,
   Brave, Opera or Vivaldi are unaffected. If the app reports the
   session is signed out, remember that **music plays while signed
   out** — open `music.youtube.com` in that browser and check the
   avatar at the top right before retrying, or pick a different
   profile.

**Option B — paste browser headers (fallback):**

1. Log into YouTube Music in your browser.
2. Open **Developer Tools → Network**.
3. Find a `POST` request to `/browse` with status `200`.
4. Right-click it → **Copy → Copy request headers**.
5. Paste the complete request headers into the dialog and press **Test & Add**.

> Why no "Sign in with Google" button? Google blocks third-party apps
> from reading a YouTube Music library over OAuth — its internal API
> rejects every third-party OAuth token, and the official Data API
> can't expose liked music without a Google Cloud project of your own.
> Browser-cookie import is the reliable, fully-local equivalent.

Both auth types are stored locally under `accounts/<slug>/` with
restrictive file permissions (mode 0600). They are never logged or sent
anywhere except YouTube Music.

---

## Daily use

- Select an account in the sidebar.
- **Refresh Liked Music** (`r`) — pull the current liked playlist and scan
  the existing download inventory.
- **Download All** (`d`) — choose quality and policy, then download
  everything that's missing.
- **Most Played** (`m`) — browse play/album statistics for your library.
- **Duplicates** (`u`) — find the same track downloaded for multiple
  accounts and remove duplicates with one click.
- **Library Status** (`s`) — snapshot of the last refresh scan, with
  **Repair Downloads** and **Purge Unliked** actions.
- **Play Stats** (`h`) — local listening stats: total plays, per-account
  breakdown, top tracks/artists, and your current streak (same data as the
  web insights view).
- **Settings** (`p`) — change default quality / existing-file policy.
- **Remove** — delete an account and its local data.

Downloads stream to `files/<slug>/<Quality_Dir>/` as `.ytm_<video_id>.*`
temp files first, then are renamed to their final tagged filename.

---

## How it works

| Path | Purpose |
| --- | --- |
| `ytm-ll-dl-modern.py` | The entire application (~9100 lines, single file). |
| `install.sh` / `run.sh` | Setup and launcher scripts. |
| `requirements.txt` | Pinned Python dependencies. |
| `venv/` | Project-local Python environment + yt-dlp/Deno binaries (created by `install.sh`). |
| `config/settings.json` | Persistent settings (created on first run). |
| `accounts/<slug>/account.json` | Display metadata for an account. |
| `accounts/<slug>/.headers_auth.json` | Your pasted request headers (secret — mode 0600). |
| `files/<slug>/.inventory.json` | Authoritative per-track index (download state, quality, results). |
| `files/<slug>/.status.json` | Snapshot of the last refresh scan. |
| `files/<slug>/<Quality_Dir>/` | Downloaded music, e.g. `MP3_320kbps`, `FLAC`. |
| `web/` | The web player package: HTTP server, SPA, downloads/refresh workers, play tracking, playlists, and scheduler. |

The app writes every file atomically (temp file + rename), so an
interrupted run never leaves a half-written index behind.

---

## Troubleshooting

**Port already in use ("Address already in use" / EADDRINUSE)**
If you try to start the web UI while another instance is already running, you'll see:
```
Port 8765 is already in use.
You have another instance of the web UI already running.
Stop the other process, or start on a different port:
    ./run.sh serve --port 8766
    ytm-ll-dl-modern.py serve --port 8766
```

Pick any free port and use it with `--port`.

**"This application must run from inside a virtualenv."**
Run `./install.sh` once, then `./run.sh`.

**Downloads fail after a fresh clone.**
The Deno binary lives inside `./venv` and is fetched on first `install.sh`
or first app start. Check network access to GitHub if it's missing.

**Auth stopped working.**
YouTube session cookies expire. Re-run **+ Add Account** with fresh request
headers (or remove and re-add the account).

**Leftover `.ytm_*` / `.tmp_*` files.**
These are interrupted-download temp state. They're safe to delete when the
app isn't running.

---

## Roadmap

- Spotify, Amazon Music, and Apple Music support
- On-demand track search-and-download from the TUI
- Your feature requests are welcome

## Notes

- This is your own music; downloading your liked library is personal use.
  Respect each service's terms of use.
- Licensed under the MIT License — see [LICENSE](LICENSE).
