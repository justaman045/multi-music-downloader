"""
Custom, smart and synced playlists.

Two stores:
  - app-wide `files/playlists.json` for locally created playlists
    (kind="custom" with an explicit track list, or kind="smart"
    with a rules object evaluated against the library).
  - per-account `files/<slug>/.playlists.json` for YouTube Music
    playlists synced by ytm-ll-dl-modern.sync_library_playlists.
    Synced playlists are only offered when online.

Custom playlists can be per-account (account=<slug>, tracks keyed by
video id) or app-wide (account=null, tracks keyed by video id +
account) so a single playlist can mix accounts.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import metadata

_PLAYLIST_NAME_RE = re.compile(r"^[^/\\]{1,200}$")


def playlists_path(app: Any | None = None) -> Path:
    if app is not None and hasattr(app, "FILES_DIR"):
        return Path(app.FILES_DIR) / "playlists.json"
    return Path("files") / "playlists.json"


def load_custom_playlists(app: Any | None = None) -> dict[str, Any]:
    path = playlists_path(app)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("playlists"), list):
                return data
        except Exception:
            pass
    return {"version": 1, "playlists": []}


def save_custom_playlists(app: Any, data: dict[str, Any]) -> None:
    path = playlists_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    temporary.replace(path)


def _store(app: Any) -> dict[str, list[dict[str, Any]]]:
    return load_custom_playlists(app)


def get_playlist(app: Any, playlist_id: str) -> dict[str, Any] | None:
    for playlist in _store(app)["playlists"]:
        if playlist.get("id") == playlist_id:
            return playlist
    return None


def create_playlist(
    app: Any,
    name: str,
    kind: str = "custom",
    account: str | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _PLAYLIST_NAME_RE.match(name):
        raise ValueError("Playlist name contains invalid characters")

    if kind not in {"custom", "smart"}:
        raise ValueError("Playlist kind must be 'custom' or 'smart'")

    if kind == "smart" and not rules:
        raise ValueError("Smart playlists need rules")

    data = _store(app)
    now = int(time.time())

    playlist = {
        "id": uuid.uuid4().hex[:12],
        "name": name.strip(),
        "kind": kind,
        "account": account,
        "rules": rules if kind == "smart" else None,
        "tracks": [],
        "created_at": now,
        "updated_at": now,
    }

    data["playlists"].append(playlist)
    save_custom_playlists(app, data)
    return playlist


def delete_playlist(app: Any, playlist_id: str) -> bool:
    data = _store(app)
    before = len(data["playlists"])
    data["playlists"] = [
        playlist
        for playlist in data["playlists"]
        if playlist.get("id") != playlist_id
    ]
    if len(data["playlists"]) == before:
        return False
    save_custom_playlists(app, data)
    return True


def update_playlist(
    app: Any,
    playlist_id: str,
    name: str | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _store(app)
    for playlist in data["playlists"]:
        if playlist.get("id") != playlist_id:
            continue
        if name is not None:
            if not _PLAYLIST_NAME_RE.match(name):
                raise ValueError("Playlist name contains invalid characters")
            playlist["name"] = name.strip()
        if rules is not None and playlist.get("kind") == "smart":
            if not rules:
                raise ValueError("Smart playlists need rules")
            playlist["rules"] = rules
        playlist["updated_at"] = int(time.time())
        save_custom_playlists(app, data)
        return playlist
    raise KeyError("Unknown playlist")


def add_tracks(
    app: Any,
    playlist_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    data = _store(app)
    for playlist in data["playlists"]:
        if playlist.get("id") != playlist_id:
            continue
        if playlist.get("kind") != "custom":
            raise ValueError("Tracks can only be added to custom playlists")

        account = playlist.get("account")
        existing = {
            (entry.get("video_id"), entry.get("account") or account)
            for entry in playlist.get("tracks", [])
        }
        added = 0

        for entry in entries:
            video_id = entry.get("video_id")
            if not video_id:
                continue
            entry_account = entry.get("account") or account
            key = (video_id, entry_account)
            if key in existing:
                continue
            playlist.setdefault("tracks", []).append(
                {
                    "video_id": video_id,
                    "account": entry_account,
                }
            )
            existing.add(key)
            added += 1

        playlist["updated_at"] = int(time.time())
        save_custom_playlists(app, data)
        return {"added": added, "playlist": playlist}
    raise KeyError("Unknown playlist")


def remove_tracks(
    app: Any,
    playlist_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    data = _store(app)
    for playlist in data["playlists"]:
        if playlist.get("id") != playlist_id:
            continue

        account = playlist.get("account")
        keys = {
            (entry.get("video_id"), entry.get("account") or account)
            for entry in entries
        }

        before = len(playlist.get("tracks", []))
        playlist["tracks"] = [
            entry
            for entry in playlist.get("tracks", [])
            if (entry.get("video_id"), entry.get("account")) not in keys
        ]
        removed = before - len(playlist["tracks"])

        playlist["updated_at"] = int(time.time())
        save_custom_playlists(app, data)
        return {"removed": removed, "playlist": playlist}
    raise KeyError("Unknown playlist")


def smart_match(
    track: dict[str, Any],
    rules: dict[str, Any],
) -> bool:
    """Evaluate a single library track entry against smart rules."""
    def _norm(value: Any) -> str:
        return str(value or "").strip().lower()

    account = rules.get("account")
    if account and track.get("account") != account:
        return False

    status = rules.get("status")
    if status and track.get("status") != status:
        return False

    artist = rules.get("artist")
    if artist and artist.lower() not in _norm(track.get("artist")):
        return False

    genre = rules.get("genre")
    if genre and genre.lower() not in _norm(track.get("genre")):
        return False

    album = rules.get("album")
    if album and album.lower() not in _norm(track.get("album")):
        return False

    title = rules.get("title")
    if title and title.lower() not in _norm(track.get("title")):
        return False

    min_plays = rules.get("min_plays")
    if min_plays is not None and int(track.get("plays", 0) or 0) < int(min_plays):
        return False

    max_plays = rules.get("max_plays")
    if max_plays is not None and int(track.get("plays", 0) or 0) > int(max_plays):
        return False

    year = rules.get("year")
    if year is not None and int(track.get("year") or 0) != int(year):
        return False

    duration = track.get("duration") or 0
    min_secs = rules.get("min_duration_secs")
    if min_secs is not None and int(duration) < int(min_secs):
        return False
    max_secs = rules.get("max_duration_secs")
    if max_secs is not None and int(duration) > int(max_secs):
        return False

    return True


def evaluate_smart_playlist(
    app: Any,
    playlist: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a smart playlist against the current library."""
    if playlist.get("kind") != "smart":
        return []
    rules = playlist.get("rules") or {}
    library = metadata.build_library(app)
    tracks = [
        track
        for track in library["tracks"]
        if smart_match(track, rules)
    ]
    max_count = rules.get("max_count")
    if max_count:
        tracks = tracks[: int(max_count)]
    return tracks


def resolve_playlist_tracks(
    app: Any,
    playlist: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the library track entries a playlist contains, in order."""
    if playlist.get("kind") == "smart":
        return evaluate_smart_playlist(app, playlist)

    library = metadata.build_library(app)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for track in library["tracks"]:
        by_key[(track["id"], track["account"])] = track

    resolved = []
    for entry in playlist.get("tracks", []):
        track = by_key.get(
            (entry.get("video_id"), entry.get("account"))
        )
        if track:
            resolved.append(track)
    return resolved


def playlist_summary(
    app: Any,
    playlist: dict[str, Any],
) -> dict[str, Any]:
    """Playlist without full track metadata, for list views."""
    resolved = resolve_playlist_tracks(app, playlist)
    return {
        "id": playlist.get("id"),
        "name": playlist.get("name"),
        "kind": playlist.get("kind"),
        "account": playlist.get("account"),
        "rules": playlist.get("rules"),
        "track_count": len(resolved),
        "created_at": playlist.get("created_at"),
        "updated_at": playlist.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Export / import (m3u + JSON + CSV)
# ---------------------------------------------------------------------------

def _csv_field(value: Any) -> str:
    text = str(value if value is not None else "")
    if any(char in text for char in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def export_playlist(
    app: Any,
    playlist: dict[str, Any],
    fmt: str,
) -> Path:
    output = app.export_directory()
    safe = re.sub(r"[^\w.-]+", "_", playlist.get("name", "playlist"))
    safe = safe.strip("_") or "playlist"

    if fmt == "json":
        resolved = [
            {
                "video_id": track.get("id"),
                "account": track.get("account"),
                "title": track.get("title"),
                "artist": track.get("artist"),
                "album": track.get("album"),
            }
            for track in resolve_playlist_tracks(app, playlist)
        ]
        data = {
            "version": 1,
            "name": playlist.get("name"),
            "kind": playlist.get("kind"),
            "account": playlist.get("account"),
            "rules": playlist.get("rules"),
            "tracks": resolved,
        }
        path = output / f"{safe}-{playlist.get('id')}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        temporary.replace(path)
        return path

    if fmt == "csv":
        header = ["video_id", "account", "title", "artist", "album"]
        rows = [
            [
                track.get("id"),
                track.get("account"),
                track.get("title"),
                track.get("artist"),
                track.get("album"),
            ]
            for track in resolve_playlist_tracks(app, playlist)
        ]
        lines = [",".join(_csv_field(value) for value in header)]
        lines.extend(
            ",".join(_csv_field(value) for value in row)
            for row in rows
        )
        path = output / f"{safe}-{playlist.get('id')}.csv"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        temporary.replace(path)
        return path

    lines = ["#EXTM3U"]
    for track in resolve_playlist_tracks(app, playlist):
        file_path = metadata.best_file_for(
            app,
            _account_for(app, track.get("account")),
            track.get("id"),
        )
        if file_path is None or not file_path.exists():
            continue
        lines.append(f"#EXTINF:0,{track.get('artist')} - {track.get('title')}")
        lines.append(str(file_path))

    path = output / f"{safe}-{playlist.get('id')}.m3u"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    temporary.replace(path)
    return path


def _account_for(app: Any, slug: str | None) -> dict[str, Any] | None:
    if not slug:
        return None
    for account in app.get_accounts():
        if account["slug"] == slug:
            return account
    return None


def import_playlist_json(
    app: Any,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Create a custom playlist from exported JSON."""
    name = str(data.get("name") or "Imported playlist")
    kind = str(data.get("kind") or "custom")
    if kind == "smart":
        playlist = create_playlist(
            app,
            name,
            kind="smart",
            rules=data.get("rules"),
        )
        return playlist

    account = data.get("account")
    playlist = create_playlist(
        app,
        name,
        kind="custom",
        account=account,
    )
    entries = [
        {
            "video_id": entry.get("video_id"),
            "account": entry.get("account") or account,
        }
        for entry in data.get("tracks", [])
        if entry.get("video_id")
    ]
    if entries:
        add_tracks(app, playlist["id"], entries)
    return playlist


def import_playlist_m3u(
    app: Any,
    text: str,
    name: str,
) -> dict[str, Any]:
    """Create a custom playlist from an m3u file, matching entries to
    downloaded files by absolute path."""
    playlist = create_playlist(app, name, kind="custom", account=None)

    library = metadata.build_library(app)
    by_slug: dict[str, dict[str, Any]] = {}
    for account in app.get_accounts():
        by_slug[account["slug"]] = account

    path_to_entry: dict[str, tuple[str, str]] = {}
    for track in library["tracks"]:
        account = by_slug.get(track["account"])
        if account is None:
            continue
        file_path = metadata.best_file_for(app, account, track["id"])
        if file_path:
            path_to_entry[str(file_path)] = (track["id"], track["account"])

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = path_to_entry.get(line)
        if match:
            entries.append(
                {
                    "video_id": match[0],
                    "account": match[1],
                }
            )

    if entries:
        add_tracks(app, playlist["id"], entries)

    return playlist
