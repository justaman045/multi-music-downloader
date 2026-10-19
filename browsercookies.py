"""Automatic sign-in via the user's own browser cookies.

Google blocks third-party OAuth apps from reading a user's YouTube
Music library: YouTube's internal (youtubei) API rejects every
third-party OAuth bearer token with HTTP 400, and the official Data
API cannot expose liked music unless the *user* runs their own Google
Cloud project with the API enabled. The reliable automatic sign-in is
therefore to read the signed-in youtube.com cookies straight out of
the user's own browser cookie store and turn them into the same
ytmusicapi "browser headers" auth the manual paste flow uses.

Supported browsers: Firefox, Chrome, Chromium, Edge, Brave, Opera and
Vivaldi (Chromium family on all three platforms, Firefox everywhere).
Safari is intentionally not supported: on macOS Safari encrypts cookie
values with machine-bound keys we cannot portably read.

Chromium cookie encryption (mirrors Chromium's os_crypt and yt-dlp's
battle-tested extractor):

- Linux: ``v10`` values use the well-known "peanuts" PBKDF2 key;
  ``v11`` values use a key derived from the browser's "… Safe
  Storage" password held in the login keyring — Secret Service via
  ``secretstorage`` (GNOME and friends), KWallet via ``kwallet-query``
  (KDE). Modern Chrome (155+) no longer writes ``os_crypt.encrypted_key``
  into Local State at all (the "portal" scheme), so Local State is
  Windows-only as a key source.
- macOS: ``v10`` values derive from the Keychain "… Safe Storage"
  password ("security find-generic-password").
- Windows: ``v10`` values are AES-GCM with the DPAPI-unprotected key
  from Local State; older values are DPAPI blobs directly.
- Chrome 127+ on Windows/macOS can use app-bound (``v20``)
  encryption which nobody outside Google can decrypt; those cookies
  are skipped with a clear error pointing at other browsers or
  manual paste.
- Cookies DB ``meta`` schema version >= 24 prepends a 32-byte hash
  to every decrypted value; it is stripped before decoding.

Failures always raise :class:`BrowserError` with an actionable
message (which browser to close, which keyring to unlock, or to fall
back to pasting headers) — never a bare traceback.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "list_browsers",
    "list_profiles",
    "auto_profile",
    "header_is_live",
    "extract_cookie_header",
    "headers_raw_for",
    "BrowserError",
]


class BrowserError(ValueError):
    """Actionable failure while importing browser cookies."""


# ytmusicapi overrides the UA with its own default anyway; this value
# only lands in the stored auth file.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    " AppleWebKit/537.36 (KHTML, like Gecko) "
    " Chrome/120.0.0.0 Safari/537.36"
)

_YOUTUBE_CLIENT_VERSION = "2.20250312.04.00"
_REQUIRED_SAPISID_COOKIE = "__Secure-3PAPISID"

# Keep everything belonging to youtube.com and google.com (the
# SAPISIDHASH auth needs the google.com session cookies; closest to
# what the manual paste flow forwards).
_DOMAIN_FILTERS = ("%youtube.com", "%google.com")

# PBKDF2 parameters from Chromium's os_crypt per platform.
_LINUX_ITERATIONS, _LINUX_DKLEN = 1, 16
_MAC_ITERATIONS, _MAC_DKLEN = 1003, 16
_SALT = b"saltysalt"
_PEANUTS = b"peanuts"


# ---------------------------------------------------------------------------
# Browser registry: id -> per-platform user-data locations
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def _linux_user_data(chrome_dir: str) -> Path:
    return _home() / ".config" / chrome_dir


def _mac_user_data(chrome_dir: str) -> Path:
    return _home() / "Library" / "Application Support" / chrome_dir


def _win_user_data(chrome_dir: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
    return Path(base) / chrome_dir


def _opera_user_data() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return Path(base) / "Opera Software" / "Opera Stable"
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / "com.operasoftware.Opera"
    return _home() / ".config" / "opera"


# Chromium browsers: (label, windows/mac dirs, keychain service+account,
# Secret Service label, `application` attribute values used to pick our
# entry out of the keyring — Electron apps share the "… Safe Storage"
# label with different application attributes).
_CHROMIUM_BROWSERS: dict[str, dict[str, Any]] = {
    "chrome": {
        "label": "Google Chrome",
        "win_dir": "Google/Chrome/User Data",
        "mac_dir": "Google/Chrome",
        "linux_dir": "google-chrome",
        "keychain": ("Chrome Safe Storage", "Chrome"),
        "secret_label": "Chrome Safe Storage",
        "secret_apps": ("chrome",),
    },
    "chromium": {
        "label": "Chromium",
        "win_dir": "Chromium/User Data",
        "mac_dir": "Chromium",
        "linux_dir": "chromium",
        "keychain": ("Chromium Safe Storage", "Chromium"),
        "secret_label": "Chromium Safe Storage",
        "secret_apps": ("chromium",),
    },
    "edge": {
        "label": "Microsoft Edge",
        "win_dir": "Microsoft/Edge/User Data",
        "mac_dir": "Microsoft Edge",
        "linux_dir": "microsoft-edge",
        "keychain": ("Microsoft Edge Safe Storage", "Microsoft Edge"),
        "secret_label": "Microsoft Edge Safe Storage",
        "secret_apps": ("microsoft-edge", "msedge", "edge"),
    },
    "brave": {
        "label": "Brave",
        "win_dir": "BraveSoftware/Brave-Browser/User Data",
        "mac_dir": "BraveSoftware/Brave-Browser",
        "linux_dir": "BraveSoftware/Brave-Browser",
        "keychain": ("Brave Safe Storage", "Brave"),
        "secret_label": "Brave Safe Storage",
        "secret_apps": ("brave", "brave-browser"),
    },
    "opera": {
        "label": "Opera",
        "win_dir": None,  # custom location, see _opera_user_data()
        "mac_dir": None,
        "linux_dir": None,
        "keychain": ("Opera Safe Storage", "Opera"),
        "secret_label": "Opera Safe Storage",
        "secret_apps": ("opera",),
    },
    "vivaldi": {
        "label": "Vivaldi",
        "win_dir": "Vivaldi/User Data",
        "mac_dir": "Vivaldi",
        "linux_dir": "vivaldi",
        "keychain": ("Vivaldi Safe Storage", "Vivaldi"),
        "secret_label": "Vivaldi Safe Storage",
        "secret_apps": ("vivaldi",),
    },
}

_FIREFOX = {"id": "firefox", "label": "Firefox"}

_KNOWN_BROWSER_IDS = [*(_CHROMIUM_BROWSERS.keys()), _FIREFOX["id"]]


def _user_data_dir(browser: str) -> Path:
    meta = _CHROMIUM_BROWSERS[browser]
    system = platform.system()
    if browser == "opera":
        return _opera_user_data()
    if system == "Windows":
        return _win_user_data(meta["win_dir"])
    if system == "Darwin":
        return _mac_user_data(meta["mac_dir"])
    return _linux_user_data(meta["linux_dir"])


def _cookie_db_path(browser: str, profile: str | None) -> Path:
    root = _user_data_dir(browser)
    return root / (profile or "Default") / "Cookies"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (_home() / ".config"))


def _firefox_profile_roots() -> list[Path]:
    """Candidate profile roots, newest Firefox conventions first.

    FF147+ honors XDG (~/.config); older builds use ~/.mozilla;
    flatpak and snap keep their own copies.
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return [
            Path(base) / "Mozilla" / "Firefox" / "Profiles",
        ]
    if system == "Darwin":
        return [
            _home() / "Library" / "Application Support" / "Firefox" / "Profiles",
        ]
    return [
        _config_home() / "mozilla" / "firefox",
        _home() / ".mozilla" / "firefox",
        _home() / ".var" / "app" / "org.mozilla.firefox" / "config" / "mozilla" / "firefox",
        _home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        _home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
    ]


def _firefox_profile_dir(profile: str | None) -> Path | None:
    candidates: list[Path] = []
    for root in _firefox_profile_roots():
        if root.is_dir():
            candidates.extend(sorted(p for p in root.iterdir() if p.is_dir()))
    if profile:
        for path in candidates:
            if profile in path.name:
                return path
        return None
    for path in candidates:
        if (path / "cookies.sqlite").exists():
            return path
    return None


# ---------------------------------------------------------------------------
# Key sources
# ---------------------------------------------------------------------------


def _pbkdf2(password: bytes, iterations: int, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, iterations, dklen)


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt a DPAPI blob (Windows only)."""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    blob_in = DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise BrowserError("Windows DPAPI could not unprotect the browser key.")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return raw


def _keychain_password(service: str, account: str) -> bytes | None:
    """macOS Keychain lookup for the browser's Safe Storage password."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                service,
                "-a",
                account,
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip(b"\n")


def _libsecret_passwords(label: str, applications: tuple[str, ...]) -> list[bytes]:
    """Safe Storage passwords from the Secret Service (GNOME et al).

    Multiple entries can share the label (migrated copies, Electron
    apps); entries whose ``application`` attribute matches our browser
    are tried first. Locked entries are skipped rather than unlocking
    — prompting a keyring dialog from a downloader would be rude, and
    Chrome leaves its own entry unlocked while running.
    """
    try:
        import secretstorage  # type: ignore[import-not-found]
    except Exception:
        return []

    found: list[tuple[int, bytes]] = []
    try:
        with contextlib.closing(secretstorage.dbus_init()) as connection:
            collection = secretstorage.get_default_collection(connection)
            for item in collection.get_all_items():
                try:
                    if item.get_label() != label or item.is_locked():
                        continue
                    attributes = dict(item.get_attributes())
                    secret = bytes(item.get_secret())
                except Exception:
                    continue
                priority = 0 if attributes.get("application") in applications else 1
                found.append((priority, secret))
    except Exception:
        return []
    found.sort(key=lambda entry: entry[0])
    return [secret for _, secret in found]


def _kwallet_password(label: str) -> bytes | None:
    """KDE KWallet lookup via kwallet-query (best effort)."""
    if shutil.which("kwallet-query") is None:
        return None
    wallet = "kdewallet"
    try:
        probe = subprocess.run(
            [
                "dbus-send",
                "--session",
                "--print-reply=literal",
                "--dest=org.kde.kwalletd5",
                "/modules/kwalletd5",
                "org.kde.KWallet.networkWallet",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            wallet = probe.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    folder = label.removesuffix(" Safe Storage") + " Keys"
    try:
        result = subprocess.run(
            [
                "kwallet-query",
                "--read-password",
                label,
                "--folder",
                folder,
                wallet,
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    password = result.stdout.rstrip(b"\n")
    if not password or password.lower().startswith(b"failed to read"):
        return None
    return password


def _keyring_passwords(browser: str) -> list[bytes]:
    """All candidate Safe Storage passwords for a Linux browser."""
    meta = _CHROMIUM_BROWSERS[browser]
    passwords = _libsecret_passwords(
        meta["secret_label"],
        meta["secret_apps"],
    )
    if not passwords:
        kwallet = _kwallet_password(meta["secret_label"])
        if kwallet is not None:
            passwords.append(kwallet)
    return passwords


def _windows_gcm_key(browser: str, user_data_dir: Path) -> bytes:
    """DPAPI-unprotect the AES-GCM key from Local State (Windows)."""
    meta = _CHROMIUM_BROWSERS[browser]
    local_state_path = user_data_dir / "Local State"
    if not local_state_path.exists():
        raise BrowserError(
            f"{meta['label']} profile data was not found at {user_data_dir}."
        )
    try:
        local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
        encrypted = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError) as error:
        raise BrowserError(
            f"Could not read {meta['label']} key material: {error}"
        ) from error
    for prefix in (b"v10", b"v11"):
        if encrypted.startswith(prefix):
            encrypted = encrypted[len(prefix):]
    if encrypted.startswith(b"DPAPI"):
        encrypted = encrypted[len(b"DPAPI"):]
    return _dpapi_unprotect(encrypted)


# ---------------------------------------------------------------------------
# Value decryption
# ---------------------------------------------------------------------------


def _pkcs7_unpad(data: bytes) -> bytes | None:
    if not data:
        return None
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[: len(data) - pad]
    return None


def _plausible(value: str) -> bool:
    """Reject wrong-key garbage that happens to decode as UTF-8."""
    return all(ord(char) >= 32 and ord(char) != 127 for char in value)


def _cbc_decrypt_once(key: bytes, payload: bytes) -> bytes | None:
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        decryptor = Cipher(
            algorithms.AES(key),
            modes.CBC(b" " * 16),
            backend=default_backend(),
        ).decryptor()
        padded = decryptor.update(payload) + decryptor.finalize()
    except Exception:
        return None
    return _pkcs7_unpad(padded)


def _finish(plain: bytes, meta_version: int) -> str | None:
    """Strip the >=24 hash prefix as needed and decode."""
    order = (
        (plain[32:], plain)
        if meta_version >= 24
        else (plain, plain[32:])
    )
    for candidate in order:
        try:
            text = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _plausible(text):
            return text
    return None


def _decrypt_cbc(
    payload: bytes,
    keys: list[bytes],
    meta_version: int,
) -> str | None:
    for key in keys:
        plain = _cbc_decrypt_once(key, payload)
        if plain is None:
            continue
        text = _finish(plain, meta_version)
        if text is not None:
            return text
    return None


def _decrypt_windows_value(
    encrypted: bytes,
    gcm_key: bytes | None,
    meta_version: int,
) -> str | None:
    version = encrypted[:3]
    payload = encrypted[3:]
    if version == b"v20":
        return None
    if version == b"v10" and gcm_key is not None:
        # AES-GCM: 12-byte nonce + ciphertext + 16-byte tag.
        if len(payload) <= 12 + 16:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            plain = AESGCM(gcm_key).decrypt(
                payload[:12], payload[12:], None
            )
        except Exception:
            return None
        return _finish(plain, meta_version)
    # Pre-v10 values are DPAPI blobs of the plaintext directly.
    if not version.startswith(b"v"):
        try:
            plain = _dpapi_unprotect(encrypted)
            return plain.decode("utf-8")
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Cookie-store readers
# ---------------------------------------------------------------------------


def _copy_db(db_path: Path) -> Path | None:
    """Copy a sqlite db (+WAL sidecars) so a locked browser won't bite."""
    temp_dir = Path(tempfile.mkdtemp(prefix="ytm-cookies-"))
    target = temp_dir / db_path.name
    for suffix in ("", "-wal", "-shm"):
        source = db_path.with_name(db_path.name + suffix)
        if source.exists():
            shutil.copy2(source, target.with_name(target.name + suffix))
    return target if target.exists() else None


def _read_meta_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'version'"
        ).fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.DatabaseError, ValueError, TypeError):
        return 0


def _firefox_cookies(profile_dir: Path) -> dict[str, str]:
    db_path = profile_dir / "cookies.sqlite"
    if not db_path.exists():
        raise BrowserError(f"No cookie store found in {profile_dir}.")
    copied = _copy_db(db_path)
    if copied is None:
        raise BrowserError(f"Could not read {db_path}.")
    try:
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT name, value, host FROM moz_cookies "
                "WHERE host LIKE ? OR host LIKE ?",
                _DOMAIN_FILTERS,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise BrowserError(
            f"Firefox cookie store could not be read ({error}). "
            "Close Firefox and try again."
        ) from error
    finally:
        shutil.rmtree(copied.parent, ignore_errors=True)

    cookies: dict[str, str] = {}
    for name, value, host in rows:
        if value is not None:
            cookies[name] = value
    return cookies


def _chromium_cookies(browser: str, profile: str | None) -> dict[str, str]:
    meta = _CHROMIUM_BROWSERS[browser]
    user_data_dir = _user_data_dir(browser)
    db_path = _cookie_db_path(browser, profile)
    if not db_path.exists():
        hint = ""
        if profile:
            hint = f" (profile {profile!r})"
        raise BrowserError(
            f"{meta['label']} cookie store not found at {db_path}{hint}. "
            "Is it installed, and is that the right profile?"
        )

    is_windows = platform.system() == "Windows"
    is_mac = platform.system() == "Darwin"

    # Linux/macOS: per-cookie AES-CBC key candidates. Windows: one
    # AES-GCM key (and DPAPI fallback handled per value). Keyring and
    # Local State access only happen when a cookie store exists.
    gcm_key: bytes | None = None
    keyring_keys: list[bytes] = []
    if is_windows:
        gcm_key = _windows_gcm_key(browser, user_data_dir)
    else:
        passwords = (
            [_keychain_password(*meta["keychain"])]
            if is_mac
            else _keyring_passwords(browser)
        )
        iterations, dklen = (
            (_MAC_ITERATIONS, _MAC_DKLEN)
            if is_mac
            else (_LINUX_ITERATIONS, _LINUX_DKLEN)
        )
        keyring_keys = [
            _pbkdf2(password, iterations, dklen)
            for password in passwords
            if password
        ]

    copied = _copy_db(db_path)
    if copied is None:
        raise BrowserError(f"Could not read {db_path}.")

    try:
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            meta_version = _read_meta_version(connection)
            rows = connection.execute(
                "SELECT name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE ? OR host_key LIKE ?",
                _DOMAIN_FILTERS,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise BrowserError(
            f"{meta['label']} cookie store could not be read ({error}). "
            f"Close {meta['label']} completely and try again."
        ) from error
    finally:
        shutil.rmtree(copied.parent, ignore_errors=True)

    empty_key = _pbkdf2(b"", _LINUX_ITERATIONS, _LINUX_DKLEN)
    peanuts_key = _pbkdf2(_PEANUTS, _LINUX_ITERATIONS, _LINUX_DKLEN)

    cookies: dict[str, str] = {}
    v20_count = 0
    failure_count = 0
    for name, value, encrypted_value in rows:
        raw = bytes(encrypted_value) if encrypted_value else b""
        plain: str | None
        if not raw:
            plain = value
        elif is_windows:
            plain = _decrypt_windows_value(raw, gcm_key, meta_version)
            if raw[:3] == b"v20":
                v20_count += 1
        else:
            version = raw[:3]
            payload = raw[3:]
            if version == b"v20":
                v20_count += 1
                plain = None
            elif version == b"v11" and not is_mac:
                plain = _decrypt_cbc(
                    payload, [*keyring_keys, empty_key], meta_version
                )
            elif version == b"v10":
                # v10 = fixed "peanuts" key (or an older keyring
                # password); empty-password values appear after
                # keyring changes.
                plain = _decrypt_cbc(
                    payload,
                    [peanuts_key, *keyring_keys, empty_key],
                    meta_version,
                )
            else:
                plain = None
        if plain is None:
            failure_count += 1
            continue
        cookies[name] = plain

    if not cookies:
        raise BrowserError(_no_usable_cookies_error(meta, v20_count, failure_count, is_mac))
    return cookies


def _no_usable_cookies_error(
    meta: dict[str, Any],
    v20_count: int,
    failure_count: int,
    is_mac: bool,
) -> str:
    if v20_count and v20_count == failure_count:
        return (
            f"{meta['label']} encrypts cookies with app-bound encryption "
            "(v20) which no outside app can read. Use Firefox, Edge, Brave "
            "or Opera instead, or paste the request headers manually."
        )
    if is_mac:
        return (
            f"Could not decrypt {meta['label']} cookies: the Keychain "
            f"password for \"{meta['keychain'][0]}\" is unavailable. Allow "
            "access and retry, or paste the request headers manually."
        )
    return (
        f"Could not decrypt {meta['label']} cookies: the key lives in your "
        "system login keyring (GNOME Keyring/KWallet) and could not be "
        "read. Unlock your login keyring, close "
        f"{meta['label']} completely and retry — or paste the request "
        "headers manually."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_MAX_PROFILE_PROBES = 4


def _chromium_profile_session(db_path: Path) -> bool | None:
    """Offline hint: does this store still carry a Google session?

    Presence only — an untouched profile can hold session cookies
    Google invalidated months ago (that is exactly why auto-detection
    validates candidates live before trusting them). None = unknown.
    """
    if not db_path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM cookies "
                "WHERE name = '__Secure-3PAPISID'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return None
    return bool(row and row[0])


def _firefox_profile_session(db_path: Path) -> bool | None:
    if not db_path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM moz_cookies "
                "WHERE name = '__Secure-3PAPISID'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return None
    return bool(row and row[0])


def list_profiles(browser: str) -> list[dict[str, Any]]:
    """Profiles of one browser: ``id``, display ``name``, offline
    ``session`` hint and ``last_used`` epoch of the cookie store.

    Sorted with session-bearing profiles first, most recently written
    first — the list feeds the profile pickers in both UIs.
    """
    browser = (browser or "").strip().lower()
    if browser not in _KNOWN_BROWSER_IDS:
        raise BrowserError(
            f"Unknown browser {browser!r}. Supported: "
            f"{', '.join(_KNOWN_BROWSER_IDS)}."
        )

    entries: list[dict[str, Any]] = []

    if browser == "firefox":
        seen: set[Path] = set()
        for root in _firefox_profile_roots():
            if not root.is_dir():
                continue
            for path in sorted(p for p in root.iterdir() if p.is_dir()):
                db = path / "cookies.sqlite"
                if path in seen or not db.exists():
                    continue
                seen.add(path)
                entries.append(
                    {
                        "id": path.name,
                        "name": path.name,
                        "session": _firefox_profile_session(db),
                        "last_used": int(db.stat().st_mtime),
                    }
                )
    else:
        root = _user_data_dir(browser)
        info_cache: dict[str, Any] = {}
        try:
            local_state = json.loads(
                (root / "Local State").read_text(encoding="utf-8")
            )
            info_cache = (
                local_state.get("profile", {}).get("info_cache", {}) or {}
            )
        except (OSError, ValueError, AttributeError):
            pass
        if root.is_dir():
            for path in sorted(p for p in root.iterdir() if p.is_dir()):
                db = path / "Cookies"
                if not db.exists():
                    continue
                info = info_cache.get(path.name, {})
                entries.append(
                    {
                        "id": path.name,
                        "name": info.get("name") or path.name,
                        "session": _chromium_profile_session(db),
                        "last_used": int(db.stat().st_mtime),
                    }
                )

    entries.sort(
        key=lambda entry: (entry["session"] is True, entry["last_used"]),
        reverse=True,
    )
    return entries


def header_is_live(cookie_header: str) -> bool | None:
    """Ask YouTube whether this cookie header still counts as a
    signed-in session (cheap HTML probe). None = could not tell."""
    try:
        import requests

        response = requests.get(
            "https://music.youtube.com/",
            headers={
                "cookie": cookie_header,
                "user-agent": _DEFAULT_UA,
                "accept": "text/html",
            },
            timeout=15,
        )
        match = re.search(r'"LOGGED_IN":\s*(true|false)', response.text)
    except Exception:
        return None
    if not match:
        return None
    return match.group(1) == "true"


def _profile_is_live(browser: str, profile: str) -> bool | None:
    try:
        return header_is_live(extract_cookie_header(browser, profile))
    except Exception:
        return None


def auto_profile(browser: str) -> str | None:
    """Choose the profile most likely to hold a *live* YouTube session.

    Session-bearing profiles are tried newest-first until YouTube
    confirms ``LOGGED_IN`` — an untouched ``Default`` with stale
    cookies is skipped in favour of the profile actually being used.
    A single candidate is trusted without probing, so one-profile
    setups and offline environments never touch the network. Firefox
    keeps its classic default-profile behaviour (returns None).
    """
    browser = (browser or "").strip().lower()
    if browser not in _KNOWN_BROWSER_IDS or browser == "firefox":
        return None

    candidates = [
        entry
        for entry in list_profiles(browser)
        if entry["session"] is True
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["id"]
    for candidate in candidates[:_MAX_PROFILE_PROBES]:
        if _profile_is_live(browser, candidate["id"]) is True:
            return candidate["id"]
    # Nothing proved live (offline?): fall back to the best offline
    # hint so the caller still gets a concrete attempt + clear error.
    return candidates[0]["id"]


def list_browsers() -> list[dict[str, Any]]:
    """Return every supported browser with availability and profiles."""
    found: list[dict[str, Any]] = []

    for browser_id, meta in _CHROMIUM_BROWSERS.items():
        profiles = list_profiles(browser_id)
        found.append(
            {
                "id": browser_id,
                "label": meta["label"],
                "available": bool(profiles),
                "profiles": profiles,
            }
        )

    firefox_profiles = list_profiles("firefox")
    found.append(
        {
            "id": "firefox",
            "label": _FIREFOX["label"],
            "available": bool(firefox_profiles),
            "profiles": firefox_profiles,
        }
    )
    return found


def extract_cookie_header(browser: str, profile: str | None = None) -> str:
    """Build the `Cookie:` header for youtube.com/google.com cookies."""
    browser = (browser or "").strip().lower()

    if browser not in _KNOWN_BROWSER_IDS:
        raise BrowserError(
            f"Unknown browser {browser!r}. Supported: "
            f"{', '.join(_KNOWN_BROWSER_IDS)}."
        )

    if browser == "firefox":
        profile_dir = _firefox_profile_dir(profile)
        if profile_dir is None:
            raise BrowserError(
                "Firefox profiles were not found. Is Firefox installed, "
                "and is that the right profile name?"
            )
        cookies = _firefox_cookies(profile_dir)
    else:
        if profile is None:
            profile = auto_profile(browser)
        cookies = _chromium_cookies(browser, profile)

    if not cookies:
        raise BrowserError(
            "No YouTube or Google cookies were found. Sign in to "
            "youtube.com in the browser first, then retry."
        )
    if _REQUIRED_SAPISID_COOKIE not in cookies:
        raise BrowserError(
            f"Google session cookies were found but {_REQUIRED_SAPISID_COOKIE} "
            "is missing — the browser may be signed out of Google. Sign in "
            "to youtube.com in that browser, then retry."
        )
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def headers_raw_for(browser: str, profile: str | None = None) -> str:
    """Build a DevTools-style request-headers block for create_account().

    The block is deliberately shaped like the headers a real
    music.youtube.com /browse request carries, so it flows through the
    exact same ytmusicapi.setup() validation as the manual paste path.
    ytmusicapi recomputes the SAPISIDHASH authorization header on every
    request, so the placeholder here never goes stale.
    """
    cookie_header = extract_cookie_header(browser, profile)
    lines = [
        f"cookie: {cookie_header}",
        "x-goog-authuser: 0",
        "authorization: SAPISIDHASH 0",
        "x-origin: https://music.youtube.com",
        "origin: https://music.youtube.com",
        "x-youtube-client-name: 1",
        f"x-youtube-client-version: {_YOUTUBE_CLIENT_VERSION}",
        f"user-agent: {_DEFAULT_UA}",
        "accept: */*",
        "content-type: application/json",
    ]
    return "\n".join(lines)
