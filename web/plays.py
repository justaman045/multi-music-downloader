"""
Local play tracking + last.fm scrobbling.

Play counts are stored per account in files/<slug>/.plays.json and
merged into the library by web/metadata.build_library. A play is only
counted when the client reports >= 60% progress, and the same track is
deduplicated within a 90 second window so one listening session counts
once.

Scrobbling is opt-in (settings.lastfm_*). Scrobbles are queued per
account and flushed in the background by web.scheduler.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

COUNTED_THRESHOLD = 60.0
DEDUPE_WINDOW = 90

_lastfm_locks: dict[str, threading.Lock] = {}

SESSION_FILE = Path("config") / "lastfm_session.json"


def _plays_lock(slug: str) -> threading.Lock:
    return _lastfm_locks.setdefault(slug, threading.Lock())


def plays_path(app: Any, slug: str) -> Path:
    if hasattr(app, "FILES_DIR"):
        return Path(app.FILES_DIR) / slug / ".plays.json"
    return Path("files") / slug / ".plays.json"


def load_plays(app: Any, slug: str) -> dict[str, Any]:
    path = plays_path(app, slug)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "version": 1,
        "plays": {},
        "recent": [],
        "updated_at": 0,
    }


def save_plays(app: Any, slug: str, data: dict[str, Any]) -> None:
    path = plays_path(app, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    temporary.replace(path)


def record_play(
    app: Any,
    slug: str,
    video_id: str,
    progress: float,
) -> dict[str, Any]:
    """
    Record a listening event. A play is counted when progress >= 60%
    and the same track was not already counted within the dedupe
    window. Always records the event in the recent log.
    """
    with _plays_lock(slug):
        data = load_plays(app, slug)
        now = int(time.time())

        recent = data.setdefault("recent", [])
        recent.append(
            {
                "video_id": video_id,
                "ts": now,
                "progress": round(progress, 1),
            }
        )
        recent[:] = recent[-500:]

        plays = data.setdefault("plays", {})
        entry = plays.get(video_id) or {
            "video_id": video_id,
            "count": 0,
            "first_played": now,
            "last_played": now,
        }

        counted = False
        if progress >= COUNTED_THRESHOLD:
            recent_counted = [
                item
                for item in reversed(recent[:-1])
                if item["video_id"] == video_id
                and item["progress"] >= COUNTED_THRESHOLD
            ]
            if not recent_counted or now - recent_counted[0]["ts"] > DEDUPE_WINDOW:
                entry["count"] += 1
                entry["last_played"] = now
                counted = True

        plays[video_id] = entry
        data["updated_at"] = now
        save_plays(app, slug, data)

    return {
        "counted": counted,
        "total": entry["count"],
        "progress": progress,
    }


def play_log(app: Any, slug: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Most recent listening events across the chosen accounts."""
    events = []
    for account in app.get_accounts():
        if slug and account["slug"] != slug:
            continue
        data = load_plays(app, account["slug"])
        events.extend(
            {
                **item,
                "account": account["slug"],
            }
            for item in data.get("recent", [])
        )
    events.sort(key=lambda item: item.get("ts", 0), reverse=True)
    return events[:limit]


def play_stats(app: Any, slug: str | None = None) -> dict[str, Any]:
    """Aggregated listening stats for the dashboard."""
    accounts = app.get_accounts()
    if slug:
        accounts = [account for account in accounts if account["slug"] == slug]

    library = None
    try:
        from . import metadata

        library = metadata.build_library(app)
    except Exception:
        library = {"tracks": []}

    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for track in library["tracks"]:
        meta[(track["account"], track["id"])] = track

    total_plays = 0
    unique_tracks = 0
    per_day: dict[str, int] = {}
    per_account: dict[str, int] = {}
    track_counts: dict[tuple[str, str], int] = {}

    for account in accounts:
        slug_now = account["slug"]
        data = load_plays(app, slug_now)
        plays = data.get("plays", {})
        per_account[slug_now] = 0

        for video_id, entry in plays.items():
            count = int(entry.get("count", 0))
            if count <= 0:
                continue
            per_account[slug_now] += count
            track_counts[(slug_now, video_id)] = count
            unique_tracks += 1

        day = time.strftime(
            "%Y-%m-%d",
            time.localtime(data.get("updated_at") or 0),
        )
        per_day[day] = per_day.get(day, 0) + sum(
            int(entry.get("count", 0))
            for entry in plays.values()
            if int(entry.get("count", 0)) > 0
        )

    total_plays = sum(per_account.values())

    top_tracks = []
    for (account_slug, video_id), count in sorted(
        track_counts.items(),
        key=lambda item: (-item[1], item[0][1]),
    )[:25]:
        entry = meta.get((account_slug, video_id))
        if entry is None:
            continue
        top_tracks.append(
            {
                "id": video_id,
                "account": account_slug,
                "title": entry.get("title"),
                "artist": entry.get("artist"),
                "album": entry.get("album"),
                "art": entry.get("art"),
                "plays": count,
            }
        )

    artist_counts: dict[str, int] = {}
    for (account_slug, video_id), count in track_counts.items():
        entry = meta.get((account_slug, video_id))
        if entry is None:
            continue
        artist = entry.get("artist") or "Unknown"
        artist_counts[artist] = artist_counts.get(artist, 0) + count

    top_artists = sorted(
        (
            {"artist": artist, "plays": plays}
            for artist, plays in artist_counts.items()
        ),
        key=lambda item: -item["plays"],
    )[:25]

    return {
        "total_plays": total_plays,
        "unique_tracks": unique_tracks,
        "per_account": per_account,
        "per_day": dict(
            sorted(per_day.items())
        ),
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "recent": play_log(app, slug, limit=50),
    }


def history_stats(
    app: Any,
    slug: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """
    Time-bucketed listening history built from each account's recent
    play log. Unlike play_stats.per_day (which only reports the last
    updated_at), this buckets every counted event by its actual
    timestamp and aggregates top tracks/artists/albums per period.
    """
    days = max(1, min(365, int(days or 30)))
    cutoff = int(time.time()) - days * 86400

    accounts = app.get_accounts()
    if slug:
        accounts = [account for account in accounts if account["slug"] == slug]

    library = None
    try:
        from . import metadata

        library = metadata.build_library(app)
    except Exception:
        library = {"tracks": []}

    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for track in library["tracks"]:
        meta[(track["account"], track["id"])] = track

    per_day: dict[str, int] = {}
    track_counts: dict[tuple[str, str], int] = {}
    artist_counts: dict[str, int] = {}
    album_counts: dict[str, int] = {}

    for account in accounts:
        slug_now = account["slug"]
        data = load_plays(app, slug_now)
        for item in data.get("recent", []):
            ts = int(item.get("ts", 0))
            if ts < cutoff:
                continue
            progress = float(item.get("progress", 0))
            if progress < COUNTED_THRESHOLD:
                continue
            video_id = item.get("video_id")
            if not video_id:
                continue

            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            per_day[day] = per_day.get(day, 0) + 1
            track_counts[(slug_now, video_id)] = (
                track_counts.get((slug_now, video_id), 0) + 1
            )

            entry = meta.get((slug_now, video_id))
            if entry is None:
                continue
            artist = entry.get("artist") or "Unknown"
            album = entry.get("album") or "Unknown"
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
            album_counts[album] = album_counts.get(album, 0) + 1

    top_tracks = []
    for (account_slug, video_id), count in sorted(
        track_counts.items(),
        key=lambda item: (-item[1], item[0][1]),
    )[:20]:
        entry = meta.get((account_slug, video_id))
        if entry is None:
            continue
        top_tracks.append(
            {
                "id": video_id,
                "account": account_slug,
                "title": entry.get("title"),
                "artist": entry.get("artist"),
                "album": entry.get("album"),
                "art": entry.get("art"),
                "plays": count,
            }
        )

    return {
        "days": days,
        "per_day": dict(sorted(per_day.items())),
        "total_plays": sum(per_day.values()),
        "top_tracks": top_tracks,
        "top_artists": sorted(
            (
                {"artist": artist, "plays": plays}
                for artist, plays in artist_counts.items()
            ),
            key=lambda item: -item["plays"],
        )[:20],
        "top_albums": sorted(
            (
                {"album": album, "plays": plays}
                for album, plays in album_counts.items()
            ),
            key=lambda item: -item["plays"],
        )[:20],
    }


# ---------------------------------------------------------------------------
# last.fm scrobbling
# ---------------------------------------------------------------------------

def _lastfm_session_file() -> Path:
    return SESSION_FILE


def load_lastfm_session() -> dict[str, Any]:
    path = _lastfm_session_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_lastfm_session(session: dict[str, Any]) -> None:
    path = _lastfm_session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(session, fh, indent=2, ensure_ascii=False)
    os_chmod(temporary, 0o600)
    temporary.replace(path)


def os_chmod(path: Path, mode: int) -> None:
    try:
        import os

        os.chmod(path, mode)
    except Exception:
        pass


def _api_sig(params: dict[str, str], secret: str) -> str:
    payload = "".join(
        f"{key}{params[key]}"
        for key in sorted(params)
    ) + secret
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _lastfm_post(
    settings: dict[str, Any],
    method: str,
    params: dict[str, Any],
    session_key: str | None = None,
) -> dict[str, Any]:
    secret = settings.get("lastfm_api_secret", "")
    api_key = settings.get("lastfm_api_key", "")

    if not api_key or not secret:
        raise ValueError("last.fm is not configured")

    call: dict[str, str] = {
        "method": method,
        "api_key": api_key,
        **{str(key): str(value) for key, value in params.items()},
    }
    if session_key:
        call["sk"] = session_key

    call["api_sig"] = _api_sig(call, secret)
    call["format"] = "json"

    body = urllib.parse.urlencode(call).encode("utf-8")
    request = urllib.request.Request(
        "https://ws.audioscrobbler.com/2.0/",
        data=body,
        method="POST",
        headers={
            "User-Agent": "ytm-ll-dl-modern/6.0.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _session_key(settings: dict[str, Any]) -> str:
    session = load_lastfm_session()
    key = session.get("sk")
    if key:
        return str(key)

    username = settings.get("lastfm_username", "")
    password_md5 = settings.get("lastfm_password_md5", "")
    if not username or not password_md5:
        raise ValueError("last.fm username / password not configured")

    try:
        result = _lastfm_post(
            settings,
            "auth.getSession",
            {
                "username": username,
                "password": password_md5,
            },
        )
    except Exception as error:
        save_lastfm_session(
            {
                "sk": None,
                "last_error": str(error),
                "updated_at": int(time.time()),
            }
        )
        raise ValueError(f"last.fm login failed: {error}")

    key = (result.get("session") or {}).get("key")
    if not key:
        raise ValueError(f"last.fm rejected login: {result}")

    save_lastfm_session(
        {
            "sk": str(key),
            "last_error": None,
            "updated_at": int(time.time()),
        }
    )
    return str(key)


def lastfm_status(app: Any) -> dict[str, Any]:
    settings = app.load_settings()
    queued = 0
    for account in app.get_accounts():
        queue = _load_queue(app, account["slug"])
        queued += len(queue)

    session = load_lastfm_session()
    return {
        "enabled": bool(
            settings.get("lastfm_enabled")
            and settings.get("lastfm_api_key")
            and settings.get("lastfm_api_secret")
        ),
        "username": settings.get("lastfm_username", ""),
        "queued": queued,
        "session_key": bool(session.get("sk")),
        "last_error": session.get("last_error"),
    }


def _queue_path(app: Any, slug: str) -> Path:
    if hasattr(app, "FILES_DIR"):
        return Path(app.FILES_DIR) / slug / ".lastfm_queue.json"
    return Path("files") / slug / ".lastfm_queue.json"


def _load_queue(app: Any, slug: str) -> list[dict[str, Any]]:
    path = _queue_path(app, slug)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save_queue(app: Any, slug: str, queue: list[dict[str, Any]]) -> None:
    path = _queue_path(app, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(queue, fh, indent=2, ensure_ascii=False)
    temporary.replace(path)


def enqueue_scrobble(
    app: Any,
    slug: str,
    video_id: str,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
) -> dict[str, Any]:
    """Queue a scrobble if last.fm is enabled."""
    settings = app.load_settings()
    if not settings.get("lastfm_enabled"):
        return {"queued": False, "reason": "disabled"}

    if not settings.get("lastfm_api_key") or not settings.get("lastfm_api_secret"):
        return {"queued": False, "reason": "not_configured"}

    inventory = app.load_inventory(_account(app, slug))
    record = inventory["tracks"].get(video_id)
    if record is None and (not title or not artist):
        return {"queued": False, "reason": "unknown_track"}

    with _plays_lock(slug):
        queue = _load_queue(app, slug)
        queue.append(
            {
                "video_id": video_id,
                "title": title or record.get("title"),
                "artist": artist or record.get("artist"),
                "album": album or record.get("album"),
                "timestamp": int(time.time()),
            }
        )
        _save_queue(app, slug, queue)

    return {"queued": True, "reason": "ok"}


def flush_scrobbles(
    app: Any,
    limit_per_account: int = 50,
) -> dict[str, Any]:
    """Push queued scrobbles to last.fm; drop the oldest ones that keep
    failing so the queue cannot grow forever."""
    settings = app.load_settings()
    if not settings.get("lastfm_enabled"):
        return {"sent": 0, "failed": 0, "error": "disabled"}

    try:
        sk = _session_key(settings)
    except Exception as error:
        return {"sent": 0, "failed": 0, "error": str(error)}

    sent = 0
    failed = 0
    failed_batch: list[str] = []

    for account in app.get_accounts():
        slug = account["slug"]
        with _plays_lock(slug):
            queue = _load_queue(app, slug)
            if not queue:
                continue

            remaining: list[dict[str, Any]] = []
            for scrobble in queue[:limit_per_account]:
                try:
                    _lastfm_post(
                        settings,
                        "track.scrobble",
                        {
                            "artist": scrobble.get("artist"),
                            "track": scrobble.get("title"),
                            "album": scrobble.get("album") or "",
                            "timestamp": scrobble.get("timestamp"),
                        },
                        session_key=sk,
                    )
                    sent += 1
                except Exception as error:
                    failed += 1
                    failed_batch.append(str(error))
                    remaining.append(scrobble)

            remaining.extend(queue[limit_per_account:])
            # If every item in a batch failed the queue is stuck;
            # drop the batch so a permanent rejection (bad auth, deleted
            # account) cannot grow the file forever.
            if sent == 0 and failed > 0 and len(failed_batch) == len(
                queue[:limit_per_account]
            ) and len(queue) > 200:
                remaining = remaining[:100]
            _save_queue(app, slug, remaining)

    return {
        "sent": sent,
        "failed": failed,
        "error": None if not failed_batch else "; ".join(set(failed_batch))[:300],
    }


def _account(app: Any, slug: str) -> dict[str, Any]:
    for account in app.get_accounts():
        if account["slug"] == slug:
            return account
    raise KeyError(f"Unknown account: {slug}")
