"""
Background scheduler for the web UI.

Wakes every 30 seconds and runs due periodic tasks based on settings:

  - auto_refresh_interval_hours: trigger a library refresh per account
  - auto_download_new_likes:    after a refresh, download new tracks
  - premium_retry_interval_days: retry premium-locked tracks
  - backup_interval_hours:       write (and prune) library backups
  - lastfm_enabled:              flush queued scrobbles periodically
  - yt-dlp:                      check for updates once a day

Every action is broadcast on the event bus so the SPA can react.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import plays as plays_module
from .bus import EventBus

TICK_SECONDS = 30
YDLP_CHECK_SECONDS = 86400
SCROBBLE_FLUSH_SECONDS = 600
BACKUP_KEEP = 20


class Scheduler:
    def __init__(
        self,
        app: Any,
        bus: EventBus,
        downloads: Any,
        refresh: Any,
    ):
        self.app = app
        self.bus = bus
        self.downloads = downloads
        self.refresh = refresh

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._last_refresh: dict[str, float] = {}
        self._pending_auto_download: set[str] = set()
        self._last_premium_retry: dict[str, float] = {}
        self._last_backup = 0.0
        self._last_ytdlp_check = 0.0
        self._last_scrobble_flush = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="web-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        now = time.time()

        def _seconds_ago(value: float) -> int:
            return max(0, int(now - value)) if value else None

        return {
            "running": bool(
                self._thread and self._thread.is_alive()
            ),
            "last_refresh": {
                slug: _seconds_ago(value)
                for slug, value in self._last_refresh.items()
            },
            "last_premium_retry": {
                slug: _seconds_ago(value)
                for slug, value in self._last_premium_retry.items()
            },
            "last_backup": _seconds_ago(self._last_backup),
            "last_ytdlp_check": _seconds_ago(self._last_ytdlp_check),
            "last_scrobble_flush": _seconds_ago(self._last_scrobble_flush),
            "pending_auto_download": sorted(self._pending_auto_download),
        }

    def run_now(self, task: str) -> dict[str, Any]:
        """Manual trigger of a scheduled task, e.g. from the SPA."""
        if task == "premium_retry":
            return self._premium_retry()
        if task == "backup":
            return self._backup()
        if task == "ytdlp_check":
            return self._ytdlp_check()
        if task == "scrobble_flush":
            return self._scrobble_flush()
        return {"error": f"Unknown task: {task}"}

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self) -> None:
        now = time.time()
        settings = self.app.load_settings()

        self._auto_refresh(settings, now)
        self._auto_download_new_likes(settings)
        self._premium_retry_if_due(settings, now)
        self._backup_if_due(settings, now)
        self._ytdlp_check_if_due(now)
        self._scrobble_flush_if_due(settings, now)
        self._queue_auto_start(settings)

    # -- tasks -------------------------------------------------------------

    def _queue_auto_start(self, settings: dict[str, Any]) -> None:
        """When the persistent download queue has auto-start enabled,
        pick up the next batch for every account whose session slot has
        freed up. start_queued_batches itself skips busy accounts and
        stale items, so this is safe to call on every tick."""
        from . import queue as queue_mod

        try:
            data = queue_mod.load_queue(self.app)
        except Exception:
            return
        if not data.get("auto_start") or not data["items"]:
            return
        try:
            result = queue_mod.start_queued_batches(self.app, self.downloads)
        except Exception:
            return
        if result["started"]:
            self.bus.broadcast(
                {
                    "type": "queue_started",
                    "accounts": result["started"],
                    "queued_left": result["queued_left"],
                }
            )

    def _auto_refresh(self, settings: dict[str, Any], now: float) -> None:
        hours = int(settings.get("auto_refresh_interval_hours", 0) or 0)
        if hours <= 0:
            return

        for account in self.app.get_accounts():
            slug = account["slug"]
            last = self._last_refresh.get(slug, 0.0)
            if now - last < hours * 3600:
                continue
            if slug in self.refresh.active:
                continue
            if self.refresh.start(slug):
                self._last_refresh[slug] = now
                self._pending_auto_download.add(slug)

    def _auto_download_new_likes(self, settings: dict[str, Any]) -> None:
        if not settings.get("auto_download_new_likes"):
            self._pending_auto_download.clear()
            return

        for slug in list(self._pending_auto_download):
            if slug in self.refresh.active:
                continue
            self._pending_auto_download.discard(slug)

            account = self._account(slug)
            if account is None:
                continue
            if self.downloads.snapshot(slug):
                continue

            quality = str(
                settings.get("default_quality", "best")
            )

            try:
                tracks = self.app.repair_pending_tracks(
                    account,
                    quality,
                )
            except Exception:
                tracks = []

            if not tracks:
                continue

            try:
                sessions = self.app.build_sessions(
                    self.app.DownloadSession(
                        quality=quality,
                        existing_policy=str(
                            settings.get(
                                "default_existing_policy",
                                "skip",
                            )
                        ),
                        session="once",
                    )
                )
                self.downloads.start(
                    account,
                    tracks,
                    sessions,
                    settings,
                )
                self.bus.broadcast(
                    {
                        "type": "auto_download_started",
                        "slug": slug,
                        "count": len(tracks),
                    }
                )
            except Exception:
                pass

    def _premium_retry_if_due(
        self,
        settings: dict[str, Any],
        now: float,
    ) -> None:
        days = int(settings.get("premium_retry_interval_days", 7) or 0)
        if days <= 0:
            return

        for account in self.app.get_accounts():
            slug = account["slug"]
            last = self._last_premium_retry.get(slug, 0.0)
            if now - last < days * 86400:
                continue
            self._last_premium_retry[slug] = now
            if self.downloads.snapshot(slug):
                continue
            self._premium_retry_for(account)

    def _premium_retry(self) -> dict[str, Any]:
        results = {}
        for account in self.app.get_accounts():
            results[account["slug"]] = self._premium_retry_for(account)
        return {"results": results}

    def _premium_retry_for(self, account: dict[str, Any]) -> dict[str, Any]:
        slug = account["slug"]
        try:
            retry = self.app.premium_retry_tracks(account)
        except Exception as error:
            return {"started": False, "error": str(error)}

        tracks = [entry["track"] for entry in retry]
        if not tracks:
            return {"started": False, "count": 0}

        if self.downloads.snapshot(slug):
            return {"started": False, "error": "already downloading"}

        settings = self.app.load_settings()
        quality = str(settings.get("default_quality", "best"))
        sessions = self.app.build_sessions(
            self.app.DownloadSession(
                quality=quality,
                existing_policy="replace",
                session="once",
            )
        )
        self.downloads.start(
            account,
            tracks,
            sessions,
            settings,
        )
        self.bus.broadcast(
            {
                "type": "premium_retry_started",
                "slug": slug,
                "count": len(tracks),
            }
        )
        return {"started": True, "count": len(tracks)}

    def _backup_if_due(
        self,
        settings: dict[str, Any],
        now: float,
    ) -> None:
        hours = int(settings.get("backup_interval_hours", 0) or 0)
        if hours <= 0:
            return
        if now - self._last_backup < hours * 3600:
            return
        self._backup()

    def _backup(self) -> dict[str, Any]:
        try:
            path = self.app.write_backup_snapshot()
            pruned = self.prune_backups(BACKUP_KEEP)
            self._last_backup = time.time()
            self.bus.broadcast(
                {
                    "type": "backup_written",
                    "file": str(path),
                    "pruned": pruned,
                }
            )
            return {"file": str(path), "pruned": pruned}
        except Exception as error:
            return {"error": str(error)}

    def prune_backups(self, keep: int) -> int:
        """Keep the newest `keep` backup files in config/exports."""
        directory = self.app.export_directory()
        backups = sorted(
            (
                path
                for path in directory.glob("backup-*.json")
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in backups[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _ytdlp_check_if_due(self, now: float) -> None:
        if now - self._last_ytdlp_check < YDLP_CHECK_SECONDS:
            return
        self._ytdlp_check()

    def _ytdlp_check(self) -> dict[str, Any]:
        try:
            result = self.app.check_yt_dlp_update()
            self._last_ytdlp_check = time.time()
            self.bus.broadcast(
                {
                    "type": "ytdlp_update_check",
                    **result,
                }
            )
            return result
        except Exception as error:
            return {"error": str(error)}

    def _scrobble_flush_if_due(
        self,
        settings: dict[str, Any],
        now: float,
    ) -> None:
        if not settings.get("lastfm_enabled"):
            return
        if now - self._last_scrobble_flush < SCROBBLE_FLUSH_SECONDS:
            return
        self._scrobble_flush()

    def _scrobble_flush(self) -> dict[str, Any]:
        result = plays_module.flush_scrobbles(self.app)
        self._last_scrobble_flush = time.time()
        self.bus.broadcast(
            {
                "type": "scrobble_flush",
                **result,
            }
        )
        return result

    # -- helpers -----------------------------------------------------------

    def _account(self, slug: str) -> dict[str, Any] | None:
        for account in self.app.get_accounts():
            if account["slug"] == slug:
                return account
        return None
