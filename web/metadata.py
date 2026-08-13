"""
Metadata enrichment for the web UI.

The on-disk inventory only stores title/artist/album. For a rich
library browser we add year, genre, duration, album art and play
counts by reading the downloaded files (mutagen) and the Most Played
report. Everything is cached in-process and, for art, written to a
per-account cache directory.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

# Best-priority order for choosing which downloaded file represents
# a track in the UI/player.
QUALITY_PRIORITY = [
    "best",
    "flac",
    "m4a_256",
    "mp3_320",
    "mp3_v0",
    "mp3_256",
    "mp3_192",
    "mp3_128",
]

_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")

_tag_cache: dict[tuple[str, str], dict[str, Any]] = {}
_art_checked: set[tuple[str, str]] = set()


def _mutagen():
    try:
        import mutagen
        import mutagen.flac
        import mutagen.mp3
        import mutagen.mp4

        return mutagen
    except Exception:
        return None


def best_file_for(
    app: Any,
    account: dict[str, Any],
    video_id: str,
) -> Path | None:
    """Return the highest-priority downloaded file that still exists."""
    inventory = app.load_inventory(account)
    record = inventory["tracks"].get(video_id)
    if not record:
        return None

    qualities = record.get("qualities", {})
    for quality in QUALITY_PRIORITY:
        entry = qualities.get(quality)
        if not entry:
            continue
        if entry.get("status") != "downloaded":
            continue
        path = entry.get("file")
        if not path:
            continue
        candidate = Path(path)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def read_tags(path: Path) -> dict[str, Any]:
    """Extract year/genre/duration from an audio file via mutagen."""
    key = (str(path), str(path.stat().st_mtime_ns))
    if key in _tag_cache:
        return _tag_cache[key]

    result: dict[str, Any] = {
        "year": None,
        "genre": None,
        "duration": None,
    }

    m = _mutagen()
    if m is not None:
        try:
            audio = m.File(str(path), easy=False)
            if audio is not None:
                tags = getattr(audio, "tags", None)
                info = getattr(audio, "info", None)

                if info is not None:
                    length = getattr(info, "length", None)
                    if length:
                        result["duration"] = int(length)

                if tags is not None:
                    result["year"], result["genre"] = _tags_year_genre(
                        type(audio).__module__,
                        tags,
                    )
        except Exception:
            pass

    _tag_cache[key] = result
    return result


def _tags_year_genre(module: str, tags: Any) -> tuple[int | None, str | None]:
    year = None
    genre = None

    if "mp4" in module:
        day = tags.get("\xa9day")
        if day:
            match = _YEAR_RE.search(str(day))
            if match:
                year = int(match.group(1))
        gen = tags.get("\xa9gen")
        if gen:
            genre = str(gen)
    elif "id3" in module or "mp3" in module:
        for frame in ("TDRC", "TDOR", "TYER"):
            value = tags.get(frame)
            if value:
                text = str(value)
                match = _YEAR_RE.search(text)
                if match:
                    year = int(match.group(1))
                    break
        value = tags.get("TCON")
        if value:
            genre = str(value)
    else:  # FLAC / Vorbis comments
        for key in ("DATE", "YEAR", "ORIGINALDATE", "TDRC"):
            value = tags.get(key)
            if value:
                text = str(value[0] if isinstance(value, list) else value)
                match = _YEAR_RE.search(text)
                if match:
                    year = int(match.group(1))
                    break
        value = tags.get("GENRE") or tags.get("genre")
        if value:
            genre = str(value[0] if isinstance(value, list) else value)

    return year, genre


# ---------------------------------------------------------------------------
# Album art
# ---------------------------------------------------------------------------

def art_file_for(
    app: Any,
    account: dict[str, Any],
    video_id: str,
) -> Path:
    return (
        Path(account["output"])
        / ".art"
        / f"{video_id}.jpg"
    )


def art_url_for(
    app: Any,
    account: dict[str, Any],
    video_id: str,
) -> str | None:
    return (
        f"/api/art/{account['slug']}/{video_id}.jpg"
    )


def ensure_art(
    app: Any,
    account: dict[str, Any],
    video_id: str,
) -> Path | None:
    """Return the cached art path for a track, extracting it from the
    file's embedded artwork on first use."""
    dest = art_file_for(app, account, video_id)

    key = (account["slug"], video_id)
    if key in _art_checked:
        return dest if dest.exists() else None

    _art_checked.add(key)

    if dest.exists():
        return dest

    path = best_file_for(app, account, video_id)
    if path is None:
        return None

    data = _extract_art_data(path)
    if not data:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(data)
        return dest
    except Exception:
        return None


def _extract_art_data(path: Path) -> bytes | None:
    m = _mutagen()
    if m is None:
        return None

    try:
        audio = m.File(str(path))
        if audio is None:
            return None

        tags = getattr(audio, "tags", None)
        module = type(audio).__module__

        if "id3" in module or "mp3" in module:
            if tags:
                for apic in tags.getall("APIC"):
                    if apic.data:
                        return apic.data
            return None

        if "mp4" in module:
            covers = tags.get("\xa9covr")
            if covers:
                for cover in covers:
                    data = getattr(cover, "data", None)
                    if data:
                        return bytes(data)
            return None

        if "flac" in module or "ogg" in module:
            pictures = getattr(audio, "pictures", None)
            if pictures:
                for pic in pictures:
                    data = getattr(pic, "data", None)
                    if data:
                        return bytes(data)
            if tags:
                raw = tags.get("METADATA_BLOCK_PICTURE")
                if raw:
                    value = raw[0] if isinstance(raw, list) else raw
                    try:
                        binary = base64.b64decode(str(value))
                        pic = m.flac.Picture(binary)
                        return bytes(pic.data)
                    except Exception:
                        pass
            return None
    except Exception:
        return None

    return None


# ---------------------------------------------------------------------------
# Library model
# ---------------------------------------------------------------------------

def build_library(app: Any) -> dict[str, Any]:
    """Merge every account's inventory into one enriched track list."""
    accounts = app.get_accounts()

    track_entries: list[dict[str, Any]] = []
    account_summaries: list[dict[str, Any]] = []

    plays_by_account: dict[str, dict[str, int]] = {}
    most_played_reports: dict[str, Path] = {}

    for account in accounts:
        slug = account["slug"]
        inventory = app.load_inventory(account)
        records = inventory["tracks"]

        plays: dict[str, int] = {}
        report = (
            Path(account["output"]) / "Most_Played" / ".most_played.json"
        )
        if report.exists():
            try:
                import json

                data = json.loads(report.read_text(encoding="utf-8"))
                for entry in data.get("tracks", []):
                    plays[str(entry.get("video_id"))] = int(
                        entry.get("plays", 0)
                    )
            except Exception:
                pass
        plays_by_account[slug] = plays
        most_played_reports[slug] = report

        liked = 0
        downloaded = 0
        file_bytes = 0

        for video_id, record in records.items():
            if record.get("currently_liked"):
                liked += 1

            path = best_file_for(app, account, video_id)
            has_file = path is not None
            if has_file:
                downloaded += 1
                try:
                    file_bytes += path.stat().st_size
                except Exception:
                    pass

            status = _track_status(record, has_file)
            tags = read_tags(path) if path else {}

            track_entries.append(
                {
                    "id": video_id,
                    "title": record.get("title") or "Unknown Title",
                    "artist": record.get("artist") or "Unknown Artist",
                    "album": record.get("album"),
                    "ext": path.suffix.lstrip(".") if path else None,
                    "year": tags.get("year"),
                    "genre": tags.get("genre"),
                    "duration": tags.get("duration"),
                    "plays": plays.get(video_id, 0),
                    "liked": bool(record.get("currently_liked")),
                    "added": record.get("last_seen_liked"),
                    "account": slug,
                    "account_name": account.get("name", slug),
                    "status": status,
                    "quality": _track_quality(app, record, has_file),
                    "art": art_url_for(app, account, video_id) if has_file else None,
                    "stream": (
                        f"/api/media/{slug}/{video_id}/stream"
                        if has_file
                        else None
                    ),
                }
            )

        account_summaries.append(
            {
                "slug": slug,
                "name": account.get("name", slug),
                "liked_count": liked,
                "downloaded_count": downloaded,
                "size_bytes": file_bytes,
            }
        )

    return {
        "accounts": account_summaries,
        "tracks": track_entries,
    }


def _track_status(record: dict[str, Any], has_file: bool) -> str:
    qualities = record.get("qualities", {})
    downloaded = any(
        entry.get("status") == "downloaded" and entry.get("file")
        for entry in qualities.values()
    )

    if has_file:
        return "downloaded"
    if downloaded:
        return "missing"
    if qualities:
        return "partial"
    return "none"


def _track_quality(
    app: Any,
    record: dict[str, Any],
    has_file: bool,
) -> str | None:
    qualities = record.get("qualities", {})
    for quality in QUALITY_PRIORITY:
        entry = qualities.get(quality)
        if not entry:
            continue
        if entry.get("status") != "downloaded":
            continue
        if has_file:
            return app.quality_short_label(quality)
    return None
