"""
Sync the downloaded library into an external folder.

Copies (or hardlinks) every downloaded file into a predictable structure:

    <target>/<Account Name>/<Artist>/<Album>/<file name>

Hardlinks are used when the target sits on the same filesystem (so a
"copy" costs no extra space — handy for Jellyfin/Plex/LMS folders on the
same disk), with a transparent fallback to a byte copy across devices.
The operation is idempotent: files already present with matching content
are skipped, and re-synced tracks whose source changed are replaced.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import metadata

MAX_ERRORS = 25


def _segment(value: Any) -> str:
    """One safe path segment (no separators, no dot-only names)."""
    text = str(value or "").strip()
    cleaned = "".join(
        char if char.isalnum() or char in (" ", "-", "_", ".") else "_"
        for char in text
    )
    cleaned = cleaned.strip(" .")
    return cleaned[:120] or "Unknown"


def sync_library_to_folder(
    app: Any,
    target: Any,
    slug: str | None = None,
    mode: str = "link",
    dry_run: bool = False,
) -> dict[str, Any]:
    if mode not in ("link", "copy"):
        raise ValueError("mode must be 'link' or 'copy'")

    raw = Path(str(target)).expanduser()
    if not raw.is_absolute():
        raise ValueError("Sync target must be an absolute path")
    if str(raw) in ("/", str(Path.home())):
        raise ValueError("Refusing to sync into that folder — pick a dedicated directory")

    target_dir = raw.resolve()
    files_root = Path(app.FILES_DIR).resolve()
    if target_dir == files_root or files_root in target_dir.parents:
        raise ValueError("Sync target must be outside the app's files/ directory")

    accounts = app.get_accounts()
    if slug:
        accounts = [account for account in accounts if account["slug"] == slug]
        if not accounts:
            raise ValueError("Unknown account")

    scanned = linked = copied = replaced = skipped = missing = 0
    errors: list[dict[str, str]] = []

    for account in accounts:
        account_dir = target_dir / _segment(account.get("name") or account["slug"])
        inventory = app.load_inventory(account)
        for video_id, record in (inventory.get("tracks") or {}).items():
            source = metadata.best_file_for(app, account, video_id)
            if source is None:
                missing += 1
                continue
            scanned += 1

            destination = (
                account_dir
                / _segment(record.get("artist"))
                / _segment(record.get("album"))
                / source.name
            )

            try:
                if destination.exists():
                    try:
                        if os.path.samefile(source, destination):
                            skipped += 1
                            continue
                    except OSError:
                        pass
                    if (
                        destination.is_file()
                        and destination.stat().st_size == source.stat().st_size
                    ):
                        skipped += 1
                        continue
                    # The source was re-downloaded (e.g. higher quality):
                    # replace the stale copy.
                    if not dry_run:
                        destination.unlink()
                    replaced += 1

                if dry_run:
                    if mode == "link":
                        linked += 1
                    else:
                        copied += 1
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                if mode == "link":
                    try:
                        os.link(source, destination)
                        linked += 1
                    except OSError:
                        # Cross-device (or otherwise unsupported) hardlink:
                        # fall back to a byte copy so the sync succeeds.
                        shutil.copy2(source, destination)
                        copied += 1
                else:
                    shutil.copy2(source, destination)
                    copied += 1
            except Exception as error:
                if len(errors) < MAX_ERRORS:
                    errors.append(
                        {"file": source.name, "error": str(error)}
                    )

    return {
        "target": str(target_dir),
        "mode": mode,
        "dry_run": dry_run,
        "scanned": scanned,
        "linked": linked,
        "copied": copied,
        "replaced": replaced,
        "skipped": skipped,
        "missing": missing,
        "errors": errors,
    }
