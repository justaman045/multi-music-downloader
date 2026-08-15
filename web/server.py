"""
HTTP server for the web UI (local Spotify player).

Serves the SPA from web/static/, the JSON API, Range-streamed audio,
on-the-fly FLAC transcoding, album art, and a Server-Sent Events
stream for live download/refresh progress.

Security: binds to 127.0.0.1 by default. Binding to a LAN address
requires --token, checked with a constant-time compare. The server
never exposes authentication headers or account internals.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import downloads, metadata, plays, playlists, refresh, scheduler
from .bus import EventBus

STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_SLUG_RE = re.compile(r"^[a-z0-9._-]{1,64}$")


class WebApp:
    """Shared state across requests."""

    def __init__(self, app: Any, token: str | None = None):
        self.app = app
        self.token = token
        self.bus = EventBus()
        self.downloads = downloads.get_manager(app, self.bus)
        self.refresh = refresh.RefreshManager(app, self.bus)
        self.scheduler = scheduler.Scheduler(
            app,
            self.bus,
            self.downloads,
            self.refresh,
        )
        self._library_lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def library(self) -> dict[str, Any]:
        with self._library_lock:
            return metadata.build_library(self.app)

    def account(self, slug: str) -> dict[str, Any] | None:
        if not _SLUG_RE.match(slug):
            return None
        for account in self.app.get_accounts():
            if account["slug"] == slug:
                return account
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        app: Any,
        token: str | None = None,
    ):
        self.web = WebApp(app, token=token)
        super().__init__(server_address, WebHandler)


def _content_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return CONTENT_TYPES.get(ext, "application/octet-stream")


class WebHandler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, *args):  # silence request logging
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _authorized(self) -> bool:
        token = self.server.web.token
        if token is None:
            return True

        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            candidate = header[7:]
        else:
            candidate = self.headers.get("X-Auth-Token", "")

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("token"):
            candidate = query["token"][0]

        if not candidate:
            return False

        return hmac.compare_digest(
            candidate.encode("utf-8"),
            token.encode("utf-8"),
        )

    def _paths(self) -> list[str]:
        parsed = urllib.parse.urlparse(self.path)
        raw = urllib.parse.unquote(parsed.path)
        parts = [p for p in raw.split("/") if p]
        return parts

    def _send_static(self, name: str) -> None:
        if ".." in name or name.startswith("/"):
            self._send_error(403, "Forbidden")
            return
        target = (STATIC_DIR / name).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()):
            self._send_error(403, "Forbidden")
            return
        if target.is_file():
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _content_type(name))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error(404, "Not found")

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        parts = self._paths()
        if parts and parts[0] == "api" and not self._authorized():
            self._send_error(401, "Unauthorized")
            return
        if not parts:
            self._send_static("index.html")
            return
        if parts[0] == "assets" and len(parts) >= 2:
            self._send_static("/".join(parts[1:]))
            return
        if parts[0] == "api":
            self._api_get(parts[1:])
            return
        self._send_static("index.html")

    def do_POST(self) -> None:
        parts = self._paths()
        if parts and parts[0] == "api":
            if not self._authorized():
                self._send_error(401, "Unauthorized")
                return
            self._api_post(parts[1:])
            return
        self._send_error(404, "Not found")

    def do_DELETE(self) -> None:
        parts = self._paths()
        if parts and parts[0] == "api":
            if not self._authorized():
                self._send_error(401, "Unauthorized")
                return
            self._api_delete(parts[1:])
            return
        self._send_error(404, "Not found")

    # -- GET API ------------------------------------------------------------

    def _api_get(self, parts: list[str]) -> None:
        web = self.server.web
        if not parts:
            self._send_error(404, "Not found")
            return

        head = parts[0]

        if head == "health":
            self._send_json({"ok": True})
            return

        if head == "library":
            self._send_json(web.library())
            return

        if head == "accounts":
            if len(parts) == 1:
                accounts = [
                    {
                        "slug": account["slug"],
                        "name": account.get("name", account["slug"]),
                    }
                    for account in web.app.get_accounts()
                ]
                self._send_json(accounts)
                return
            if len(parts) == 3 and parts[2] == "unliked":
                account = web.account(parts[1])
                if account is None:
                    self._send_error(404, "Unknown account")
                    return
                self._send_json(web.app.unliked_inventory_records(account))
                return
            self._send_error(404, "Not found")
            return

        if head == "settings":
            self._send_json(web.app.load_settings())
            return

        if head == "downloads":
            self._send_json(web.downloads.snapshot())
            return

        if head == "refresh":
            self._send_json({"active": web.refresh.status()})
            return

        if head == "events":
            self._stream_events()
            return

        if head == "most-played" and len(parts) >= 2:
            self._api_most_played(parts[1])
            return

        if head == "status" and len(parts) >= 2:
            self._api_status(parts[1])
            return

        if head == "duplicates" and len(parts) == 1:
            self._api_duplicates()
            return

        if head == "media" and len(parts) >= 3:
            self._api_media(parts[1], parts[2])
            return

        if head == "art" and len(parts) >= 2:
            self._api_art(parts)
            return

        if head == "playlists":
            self._api_playlists(parts[1:])
            return

        if head == "stats":
            self._api_stats(parts)
            return

        if head == "history":
            self._api_history(parts)
            return

        if head == "duplicates" and len(parts) == 2 and parts[1] == "within":
            self._api_within_duplicates()
            return

        if head == "upgrade-preview" and len(parts) >= 2:
            self._api_upgrade_preview(parts[1])
            return

        if head == "yt-dlp-update":
            self._api_ytdlp()
            return

        if head == "auth-health" and len(parts) >= 2:
            self._api_auth_health(parts[1])
            return

        if head == "backups":
            self._api_backups()
            return

        if head == "search":
            self._api_search()
            return

        if head == "lastfm":
            self._api_lastfm()
            return

        if head == "scheduler":
            self._send_json(web.scheduler.status())
            return

        self._send_error(404, "Not found")

    # -- new GET handlers --------------------------------------------------

    def _api_playlists(self, parts: list[str]) -> None:
        web = self.server.web

        if not parts:
            custom = [
                playlists.playlist_summary(web.app, playlist)
                for playlist in playlists.load_custom_playlists(web.app)["playlists"]
            ]

            ytm = {}
            for account in web.app.get_accounts():
                snapshot = web.app.load_playlist_snapshot(account)
                if snapshot is None or not snapshot.get("online"):
                    continue
                ytm[account["slug"]] = snapshot.get("playlists", [])

            self._send_json(
                {
                    "custom": custom,
                    "ytm": ytm,
                }
            )
            return

        if len(parts) >= 3 and parts[0] == "ytm":
            self._api_ytm_playlist(parts[1], parts[2])
            return

        if len(parts) == 1:
            playlist = playlists.get_playlist(web.app, parts[0])
            if playlist is None:
                self._send_error(404, "Unknown playlist")
                return
            self._send_json(
                {
                    **playlist,
                    "tracks": playlists.resolve_playlist_tracks(web.app, playlist),
                }
            )
            return

        self._send_error(404, "Not found")

    def _api_ytm_playlist(self, slug: str, playlist_id: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return
        snapshot = web.app.load_playlist_snapshot(account)
        if snapshot is None or not snapshot.get("online"):
            self._send_error(404, "Synced playlists unavailable offline")
            return
        for playlist in snapshot.get("playlists", []):
            if playlist.get("id") == playlist_id:
                self._send_json(
                    {
                        "id": playlist_id,
                        "name": playlist.get("title"),
                        "kind": "ytm",
                        "account": slug,
                        "tracks": playlist.get("tracks", []),
                        "online": True,
                    }
                )
                return
        self._send_error(404, "Unknown playlist")

    def _api_stats(self, parts: list[str]) -> None:
        web = self.server.web
        slug = parts[1] if len(parts) >= 2 else None
        if slug and web.account(slug) is None:
            self._send_error(404, "Unknown account")
            return
        self._send_json(plays.play_stats(web.app, slug))

    def _api_history(self, parts: list[str]) -> None:
        web = self.server.web
        slug = parts[1] if len(parts) >= 2 else None
        if slug and web.account(slug) is None:
            self._send_error(404, "Unknown account")
            return
        days = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("days", ["30"])[0]
        try:
            days = int(days)
        except ValueError:
            self._send_error(400, "days must be an integer")
            return
        self._send_json(plays.history_stats(web.app, slug, days))

    def _api_within_duplicates(self) -> None:
        web = self.server.web
        result = []
        for account in web.app.get_accounts():
            try:
                result.append(
                    {
                        "slug": account["slug"],
                        "name": account.get("name", account["slug"]),
                        "groups": web.app.within_account_duplicates(account),
                    }
                )
            except Exception as error:
                result.append(
                    {
                        "slug": account["slug"],
                        "error": str(error),
                    }
                )
        self._send_json(result)

    def _api_upgrade_preview(self, slug: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return
        quality = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("quality", ["best"])[0]
        if quality not in {
            value for _label, value in web.app.QUALITY_OPTIONS
        }:
            self._send_error(400, f"Unknown quality: {quality}")
            return
        tracks = web.app.upgrade_quality_tracks(account, quality)
        self._send_json(
            {
                "slug": slug,
                "quality": quality,
                "tracks": tracks,
                "count": len(tracks),
            }
        )

    def _api_ytdlp(self) -> None:
        web = self.server.web
        self._send_json(web.app.check_yt_dlp_update())

    def _api_auth_health(self, slug: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return
        self._send_json(web.app.load_auth_health(account))

    def _api_backups(self) -> None:
        web = self.server.web
        directory = web.app.export_directory()
        files = []
        for path in sorted(
            directory.glob("backup-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            try:
                files.append(
                    {
                        "name": path.name,
                        "size_bytes": path.stat().st_size,
                        "created_at": int(path.stat().st_mtime),
                    }
                )
            except OSError:
                continue
        self._send_json(files)

    def _api_search(self) -> None:
        web = self.server.web
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        query = params.get("q", [""])[0]
        limit = params.get("limit", ["10"])[0]
        if not query.strip():
            self._send_error(400, "Missing query parameter q")
            return
        try:
            limit_n = int(limit)
        except ValueError:
            limit_n = 10

        accounts_param = params.get("accounts", [""])[0]
        targets = [
            slug
            for slug in accounts_param.split(",")
            if slug.strip()
        ] if accounts_param else []

        if targets:
            accounts_by_slug = {
                account["slug"]: account
                for account in web.app.get_accounts()
            }
            unknown = [
                slug for slug in targets if slug not in accounts_by_slug
            ]
            if unknown:
                self._send_error(404, f"Unknown accounts: {', '.join(unknown)}")
                return

            owned = self._search_owned_ids()
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            errors: list[str] = []

            for slug in targets:
                try:
                    found = web.app.search_tracks(
                        query.strip(),
                        limit=limit_n,
                        account=accounts_by_slug[slug],
                    )
                except Exception as error:
                    errors.append(f"{slug}: {error}")
                    continue

                for item in found:
                    video_id = item.get("videoId")
                    if not video_id:
                        continue
                    if video_id in seen:
                        continue
                    seen.add(video_id)
                    results.append(
                        {
                            **item,
                            "accounts": [slug],
                            "owned_by": [
                                owner
                                for owner in owned.get(video_id, [])
                            ],
                        }
                    )

            self._send_json(
                {
                    "query": query.strip(),
                    "accounts": targets,
                    "errors": errors,
                    "results": results,
                }
            )
            return

        try:
            results = web.app.search_tracks(
                query.strip(),
                limit=limit_n,
            )
        except Exception as error:
            self._send_error(502, f"Search failed: {error}")
            return
        self._send_json(results)

    def _search_owned_ids(self) -> dict[str, list[str]]:
        """video_id -> list of account slugs whose library has it."""
        web = self.server.web
        owned: dict[str, list[str]] = {}
        for account in web.app.get_accounts():
            try:
                inventory = web.app.load_inventory(account)
            except Exception:
                continue
            for video_id, record in (inventory["tracks"] or {}).items():
                qualities = record.get("qualities") or {}
                if any(
                    entry.get("status") == "downloaded"
                    for entry in qualities.values()
                ):
                    owned.setdefault(video_id, []).append(account["slug"])
        return owned

    def _api_lastfm(self) -> None:
        self._send_json(plays.lastfm_status(self.server.web.app))

    def _api_most_played(self, slug: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return

        fresh = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("fresh") == ["1"]

        report_path = (
            Path(account["output"]) / "Most_Played" / ".most_played.json"
        )
        data = None

        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                data = None

        if fresh or data is None:
            try:
                ranked, plays = web.app.fetch_most_played(account)
                web.app.save_most_played_list(account, ranked, plays)
                if report_path.exists():
                    data = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as error:
                if data is None:
                    self._send_error(502, f"Most Played fetch failed: {error}")
                    return

        self._send_json(
            {
                "account": slug,
                "report": data or {
                    "version": 1,
                    "timestamp": 0,
                    "tracks": [],
                },
            }
        )

    def _api_status(self, slug: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return

        status_path = Path(account["output"]) / ".status.json"
        if status_path.exists():
            try:
                self._send_json(
                    json.loads(status_path.read_text(encoding="utf-8"))
                )
                return
            except Exception:
                pass
        self._send_json(
            {
                "version": 1,
                "timestamp": 0,
                "counts": {},
            }
        )

    def _api_duplicates(self) -> None:
        web = self.server.web
        accounts = web.app.get_accounts()
        try:
            report = web.app.find_cross_account_duplicates(accounts)
            candidates = web.app.duplicate_removal_candidates(
                accounts,
                report,
            )
            self._send_json(
                {
                    "report": report,
                    "candidates": candidates,
                }
            )
        except Exception as error:
            self._send_error(500, f"Duplicate scan failed: {error}")

    def _api_media(self, slug: str, video_id: str) -> None:
        web = self.server.web
        account = web.account(slug)
        if account is None or not _VIDEO_ID_RE.match(video_id):
            self._send_error(404, "Unknown track")
            return

        path = metadata.best_file_for(web.app, account, video_id)
        if path is None:
            self._send_error(404, "No downloaded file for this track")
            return

        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        transcode = query.get("format", [""])[0]

        if transcode in {"opus", "ogg"} and path.suffix.lower() in {
            ".flac",
            ".aiff",
            ".wav",
        }:
            self._stream_transcode(path, "opus")
            return

        if query.get("download") == ["1"]:
            self.send_response(200)
            self.send_header("Content-Type", _content_type(path.name))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"',
            )
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as fh:
                self._copy(fh, None)
            return

        self._stream_file(path)

    def _stream_file(self, path: Path) -> None:
        size = path.stat().st_size
        range_header = self.headers.get("Range")

        if range_header:
            match = re.match(
                r"bytes=(\d*)-(\d*)",
                range_header.strip(),
            )
            if match:
                start_s, end_s = match.groups()
                start = int(start_s) if start_s else 0
                end = (
                    min(int(end_s), size - 1)
                    if end_s
                    else size - 1
                )
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                length = end - start + 1
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", _content_type(path.name))
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{size}",
                )
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with path.open("rb") as fh:
                    fh.seek(start)
                    self._copy(fh, length)
                return

        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", _content_type(path.name))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as fh:
            self._copy(fh, None)

    def _stream_transcode(self, path: Path, target: str) -> None:
        exe = _ffmpeg_exe()
        if exe is None:
            self._send_error(500, "FFmpeg not available")
            return

        if target == "opus":
            args = [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-f",
                "opus",
                "-c:a",
                "libopus",
                "-b:a",
                "160k",
                "pipe:1",
            ]
            content_type = "audio/ogg; codecs=opus"
        else:
            self._send_error(400, "Unsupported transcode target")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:
            self._send_error(500, f"Transcode failed to start: {error}")
            return

        assert process.stdout is not None
        self._copy(process.stdout, None)
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()

    def _copy(self, source, length: int | None) -> None:
        try:
            remaining = length
            while True:
                chunk_size = (
                    min(65536, remaining)
                    if remaining is not None
                    else 65536
                )
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _api_art(self, parts: list[str]) -> None:
        web = self.server.web
        video_part = parts[-1]
        if not video_part.endswith(".jpg"):
            self._send_error(404, "Not found")
            return
        video_id = video_part[:-4]
        slug = parts[1]

        account = web.account(slug)
        if account is None or not _VIDEO_ID_RE.match(video_id):
            self._send_error(404, "Unknown track")
            return

        art = metadata.ensure_art(web.app, account, video_id)
        if art is None or not art.exists():
            self._send_error(404, "No artwork")
            return

        body = art.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    # -- POST API -----------------------------------------------------------

    def _api_post(self, parts: list[str]) -> None:
        web = self.server.web
        if not parts:
            self._send_error(404, "Not found")
            return

        head = parts[0]
        body = self._body()

        if head == "accounts" and len(parts) == 1:
            name = str(body.get("name", "")).strip()
            headers_raw = str(body.get("headers", ""))
            if not name or not headers_raw:
                self._send_error(400, "Name and headers are required")
                return
            try:
                account = web.app.create_account(name, headers_raw)
                self._send_json(
                    {
                        "slug": account["slug"],
                        "name": account.get("name", name),
                    },
                    status=201,
                )
            except Exception as error:
                self._send_error(400, str(error))
            return

        if head == "accounts" and len(parts) == 3 and parts[2] == "refresh":
            if not web.refresh.start(parts[1]):
                self._send_error(409, "Refresh already running")
                return
            self._send_json({"started": True}, status=202)
            return

        if head == "accounts" and len(parts) == 3 and parts[2] == "unliked":
            account = web.account(parts[1])
            if account is None:
                self._send_error(404, "Unknown account")
                return
            video_ids = body.get("video_ids") or []
            if not video_ids:
                self._send_error(400, "No video ids given")
                return
            result = web.app.purge_unliked_records(account, video_ids)
            self._send_json(result)
            return

        if head == "settings" and len(parts) == 1:
            settings = dict(web.app.load_settings())
            for key in (
                "default_quality",
                "default_existing_policy",
                "default_session",
                "default_limit",
                "retry_attempts",
                "retry_delay",
                "auto_refresh_interval_hours",
                "auto_download_new_likes",
                "premium_retry_interval_days",
                "max_parallel_downloads",
                "download_rate_limit",
                "backup_interval_hours",
                "notifications_enabled",
                "lastfm_enabled",
                "lastfm_api_key",
                "lastfm_api_secret",
                "lastfm_username",
                "lastfm_password_md5",
            ):
                if key in body:
                    settings[key] = body[key]
            try:
                _validate_settings(settings)
            except ValueError as error:
                self._send_error(400, str(error))
                return
            web.app.save_settings(settings)
            self._send_json(settings)
            return

        if head == "backup" and len(parts) == 1:
            try:
                path = web.app.write_backup_snapshot()
                self._send_json({"file": str(path)})
            except Exception as error:
                self._send_error(500, str(error))
            return

        if head == "downloads" and len(parts) == 1:
            self._start_download(body)
            return

        if head == "downloads" and len(parts) == 2 and parts[1] == "cancel-all":
            web.downloads.cancel_all()
            self._send_json({"cancelled": True})
            return

        if (
            head == "downloads"
            and len(parts) == 3
            and parts[2] in {"stop", "cancel"}
        ):
            slug = parts[1]
            ok = (
                web.downloads.stop_after_stage(slug)
                if parts[2] == "stop"
                else web.downloads.cancel(slug)
            )
            if not ok:
                self._send_error(404, "No active download for this account")
                return
            self._send_json({"ok": True})
            return

        if head == "duplicates" and len(parts) == 2 and parts[1] == "export":
            try:
                accounts = web.app.get_accounts()
                report = web.app.find_cross_account_duplicates(accounts)
                candidates = web.app.duplicate_removal_candidates(
                    accounts,
                    report,
                )
                path = web.app.export_duplicates_report(
                    {
                        "report": report,
                        "candidates": candidates,
                    }
                )
                self._send_json({"file": str(path)})
            except Exception as error:
                self._send_error(500, f"Duplicate export failed: {error}")
            return

        if head == "duplicates" and len(parts) == 2 and parts[1] == "remove":
            self._remove_duplicates(body)
            return

        if head == "playlists" and len(parts) == 1:
            self._create_playlist(body)
            return

        if head == "playlists" and len(parts) == 2 and parts[1] == "sync":
            self._sync_playlists(body)
            return

        if head == "playlists" and len(parts) == 2 and parts[1] == "import":
            self._import_playlist(body)
            return

        if head == "playlists" and len(parts) == 3:
            self._playlist_action(parts[1], parts[2], body)
            return

        if head == "plays" and len(parts) == 1:
            self._record_play(body)
            return

        if head == "lastfm" and len(parts) == 2 and parts[1] == "flush":
            self._send_json(plays.flush_scrobbles(web.app))
            return

        if head == "tags" and len(parts) == 1:
            self._write_tags(body)
            return

        if head == "verify" and len(parts) == 1:
            self._verify_files(body)
            return

        if (
            head == "duplicates"
            and len(parts) == 3
            and parts[1] == "within"
            and parts[2] == "remove"
        ):
            self._remove_within_duplicates(body)
            return

        if head == "yt-dlp-update" and len(parts) == 1:
            self._send_json(web.app.update_yt_dlp())
            return

        if head == "auth-health" and len(parts) == 2:
            account = web.account(parts[1])
            if account is None:
                self._send_error(404, "Unknown account")
                return
            self._send_json(web.app.probe_auth(account))
            return

        if head == "backups" and len(parts) == 2 and parts[1] == "prune":
            keep = int(body.get("keep", 20) or 20)
            self._send_json({"pruned": web.scheduler.prune_backups(keep)})
            return

        if head == "scheduler" and len(parts) == 2:
            self._send_json(web.scheduler.run_now(parts[1]))
            return

        self._send_error(404, "Not found")

    # -- new POST handlers -------------------------------------------------

    def _create_playlist(self, body: dict[str, Any]) -> None:
        web = self.server.web
        name = str(body.get("name", "")).strip()
        if not name:
            self._send_error(400, "Playlist name is required")
            return
        kind = str(body.get("kind", "custom"))
        account = body.get("account")
        if account and web.account(account) is None:
            self._send_error(404, "Unknown account")
            return
        try:
            playlist = playlists.create_playlist(
                web.app,
                name,
                kind=kind,
                account=account or None,
                rules=body.get("rules"),
            )
            self._send_json(playlist, status=201)
        except ValueError as error:
            self._send_error(400, str(error))

    def _sync_playlists(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = body.get("slug")
        accounts = (
            [web.account(slug)]
            if slug
            else web.app.get_accounts()
        )
        accounts = [account for account in accounts if account is not None]
        if not accounts:
            self._send_error(404, "Unknown account")
            return

        results = {}
        for account in accounts:
            try:
                results[account["slug"]] = web.app.sync_library_playlists(account)
            except Exception as error:
                results[account["slug"]] = {
                    "online": False,
                    "error": str(error),
                }

        self._send_json(results)

    def _import_playlist(self, body: dict[str, Any]) -> None:
        web = self.server.web
        import_type = str(body.get("type", "json"))
        name = str(body.get("name", "")).strip() or "Imported playlist"

        try:
            if import_type == "json":
                playlist = playlists.import_playlist_json(
                    web.app,
                    body.get("content") or {},
                )
            elif import_type == "m3u":
                playlist = playlists.import_playlist_m3u(
                    web.app,
                    str(body.get("content") or ""),
                    name,
                )
            elif import_type == "spotify":
                playlist = self._import_spotify(body)
            else:
                self._send_error(400, f"Unknown import type: {import_type}")
                return
        except ValueError as error:
            self._send_error(400, str(error))
            return
        except Exception as error:
            self._send_error(502, str(error))
            return

        self._send_json(playlist, status=201)

    def _import_spotify(self, body: dict[str, Any]) -> dict[str, Any]:
        web = self.server.web
        url = str(body.get("url", ""))
        account = web.account(str(body.get("account") or ""))
        limit = int(body.get("limit", 50) or 50)

        data = web.app.import_spotify_playlist(url, limit=limit)
        matched = web.app.map_spotify_tracks(
            data["tracks"],
            account=account,
            limit=limit,
        )

        name = str(body.get("name") or data["title"]).strip() or "Spotify playlist"
        playlist = playlists.create_playlist(
            web.app,
            name,
            kind="custom",
            account=account["slug"] if account else None,
        )
        if matched:
            playlists.add_tracks(
                web.app,
                playlist["id"],
                [
                    {
                        "video_id": track["videoId"],
                        "account": account["slug"] if account else None,
                    }
                    for track in matched
                ],
            )

        return {
            **playlist,
            "matched": matched,
            "unmatched": len(data["tracks"]) - len(matched),
        }

    def _playlist_action(
        self,
        playlist_id: str,
        action: str,
        body: dict[str, Any],
    ) -> None:
        web = self.server.web
        try:
            if action == "update":
                result = playlists.update_playlist(
                    web.app,
                    playlist_id,
                    name=body.get("name"),
                    rules=body.get("rules"),
                )
                self._send_json(result)
                return
            if action == "add":
                entries = body.get("tracks") or []
                result = playlists.add_tracks(web.app, playlist_id, entries)
                self._send_json(result)
                return
            if action == "remove":
                entries = body.get("tracks") or []
                result = playlists.remove_tracks(web.app, playlist_id, entries)
                self._send_json(result)
                return
            if action == "export":
                playlist = playlists.get_playlist(web.app, playlist_id)
                if playlist is None:
                    self._send_error(404, "Unknown playlist")
                    return
                fmt = str(body.get("format", "m3u"))
                if fmt not in {"m3u", "json", "csv"}:
                    self._send_error(400, "Format must be m3u, json or csv")
                    return
                path = playlists.export_playlist(web.app, playlist, fmt)
                self._send_json({"file": str(path)})
                return
        except (ValueError, KeyError) as error:
            self._send_error(400, str(error))
            return
        self._send_error(404, "Not found")

    def _record_play(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = str(body.get("slug", ""))
        video_id = str(body.get("video_id", ""))
        try:
            progress = float(body.get("progress", 0))
        except (TypeError, ValueError):
            progress = 0.0

        if web.account(slug) is None:
            self._send_error(404, "Unknown account")
            return
        if not video_id or not _VIDEO_ID_RE.match(video_id):
            self._send_error(400, "Invalid video id")
            return

        result = plays.record_play(web.app, slug, video_id, progress)

        # Live-update connected SPA clients.
        web.bus.broadcast(
            {
                "type": "play_recorded",
                "slug": slug,
                "video_id": video_id,
                "counted": result.get("counted", False),
                "total": result.get("total", 0),
            }
        )

        # Scrobble counted plays to last.fm (best-effort, async).
        if result.get("counted") and web.app.load_settings().get("lastfm_enabled"):
            try:
                plays.enqueue_scrobble(web.app, slug, video_id)
            except Exception:
                pass

        self._send_json(result)

    def _write_tags(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = str(body.get("slug", ""))
        video_id = str(body.get("video_id", ""))
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return
        if not video_id or not _VIDEO_ID_RE.match(video_id):
            self._send_error(400, "Invalid video id")
            return

        tags = body.get("tags") or {}
        try:
            updated = self._apply_tags(account, video_id, tags)
        except Exception as error:
            self._send_error(500, str(error))
            return

        self._send_json(updated)

    def _apply_tags(
        self,
        account: dict[str, Any],
        video_id: str,
        tags: dict[str, Any],
    ) -> dict[str, Any]:
        """Write title/artist/album/genre/year tags to every downloaded
        file for the track via mutagen."""
        import mutagen
        import mutagen.flac
        import mutagen.id3
        import mutagen.mp3
        import mutagen.mp4

        web = self.server.web
        inventory = web.app.load_inventory(account)
        record = inventory["tracks"].get(video_id)
        if record is None:
            raise ValueError("Unknown track")

        files = [
            Path(entry["file"])
            for entry in (record.get("qualities") or {}).values()
            if entry.get("status") == "downloaded" and entry.get("file")
        ]
        files = [path for path in files if path.exists()]
        if not files:
            raise ValueError("No downloaded files for this track")

        title = tags.get("title", record.get("title"))
        artist = tags.get("artist", record.get("artist"))
        album = tags.get("album", record.get("album"))
        genre = tags.get("genre") or None
        year = tags.get("year") or None

        for path in files:
            audio = mutagen.File(str(path))
            if audio is None:
                continue
            module = type(audio).__module__

            try:
                if "mp4" in module:
                    audio.tags["\xa9nam"] = title
                    audio.tags["\xa9ART"] = artist
                    audio.tags["\xa9alb"] = album
                    if genre:
                        audio.tags["\xa9gen"] = genre
                    if year:
                        audio.tags["\xa9day"] = str(year)
                elif "flac" in module:
                    audio.tags["title"] = title
                    audio.tags["artist"] = artist
                    audio.tags["album"] = album
                    if genre:
                        audio.tags["genre"] = genre
                    if year:
                        audio.tags["date"] = str(year)
                elif "id3" in module or "mp3" in module:
                    tags_obj = audio.tags or mutagen.id3.ID3(str(path))
                    tags_obj["TIT2"] = mutagen.id3.TIT2(encoding=3, text=title)
                    tags_obj["TPE1"] = mutagen.id3.TPE1(encoding=3, text=artist)
                    tags_obj["TALB"] = mutagen.id3.TALB(encoding=3, text=album)
                    if genre:
                        tags_obj["TCON"] = mutagen.id3.TCON(encoding=3, text=genre)
                    if year:
                        tags_obj["TDRC"] = mutagen.id3.TDRC(encoding=3, text=str(year))
                    audio.tags = tags_obj
                else:
                    continue
                audio.save()
            except Exception as error:
                raise ValueError(f"Could not tag {path.name}: {error}")

        # Update the inventory copy of the metadata.
        record["title"] = title
        record["artist"] = artist
        record["album"] = album
        web.app.save_inventory(account, inventory)

        return {
            "video_id": video_id,
            "title": title,
            "artist": artist,
            "album": album,
            "files": [str(path) for path in files],
        }

    def _remove_within_duplicates(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = str(body.get("slug", ""))
        video_ids = body.get("video_ids") or []
        if not video_ids:
            self._send_error(400, "No video ids given")
            return

        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return

        removed = []
        for video_id in video_ids:
            try:
                web.app.remove_duplicates_from_account(
                    account,
                    [video_id],
                )
                removed.append(video_id)
            except Exception:
                pass

        self._send_json({"removed": removed})

    def _verify_files(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = str(body.get("slug", ""))
        account = web.account(slug)
        if account is None:
            self._send_error(404, "Unknown account")
            return

        try:
            report = web.app.verify_account_files(account)
        except Exception as error:
            self._send_error(500, f"Verification failed: {error}")
            return

        self._send_json(report)

    def _start_download(self, body: dict[str, Any]) -> None:
        web = self.server.web
        slug = str(body.get("account", ""))
        account = web.account(slug)
        if account is None:
            self._send_error(400, "Unknown account")
            return

        quality = str(body.get("quality", "best"))
        policy = str(body.get("existing_policy", "skip"))
        output = str(body.get("output", "library"))
        count = int(body.get("count", 0) or 0)
        video_ids = body.get("video_ids") or []

        settings = web.app.load_settings()

        mode = str(body.get("session", ""))
        if not mode:
            mode = str(settings.get("default_session", "once"))

        quality_values = [
            value for _label, value in web.app.QUALITY_OPTIONS
        ]
        if quality not in quality_values:
            self._send_error(400, f"Unknown quality: {quality}")
            return
        if policy not in {"skip", "replace"}:
            self._send_error(400, f"Unknown policy: {policy}")
            return
        if mode not in {"once", "loop", "skip"}:
            self._send_error(400, f"Unknown session mode: {mode}")
            return

        if output == "repair":
            tracks = web.app.repair_pending_tracks(account, quality)
            output_dir = None
            source = "library"
        elif output == "most_played":
            ranked, plays = self._most_played_tracks(account, count)
            tracks = [
                {
                    "videoId": track.get("videoId"),
                    "title": track.get("title"),
                    "artists": track.get("artists", []),
                    "album": track.get("album"),
                }
                for track in ranked
                if track.get("videoId")
            ]
            output_dir = Path(account["output"]) / "Most_Played"
            source = "library"
        elif output == "search":
            tracks = body.get("tracks") or []
            tracks = [
                track
                for track in tracks
                if track.get("videoId")
            ]
            output_dir = None
            source = "manual"
        elif output == "playlist":
            playlist_id = str(body.get("playlist_id", ""))
            if not playlist_id:
                self._send_error(400, "Missing playlist_id")
                return
            try:
                tracks = web.app.fetch_playlist_tracks(
                    account,
                    playlist_id,
                )
            except Exception as error:
                self._send_error(502, f"Playlist fetch failed: {error}")
                return
            output_dir = None
            source = "manual"
        elif output == "upgrade":
            policy = "replace"
            tracks = web.app.upgrade_quality_tracks(
                account,
                quality,
            )
            output_dir = None
            source = "library"
        elif video_ids:
            tracks = [
                track
                for track in web.app.inventory_current_tracks(account)
                if track.get("videoId") in video_ids
            ]
            output_dir = None
            source = "library"
        else:
            tracks = web.app.inventory_current_tracks(account)
            output_dir = None
            source = "library"

        if not tracks:
            self._send_error(400, "No tracks to download")
            return

        sessions = web.app.build_sessions(
            web.app.DownloadSession(
                quality=quality,
                existing_policy=policy,
                session=mode,
            )
        )

        snapshot = web.downloads.start(
            account,
            tracks,
            sessions,
            settings,
            output_dir=output_dir,
            source=source,
        )
        self._send_json(snapshot, status=202)

    def _most_played_tracks(
        self,
        account: dict[str, Any],
        count: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        report_path = (
            Path(account["output"]) / "Most_Played" / ".most_played.json"
        )
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                tracks = data.get("tracks", [])
                plays = {
                    str(entry.get("video_id")): int(entry.get("plays", 0))
                    for entry in tracks
                }
                ranked = [
                    {
                        "videoId": entry.get("video_id"),
                        "title": entry.get("title"),
                        "artists": [
                            {"name": entry.get("artist")}
                        ],
                        "album": None,
                    }
                    for entry in tracks
                ]
                if count and count > 0:
                    ranked = ranked[:count]
                return ranked, plays
            except Exception:
                pass

        ranked, plays = self.server.web.app.fetch_most_played(account)
        if count and count > 0:
            ranked = ranked[:count]
        return ranked, plays

    def _remove_duplicates(self, body: dict[str, Any]) -> None:
        web = self.server.web
        candidates = body.get("candidates", [])
        if not isinstance(candidates, list) or not candidates:
            self._send_error(400, "No candidates given")
            return

        by_slug: dict[str, list[str]] = {}
        for candidate in candidates:
            slug = candidate.get("slug")
            video_id = candidate.get("video_id")
            if not slug or not video_id:
                continue
            by_slug.setdefault(slug, []).append(video_id)

        results = []
        for slug, video_ids in by_slug.items():
            account = web.account(slug)
            if account is None:
                continue
            try:
                result = web.app.remove_duplicates_from_account(
                    account,
                    video_ids,
                )
                results.append(
                    {
                        "slug": slug,
                        **result,
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "slug": slug,
                        "error": str(error),
                    }
                )

        self._send_json({"results": results})

    # -- DELETE API ---------------------------------------------------------

    def _api_delete(self, parts: list[str]) -> None:
        web = self.server.web
        if parts and parts[0] == "accounts" and len(parts) == 2:
            slug = parts[1]
            if web.account(slug) is None:
                self._send_error(404, "Unknown account")
                return
            web.app.delete_account(slug)
            self._send_json({"deleted": slug})
            return
        if parts and parts[0] == "playlists" and len(parts) == 2:
            if playlists.delete_playlist(web.app, parts[1]):
                self._send_json({"deleted": parts[1]})
            else:
                self._send_error(404, "Unknown playlist")
            return
        self._send_error(404, "Not found")

    # -- SSE ----------------------------------------------------------------

    def _stream_events(self) -> None:
        web = self.server.web
        queue_ = web.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            self.wfile.write(
                b"data: "
                + json.dumps(
                    {
                        "type": "hello",
                        "downloads": web.downloads.snapshot(),
                        "refreshing": web.refresh.status(),
                    }
                ).encode("utf-8")
                + b"\n\n"
            )
            self.wfile.flush()

            last_heartbeat = time.time()
            while True:
                try:
                    payload = queue_.get(timeout=5)
                except Exception:
                    if time.time() - last_heartbeat >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.time()
                    continue
                if payload is None:
                    break
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            web.bus.unsubscribe(queue_)


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _validate_settings(settings: dict[str, Any]) -> None:
    try:
        attempts = int(settings.get("retry_attempts", 3))
        delay = int(settings.get("retry_delay", 3))
        limit = int(settings.get("default_limit", 25))
        auto_refresh = int(settings.get("auto_refresh_interval_hours", 0) or 0)
        premium_retry = int(settings.get("premium_retry_interval_days", 7) or 0)
        parallel = int(settings.get("max_parallel_downloads", 1) or 1)
        backup_interval = int(settings.get("backup_interval_hours", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("Numeric settings must be integers")

    rate_limit = str(settings.get("download_rate_limit", "") or "").strip()
    if rate_limit and not re.match(r"^\d+(\.\d+)?[KMG]?$", rate_limit):
        raise ValueError("Rate limit must be empty or like 500K, 1M, 5M")

    if not 1 <= attempts <= 20:
        raise ValueError("Retry attempts must be 1-20")
    if not 0 <= delay <= 300:
        raise ValueError("Retry delay must be 0-300 seconds")
    if not 1 <= limit <= 100:
        raise ValueError("Most Played count must be 1-100")
    if not 0 <= auto_refresh <= 168:
        raise ValueError("Auto-refresh interval must be 0-168 hours")
    if not 0 <= premium_retry <= 90:
        raise ValueError("Premium retry interval must be 0-90 days")
    if not 1 <= parallel <= 8:
        raise ValueError("Max parallel downloads must be 1-8")
    if not 0 <= backup_interval <= 336:
        raise ValueError("Backup interval must be 0-336 hours")


def run_server(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> None:
    server = Server((host, port), app, token=token)
    server.web.scheduler.start()
    print("=" * 56)
    print(" YT Music Downloader — Web UI")
    print("=" * 56)
    print()
    print(f"  Local:   http://{host}:{port}/")
    if token:
        print(f"  Token:   {token}")
    print()
    print("  Press Ctrl+C to stop.")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Stopped.")
