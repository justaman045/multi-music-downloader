"""Background library refresh for the web UI.

Mirrors the TUI's refresh pipeline (fetch liked tracks -> update
inventory -> scan -> status snapshot) so it can run from the browser.
"""

from __future__ import annotations

import threading
from typing import Any

from .bus import EventBus


class RefreshManager:
    def __init__(self, app: Any, bus: EventBus):
        self.app = app
        self.bus = bus
        self._lock = threading.Lock()
        self.active: set[str] = set()

    def start(self, slug: str) -> bool:
        with self._lock:
            if slug in self.active:
                return False
            self.active.add(slug)

        account = self._account(slug)
        if account is None:
            with self._lock:
                self.active.discard(slug)
            return False

        def run() -> None:
            try:
                quality = self.app.load_settings().get(
                    "default_quality",
                    "best",
                )
                tracks = self.app.fetch_all_liked_tracks(account)
                self.app.update_inventory_from_library(account, tracks)
                report = self.app.scan_inventory(
                    account,
                    tracks,
                    quality,
                )
                self.app.save_status_snapshot(account, quality, report)
                self.bus.broadcast(
                    {
                        "type": "refresh_finished",
                        "slug": slug,
                        "liked_count": report.get("liked_total", len(tracks)),
                    }
                )
            except Exception as error:
                self.bus.broadcast(
                    {
                        "type": "refresh_failed",
                        "slug": slug,
                        "error": str(error),
                    }
                )
            finally:
                with self._lock:
                    self.active.discard(slug)

        threading.Thread(
            target=run,
            name=f"web-refresh-{slug}",
            daemon=True,
        ).start()

        return True

    def status(self) -> list[str]:
        with self._lock:
            return sorted(self.active)

    def _account(self, slug: str) -> dict[str, Any] | None:
        for account in self.app.get_accounts():
            if account["slug"] == slug:
                return account
        return None
