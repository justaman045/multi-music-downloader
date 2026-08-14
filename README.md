# YT Music Downloader

A self-contained, multi-account **YouTube Music liked-music downloader** with
a terminal UI (TUI). Add one or more YouTube Music accounts, refresh your
liked library, and download every liked track at your chosen quality — all
in a single terminal app.

Everything runs inside a project-local virtualenv (`./venv`): Python
dependencies, a `yt-dlp` binary, a Deno binary (used by yt-dlp for YouTube
challenges), and FFmpeg. No system-wide installs are required.

---

## Features

- **Multiple accounts** — one sidebar, switch between them freely.
- **Full liked library** — fetches the *complete* Liked Music playlist, not
  just the first page.
- **Authentication from browser headers** — paste a request copied from your
  browser's Developer Tools; no OAuth dance.
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

---

## Requirements

- Linux, macOS, or Windows
- Python **3.10+** (`python3`)
- A **YouTube Music account** and the browser you use to log into it
- Network access (PyPI, GitHub for the Deno download, YouTube Music)

No global FFmpeg, Deno, or system packages are required.

---

## Quick start

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
```

### Web UI (local Spotify-style player)

Run the browser UI alongside or instead of the TUI:

```bash
./run.sh serve                 # http://127.0.0.1:8765
./run.sh serve --port 9000     # custom port
./run.sh serve --token <tok>   # require a bearer token in every API call
```

The web UI is a self-hosted local player for your downloaded music: browse
by artist, album, year, or genre; stream any downloaded file (FLAC is
transcoded to Opus for browsers that can't play it); search across all
accounts; and trigger library refreshes / downloads — all without sending
anything off-machine. When the server runs with `--token`, the UI prompts
for the token on first load and remembers it.

API endpoints are prefixed `/api/`: `/api/health`, `/api/accounts`,
`/api/library`, `/api/most-played/<slug>`, `/api/media/<slug>/<video_id>/stream`,
`/api/art/<slug>/<video_id>.jpg`, `/api/events` (Server-Sent Events for live
download/refresh progress), plus POST handlers for refresh, downloads,
duplicate removal/export, settings, backup, and per-account unliked preview
and purge.

---

## First run: add an account

1. Launch the app (`./run.sh`) and press **+ Add Account** (or `a`).
2. Log into the YouTube Music account you want to sync in your browser.
3. Open **Developer Tools → Network**.
4. Find a `POST` request to `/browse` with status `200`.
5. Right-click it → **Copy → Copy request headers**.
6. Paste the complete request headers into the dialog and press **Test & Add**.

The headers (including your session cookies) are stored locally in
`accounts/<slug>/.headers_auth.json` with restrictive file permissions
(mode 0600). They are never logged or sent anywhere except YouTube Music.

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
- **Settings** (`p`) — change default quality / existing-file policy.
- **Remove** — delete an account and its local data.

Downloads stream to `files/<slug>/<Quality_Dir>/` as `.ytm_<video_id>.*`
temp files first, then are renamed to their final tagged filename.

---

## How it works

| Path | Purpose |
| --- | --- |
| `ytm-ll-dl-modern.py` | The entire application (~7800 lines, single file). |
| `install.sh` / `run.sh` | Setup and launcher scripts. |
| `requirements.txt` | Pinned Python dependencies. |
| `venv/` | Project-local Python environment + yt-dlp/Deno binaries (created by `install.sh`). |
| `config/settings.json` | Persistent settings (created on first run). |
| `accounts/<slug>/account.json` | Display metadata for an account. |
| `accounts/<slug>/.headers_auth.json` | Your pasted request headers (secret — mode 0600). |
| `files/<slug>/.inventory.json` | Authoritative per-track index (download state, quality, results). |
| `files/<slug>/.status.json` | Snapshot of the last refresh scan. |
| `files/<slug>/<Quality_Dir>/` | Downloaded music, e.g. `MP3_320kbps`, `FLAC`. |

The app writes every file atomically (temp file + rename), so an
interrupted run never leaves a half-written index behind.

---

## Troubleshooting

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
- Search-and-download across all connected accounts
- Your feature requests are welcome

## Notes

- This is your own music; downloading your liked library is personal use.
  Respect each service's terms of use.
- Licensed under the MIT License — see [LICENSE](LICENSE).
