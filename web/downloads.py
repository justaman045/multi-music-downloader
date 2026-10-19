"""
Download session management for the web UI.

Runs the same DownloadWorker the TUI uses, but routes its event
stream into a shared event bus instead of Textual. The SPA consumes
the bus via Server-Sent Events (and can poll /api/downloads).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .bus import EventBus


class DownloadManager:
    """One shared manager: per-account workers + an SSE event bus."""

    def __init__(self, app: Any, bus: EventBus):
        self.app = app
        self.bus = bus
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}
        self.workers: dict[str, dict[str, Any]] = {}

    # -- session management -------------------------------------------------

    def start(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        sessions: list[Any],
        settings: dict[str, Any],
        output_dir=None,
        source: str = "library",
    ) -> dict[str, Any]:
        slug = account["slug"]
        session_id = uuid.uuid4().hex[:12]

        snapshot: dict[str, Any] = {
            "session_id": session_id,
            "slug": slug,
            "name": account.get("name", slug),
            "quality": sessions[0].quality if sessions else None,
            "stage": None,
            "track": None,
            "progress": 0.0,
            "status": "Starting…",
            "counts": {
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
                "unavailable": 0,
            },
            "finished": False,
            "finished_info": None,
        }

        def callback(event: str, data: dict[str, Any]) -> None:
            self._handle_event(slug, session_id, event, data)

        worker = self.app.DownloadWorker(
            account,
            tracks,
            sessions,
            settings,
            callback,
            output_dir=output_dir,
            source=source,
        )

        with self._lock:
            self.workers[slug] = {
                "session_id": session_id,
                "worker": worker,
                "snapshot": snapshot,
            }
            self._started_at[slug] = time.time()

        def run() -> None:
            try:
                worker.run()
            finally:
                pass

        threading.Thread(
            target=run,
            name=f"web-download-{slug}",
            daemon=True,
        ).start()

        return snapshot

    def _handle_event(
        self,
        slug: str,
        session_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        with self._lock:
            entry = self.workers.get(slug)
            if entry is None:
                return
            snapshot = entry["snapshot"]

        if event == "stage":
            snapshot["stage"] = data
            snapshot["status"] = (
                f"Stage {data.get('index')}/{data.get('total')} "
                f"— {data.get('quality')}"
            )
            snapshot["progress"] = 0.0
        elif event == "track":
            snapshot["track"] = data
            snapshot["status"] = "Downloading…"
            snapshot["progress"] = 0.0
        elif event == "progress":
            # Worker emits "value"; accept both for compatibility
            value = data.get("percent") if data.get("percent") is not None else data.get("value")
            try:
                snapshot["progress"] = float(value or 0)
            except Exception:
                snapshot["progress"] = 0.0
        elif event == "status":
            snapshot["status"] = data.get("text", "")
        elif event == "counts":
            snapshot["counts"].update(data)
        elif event == "finished":
            snapshot["finished"] = True
            snapshot["finished_info"] = data
            snapshot["progress"] = 100.0
            self._schedule_cleanup(slug, session_id)
            self.bus.broadcast(
                {
                    "type": "session_finished",
                    "slug": slug,
                    "session_id": session_id,
                    **data,
                }
            )

        self.bus.broadcast(
            {
                "type": "session_update",
                "session": snapshot,
            }
        )

    def _schedule_cleanup(self, slug: str, session_id: str) -> None:
        def _cleanup() -> None:
            time.sleep(12)
            with self._lock:
                entry = self.workers.get(slug)
                if entry and entry["session_id"] == session_id:
                    del self.workers[slug]

        threading.Thread(
            target=_cleanup,
            name=f"web-dl-cleanup-{slug}",
            daemon=True,
        ).start()

    def snapshot(self, slug: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if slug is not None:
                entry = self.workers.get(slug)
                return [dict(entry["snapshot"])] if entry else []
            return [
                dict(entry["snapshot"])
                for entry in self.workers.values()
            ]

    def stop_after_stage(self, slug: str) -> bool:
        with self._lock:
            entry = self.workers.get(slug)
            if entry is None:
                return False
            worker = entry["worker"]
        worker.stop_after_stage()
        return True

    def cancel(self, slug: str) -> bool:
        with self._lock:
            entry = self.workers.get(slug)
            if entry is None:
                return False
            worker = entry["worker"]
        worker.cancel()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            slugs = list(self.workers.keys())
        for slug in slugs:
            self.cancel(slug)


manager: DownloadManager | None = None


def get_manager(app: Any, bus: EventBus) -> DownloadManager:
    global manager
    if manager is None:
        manager = DownloadManager(app, bus)
    return manager
