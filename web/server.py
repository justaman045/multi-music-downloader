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

from . import downloads, metadata, refresh
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

        if head == "duplicates":
            self._api_duplicates()
            return

        if head == "media" and len(parts) >= 3:
            self._api_media(parts[1], parts[2])
            return

        if head == "art" and len(parts) >= 2:
            self._api_art(parts)
            return

        self._send_error(404, "Not found")

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

        self._send_error(404, "Not found")

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
        elif video_ids:
            tracks = [
                track
                for track in web.app.inventory_current_tracks(account)
                if track.get("videoId") in video_ids
            ]
            output_dir = None
        else:
            tracks = web.app.inventory_current_tracks(account)
            output_dir = None

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
    except (TypeError, ValueError):
        raise ValueError("Numeric settings must be integers")

    if not 1 <= attempts <= 20:
        raise ValueError("Retry attempts must be 1-20")
    if not 0 <= delay <= 300:
        raise ValueError("Retry delay must be 0-300 seconds")
    if not 1 <= limit <= 100:
        raise ValueError("Most Played count must be 1-100")


def run_server(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> None:
    server = Server((host, port), app, token=token)
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
