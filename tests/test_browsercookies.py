"""Unit tests for browsercookies: detection, cookie-store readers and
Chromium value decryption, using synthetic cookie databases.

Chromium-family tests are Linux-marked where they depend on the
"peanuts" fallback key derivation; the Windows DPAPI branch is tested
anywhere by stubbing the unprotect call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import browsercookies


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Keep tests hermetic: never touch the dev machine's session
    keyring/D-Bus. Individual tests override with monkeypatch."""
    monkeypatch.setattr(browsercookies, "_keyring_passwords", lambda browser: [])


# ---------------------------------------------------------------------------
# Helpers: synthetic Firefox / Chromium cookie stores
# ---------------------------------------------------------------------------


def _make_firefox_profile(home: Path, cookies: list[tuple[str, str, str]]) -> Path:
    profile = home / ".mozilla" / "firefox" / "abc123.default-release"
    profile.mkdir(parents=True)
    db = profile / "cookies.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, "
        "path TEXT, expiry INTEGER)"
    )
    for name, value, host in cookies:
        connection.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, '/', 9999999999)",
            (name, value, host),
        )
    connection.commit()
    connection.close()
    return profile


def _pbkdf2(password: bytes, iterations: int, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, dklen)


def _aes_cbc_encrypt(key: bytes, plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    encryptor = Cipher(
        algorithms.AES(key), modes.CBC(b" " * 16)
    ).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _make_chromium(
    home: Path,
    browser_dir: str,
    key: bytes,
    rows: list[tuple[str, str, str, bytes]],
    local_state_key: bytes | None = None,
    meta_version: int | None = None,
) -> Path:
    """Create a synthetic chromium user-data dir.

    rows: (host_key, name, value, encrypted_value)
    local_state_key: raw os_crypt key material (with v10 prefix added).
    meta_version: when set, writes the Cookies `meta` table version.
    """
    user_data = home / ".config" / browser_dir
    (user_data / "Default").mkdir(parents=True)
    state = {
        "os_crypt": {
            "encrypted_key": base64.b64encode(
                b"v10" + (local_state_key or key)
            ).decode()
        }
    }
    (user_data / "Local State").write_text(json.dumps(state))
    db = user_data / "Default" / "Cookies"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB)"
    )
    if meta_version is not None:
        connection.execute(
            "CREATE TABLE meta (key TEXT UNIQUE NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO meta VALUES ('version', ?)", (str(meta_version),)
        )
    for host_key, name, value, encrypted in rows:
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            (host_key, name, value, encrypted),
        )
    connection.commit()
    connection.close()
    return user_data


def _make_profile(
    browser_root: Path,
    profile: str,
    rows: list[tuple[str, str, str, bytes]],
    mtime: int | None = None,
) -> Path:
    """Create a bare profile dir with a Cookies store under a chromium
    user-data root (profiles beyond the helper's fixed `Default`)."""
    profile_dir = browser_root / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    db = profile_dir / "Cookies"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB)"
    )
    for host_key, name, value, encrypted in rows:
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?)",
            (host_key, name, value, encrypted),
        )
    connection.commit()
    connection.close()
    if mtime is not None:
        os.utime(db, (mtime, mtime))
    return profile_dir


GOOD_COOKIES = [
    ("__Secure-3PAPISID", "papisid-value-123", "youtube.com"),
    ("SOCS", "CAI", "youtube.com"),
    ("VISITOR_INFO1_LIVE", "visitor-abc", ".youtube.com"),
]


# ---------------------------------------------------------------------------
# Firefox
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_firefox_extracts_only_youtube_and_google_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_firefox_profile(
        tmp_path,
        GOOD_COOKIES
        + [("session", "unrelated-value", "example.com")],
    )

    header = browsercookies.extract_cookie_header("firefox")
    assert "__Secure-3PAPISID=papisid-value-123" in header
    assert "SOCS=CAI" in header
    assert "unrelated-value" not in header

    raw = browsercookies.headers_raw_for("firefox")
    assert "cookie: " in raw
    assert "x-goog-authuser: 0" in raw
    assert "authorization: SAPISIDHASH 0" in raw
    assert "x-origin: https://music.youtube.com" in raw


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_firefox_missing_sapisid_gives_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_firefox_profile(tmp_path, [("SOCS", "CAI", "youtube.com")])

    with pytest.raises(browsercookies.BrowserError, match="__Secure-3PAPISID"):
        browsercookies.extract_cookie_header("firefox")


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_firefox_no_youtube_cookies_gives_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_firefox_profile(tmp_path, [("session", "x", "example.com")])

    with pytest.raises(browsercookies.BrowserError, match="No YouTube or Google"):
        browsercookies.extract_cookie_header("firefox")


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_firefox_absent_gives_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(browsercookies.BrowserError, match="Firefox profiles"):
        browsercookies.extract_cookie_header("firefox")


# ---------------------------------------------------------------------------
# Chromium family: key derivation + value decryption
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="peanuts fallback is linux")
def test_chromium_v10_peanuts_decrypt(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    key = _pbkdf2(b"peanuts", 1, 16)
    encrypted = b"v10" + _aes_cbc_encrypt(key, b"secret-papisid-value")

    _make_chromium(
        tmp_path,
        "google-chrome",
        key,
        rows=[
            ("youtube.com", "SOCS", "plain-value", b""),
            (".google.com", "__Secure-3PAPISID", "", encrypted),
        ],
    )

    header = browsercookies.extract_cookie_header("chrome")
    assert "SOCS=plain-value" in header
    assert "__Secure-3PAPISID=secret-papisid-value" in header


def _aes_gcm_encrypt(key: bytes, plaintext: bytes, nonce: bytes = b"123456789012") -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return b"v10" + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def test_chromium_windows_gcm_branch(monkeypatch, tmp_path):
    """The Windows branch: DPAPI-unprotect the Local State key, then
    AES-GCM-decrypt v10 values. DPAPI itself is stubbed (win32-only)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(browsercookies.platform, "system", lambda: "Windows")
    gcm_key = b"k" * 32
    monkeypatch.setattr(browsercookies, "_dpapi_unprotect", lambda data: gcm_key)

    encrypted = _aes_gcm_encrypt(gcm_key, b"win-secret")

    # Same chromium layout, but under the (patched) LOCALAPPDATA root.
    user_data = tmp_path / "AppData" / "Google" / "Chrome" / "User Data"
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Local State").write_text(
        json.dumps(
            {
                "os_crypt": {
                    # Windows Local State keys carry the DPAPI suffix.
                    "encrypted_key": base64.b64encode(
                        b"v10" + b"DPAPI" + b"wrapped-blob"
                    ).decode()
                }
            }
        )
    )
    db = user_data / "Default" / "Cookies"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB)"
    )
    connection.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?)",
        (".google.com", "__Secure-3PAPISID", "", encrypted),
    )
    connection.commit()
    connection.close()

    header = browsercookies.extract_cookie_header("chrome")
    assert "__Secure-3PAPISID=win-secret" in header


@pytest.mark.skipif(sys.platform != "linux", reason="keyring model is Linux")
def test_chromium_v11_keyring_and_hash_prefix(monkeypatch, tmp_path):
    """Chrome 155+ real-world shape: v11 cookies keyed by the login
    keyring secret, meta schema >= 24 (32-byte hash prefix on every
    decrypted value), and Local State with only os_crypt.portal — no
    encrypted_key at all."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        browsercookies, "_keyring_passwords", lambda browser: [b"keyring-pass"]
    )

    key = _pbkdf2(b"keyring-pass", 1, 16)
    digest = hashlib.sha256(b"synthetic-cookie-integrity").digest()  # 32 B
    encrypted = b"v11" + _aes_cbc_encrypt(key, digest + b"secret-papisid")

    _make_chromium(
        tmp_path,
        "google-chrome",
        key,
        rows=[(".google.com", "__Secure-3PAPISID", "", encrypted)],
        meta_version=24,
    )
    # Portal-era Local State: no encrypted_key anywhere.
    (tmp_path / ".config" / "google-chrome" / "Local State").write_text(
        json.dumps({"os_crypt": {"portal": {"prev_desktop": "GNOME"}}})
    )

    header = browsercookies.extract_cookie_header("chrome")
    assert "__Secure-3PAPISID=secret-papisid" in header


@pytest.mark.skipif(sys.platform != "linux", reason="keyring model is Linux")
def test_chromium_v11_without_keyring_gives_clear_error(monkeypatch, tmp_path):
    """Unreadable keyring -> actionable message, never a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # autouse fixture already forces _keyring_passwords -> []

    key = _pbkdf2(b"some-other-password", 1, 16)
    encrypted = b"v11" + _aes_cbc_encrypt(key, b"undecryptable")

    _make_chromium(
        tmp_path,
        "google-chrome",
        key,
        rows=[(".google.com", "__Secure-3PAPISID", "", encrypted)],
    )

    with pytest.raises(browsercookies.BrowserError, match="login keyring"):
        browsercookies.extract_cookie_header("chrome")


def test_chromium_v20_only_cookies_give_clear_error(monkeypatch, tmp_path):
    """App-bound (v20) encryption must produce the actionable message
    pointing at other browsers or manual paste."""
    monkeypatch.setenv("HOME", str(tmp_path))
    key = _pbkdf2(b"peanuts", 1, 16)

    _make_chromium(
        tmp_path,
        "google-chrome",
        key,
        rows=[
            (".google.com", "__Secure-3PAPISID", "", b"v20" + b"opaque-blob"),
            ("youtube.com", "SOCS", "", b"v20" + b"opaque-blob-2"),
        ],
    )

    with pytest.raises(browsercookies.BrowserError, match="app-bound"):
        browsercookies.extract_cookie_header("chrome")


def test_unknown_browser_is_rejected(tmp_path):
    with pytest.raises(browsercookies.BrowserError, match="Unknown browser"):
        browsercookies.extract_cookie_header("netscape")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_list_browsers_structure():
    entries = browsercookies.list_browsers()
    ids = {entry["id"] for entry in entries}
    assert ids == {
        "chrome",
        "chromium",
        "edge",
        "brave",
        "opera",
        "vivaldi",
        "firefox",
    }
    for entry in entries:
        assert set(entry) == {"id", "label", "available", "profiles"}
        assert isinstance(entry["available"], bool)
        assert isinstance(entry["profiles"], list)
        for profile in entry["profiles"]:
            assert set(profile) == {"id", "name", "session", "last_used"}
            assert profile["session"] in (True, False, None)
            assert isinstance(profile["last_used"], int)


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_list_browsers_detects_chrome_under_fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_chromium(
        tmp_path,
        "google-chrome",
        _pbkdf2(b"peanuts", 1, 16),
        rows=[("youtube.com", "SOCS", "CAI", b"")],
    )

    browsers = {b["id"]: b for b in browsercookies.list_browsers()}
    assert browsers["chrome"]["available"] is True
    assert browsers["firefox"]["available"] is False
    assert browsers["edge"]["available"] is False
    # The profiles of the fake browser are listed.
    chrome_profiles = {p["id"] for p in browsers["chrome"]["profiles"]}
    assert chrome_profiles == {"Default"}


# ---------------------------------------------------------------------------
# Profile discovery & auto-detection
# ---------------------------------------------------------------------------

_SESSION_ROW = [(".google.com", "__Secure-3PAPISID", "", b"")]


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_list_profiles_orders_sessions_by_recency(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".config" / "google-chrome"
    # Explicit, well-separated activity times (mtimes are read at
    # int-second granularity, creation order alone can tie).
    _make_profile(root, "Profile 1", _SESSION_ROW, mtime=1_000)
    _make_profile(root, "Profile 2", [], mtime=2_000)  # no Google session
    _make_profile(root, "Profile 3", _SESSION_ROW, mtime=3_000)

    entries = browsercookies.list_profiles("chrome")
    ids = [entry["id"] for entry in entries]
    assert ids == ["Profile 3", "Profile 1", "Profile 2"]

    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["Profile 3"]["session"] is True
    assert by_id["Profile 1"]["session"] is True
    assert by_id["Profile 2"]["session"] is False
    assert by_id["Profile 3"]["name"] == "Profile 3"
    assert all(isinstance(e["last_used"], int) for e in entries)


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_auto_profile_probes_newest_until_live(monkeypatch, tmp_path):
    """Auto-detection validates candidates newest-first and stops at
    the first profile YouTube confirms as signed in — a stale newer
    profile must never beat a live older one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".config" / "google-chrome"
    _make_profile(root, "Profile 1", _SESSION_ROW, mtime=1_000)  # older, live
    _make_profile(root, "Profile 2", _SESSION_ROW, mtime=2_000)  # newer, out

    probed: list[str] = []

    def fake_live(browser: str, profile: str) -> bool:
        probed.append(profile)
        return profile == "Profile 1"

    monkeypatch.setattr(browsercookies, "_profile_is_live", fake_live)
    assert browsercookies.auto_profile("chrome") == "Profile 1"
    assert probed == ["Profile 2", "Profile 1"]


@pytest.mark.skipif(sys.platform != "linux", reason="HOME layout is linux")
def test_auto_profile_single_candidate_and_no_session(monkeypatch, tmp_path):
    """One candidate is trusted without a network probe (offline and
    test environments stay hermetic); no session-bearing profile at
    all yields None, and Firefox keeps its classic behaviour."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_profile(
        tmp_path / ".config" / "google-chrome",
        "Default",
        _SESSION_ROW,
    )
    _make_profile(
        tmp_path / ".config" / "BraveSoftware" / "Brave-Browser",
        "Default",
        [],  # installed but signed out
    )

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("a single candidate must not be probed")

    monkeypatch.setattr(browsercookies, "_profile_is_live", must_not_probe)
    assert browsercookies.auto_profile("chrome") == "Default"
    assert browsercookies.auto_profile("brave") is None
    assert browsercookies.auto_profile("firefox") is None
