"""
Persistent download queue (files/.queue.json).

Tracks are queued from the SPA (per account + video id), then started in
batches through the same DownloadManager the /api/downloads endpoint
uses. Items are consumed when their batch starts; failed downloads can
be retried from the finished session, so a dropped item never silently
disappears without a visible result.

`auto_start` lets the scheduler pick up the next batch whenever an
account's session slot frees up (checked on every scheduler tick).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def queue_path(app: Any) -> Path:
    return Path(app.FILES_DIR) / ".queue.json"


def load_queue(app: Any) -> dict[str, Any]:
    try:
        data = json.loads(queue_path(app).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data.setdefault("auto_start", False)
            return data
    except Exception:
        pass
    return {"version": 1, "auto_start": False, "items": []}


def save_queue(app: Any, data: dict[str, Any]) -> None:
    path = queue_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    temporary.replace(path)


def add_items(app: Any, tracks: list[dict[str, Any]]) -> dict[str, Any]:
    with _lock:
        data = load_queue(app)
        seen = {
            (item["account"], item["video_id"])
            for item in data["items"]
        }
        added = 0
        for track in tracks:
            video_id = str(
                track.get("video_id") or track.get("videoId") or ""
            ).strip()
            slug = str(track.get("account") or "").strip()
            if not video_id or not slug:
                continue
            if (slug, video_id) in seen:
                continue
            seen.add((slug, video_id))
            added += 1
            data["items"].append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "video_id": video_id,
                    "account": slug,
                    "title": track.get("title"),
                    "artist": track.get("artist"),
                    "added_at": int(time.time()),
                }
            )
        save_queue(app, data)
        return {"added": added, "queued": len(data["items"])}


def remove_items(app: Any, ids: list[str]) -> dict[str, Any]:
    wanted = {str(value) for value in ids}
    with _lock:
        data = load_queue(app)
        before = len(data["items"])
        data["items"] = [
            item for item in data["items"] if item["id"] not in wanted
        ]
        save_queue(app, data)
        return {"removed": before - len(data["items"]), "queued": len(data["items"])}


def clear(app: Any) -> dict[str, Any]:
    with _lock:
        data = load_queue(app)
        removed = len(data["items"])
        data["items"] = []
        save_queue(app, data)
        return {"removed": removed, "queued": 0}


def set_auto(app: Any, enabled: bool) -> dict[str, Any]:
    with _lock:
        data = load_queue(app)
        data["auto_start"] = bool(enabled)
        save_queue(app, data)
        return {"auto_start": data["auto_start"], "queued": len(data["items"])}


def start_queued_batches(
    app: Any,
    manager: Any,
    quality: str | None = None,
    policy: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Start one batch per account that has queued items and a free
    session slot. Consumes the items whose batch started (and items that
    are stale — unknown account or gone from the inventory — so the
    queue can never jam). Raises ValueError with a clear message for
    invalid quality/policy/session values."""
    settings = app.load_settings()
    quality = quality or settings.get("default_quality") or "best"
    policy = policy or settings.get("default_existing_policy") or "skip"
    mode = mode or settings.get("default_session") or "once"

    if quality not in [value for _label, value in app.QUALITY_OPTIONS]:
        raise ValueError(f"Unknown quality: {quality}")
    if policy not in {"skip", "replace"}:
        raise ValueError(f"Unknown policy: {policy}")
    if mode not in {"once", "loop", "skip"}:
        raise ValueError(f"Unknown session mode: {mode}")

    with _lock:
        data = load_queue(app)
        items = data["items"]
        if not items:
            return {
                "started": [],
                "deferred": [],
                "dropped": 0,
                "queued_left": 0,
                "snapshots": {},
            }

        by_account: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_account.setdefault(item["account"], []).append(item)

        accounts = {account["slug"]: account for account in app.get_accounts()}
        workers = getattr(manager, "workers", {}) or {}

        started: list[str] = []
        deferred: list[str] = []
        snapshots: dict[str, Any] = {}
        consumed: set[str] = set()
        dropped = 0

        for slug, account_items in by_account.items():
            account = accounts.get(slug)
            if account is None:
                # The account was removed: drop its stale items so the
                # queue does not stall forever.
                consumed.update(item["id"] for item in account_items)
                dropped += len(account_items)
                continue

            entry = workers.get(slug)
            if entry and not entry.get("snapshot", {}).get("finished"):
                deferred.append(slug)
                continue

            wanted = {item["video_id"] for item in account_items}
            tracks = [
                track
                for track in app.inventory_current_tracks(account)
                if track.get("videoId") in wanted
            ]
            resolved = {track.get("videoId") for track in tracks}
            for item in account_items:
                consumed.add(item["id"])
                if item["video_id"] not in resolved:
                    dropped += 1
            if not tracks:
                continue

            sessions = app.build_sessions(
                app.DownloadSession(
                    quality=quality,
                    existing_policy=policy,
                    session=mode,
                )
            )
            snapshots[slug] = manager.start(
                account,
                tracks,
                sessions,
                settings,
                output_dir=None,
                source="library",
            )
            started.append(slug)

        if consumed:
            data["items"] = [
                item for item in data["items"] if item["id"] not in consumed
            ]
            save_queue(app, data)

        return {
            "started": started,
            "deferred": deferred,
            "dropped": dropped,
            "queued_left": len(data["items"]),
            "snapshots": snapshots,
        }
