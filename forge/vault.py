"""Encrypted local secrets vault for Code Forge (GUIFX V1 + S2).

Forge stores the owner's API keys and PC (sudo) password in an
encrypted vault instead of plaintext config:

* ``vault.json`` in the app dir holds one encrypted blob per named
  secret.  ``names()`` lists names only — a stored value is only ever
  handed to the exact code path that needs it (a provider request
  header, or the stdin of an approved sudo command), never to the
  model, a log, or an error message.
* ``vault.key`` beside it holds the random 32-byte vault key
  (mode 0600 on posix).  Honest posture: this protects against casual
  reading and accidental leaks of the config files.  It is not a
  defence against someone who already has full access to the owner's
  files — the real gate is that only owner-approved actions ever use
  the secrets.

Cipher: the deployment venv has no ``cryptography`` package, so the
vault uses a stdlib construction — an HMAC-SHA256 keystream (counter
mode) XORed with the plaintext, with an HMAC-SHA256 integrity tag,
verified in constant time before decryption.  Tampered or corrupt
data raises :class:`VaultError`; the vault never silently returns
wrong data.  Exception messages never contain secret values.

S2 upgrades over V1:
* ``vault.json`` is written mode 0600 (born private, like the key).
* Entry format v2 binds the entry NAME into the integrity tag (tag
  over name+nonce+ciphertext), so a blob copied onto another entry
  no longer decrypts.  V1 entries (unversioned, name-unbound) still
  verify the old way and are re-encrypted to v2 the next time the
  vault file is written (any set/delete, or the migration pass).
* (key, entries) are cached in RAM, keyed by the (mtime, size) of
  ``vault.json`` + ``vault.key``: a repeated ``get()`` on an
  unchanged vault does not touch the disk again.  Any write
  invalidates via the changed stamp.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

VAULT_FILE = "vault.json"
KEY_FILE = "vault.key"
KEY_BYTES = 32

#: File-format marker written at the top level of vault.json.
VAULT_FORMAT_VERSION = 2

#: Prefix marking a v2 (name-bound) entry blob.  V1 blobs are bare
#: base64 with no prefix.
_ENTRY_V2_PREFIX = "v2:"

#: Connector-config fields that are secrets.  They live in the vault,
#: never in connectors.json (see migrate_connector_secrets).
SECRET_CONFIG_KEYS = ("groq_api_key", "gemini_api_key", "exaone_api_key",
                      "github_token", "hf_token")

#: The vault entry holding the owner's PC (sudo) password.
SUDO_PASSWORD_NAME = "sudo_password"


class VaultError(RuntimeError):
    """The vault is unreadable, corrupt, or failed its integrity
    check.  Messages name the problem, never a secret value."""


def default_app_dir() -> Path:
    """The app dir main.py uses (kept in step with its APP_DIR)."""
    return Path.home() / ".local" / "share" / "linux-forge-agent-shell"


def _hmac(key: bytes, *parts: bytes) -> bytes:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(part)
    return mac.digest()


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(_hmac(enc_key, nonce, counter.to_bytes(8, "big")))
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _stat_stamp(path: Path):
    """(mtime_ns, size) for path, or None when it does not exist.
    The cache key for the in-RAM (key, entries) cache."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class Vault:
    """An encrypted name -> secret store rooted at one app dir."""

    def __init__(self, app_dir):
        self.app_dir = Path(app_dir)
        self.vault_path = self.app_dir / VAULT_FILE
        self.key_path = self.app_dir / KEY_FILE
        self._lock = threading.RLock()
        # In-RAM cache (S2): the parsed entries + derived subkeys,
        # valid only while _cache_stamp matches the files on disk.
        self._cache_stamp = None
        self._cache_entries: dict | None = None
        self._cache_subkeys: tuple[bytes, bytes] | None = None
        # The key-file stamp the cached subkeys were derived from.
        # Kept separate from _cache_stamp on purpose: if the key
        # file is deleted/replaced externally, the entries cache
        # refreshing must NOT adopt subkeys derived from the old
        # key (that once let a deleted key keep decrypting).
        self._cache_subkey_stamp = None

    # -- cache plumbing -------------------------------------------------
    def _stamp(self):
        return (_stat_stamp(self.vault_path),
                _stat_stamp(self.key_path))

    def _cache_valid(self) -> bool:
        return (self._cache_entries is not None
                and self._cache_stamp == self._stamp())

    def _drop_cache(self) -> None:
        self._cache_stamp = None
        self._cache_entries = None
        self._cache_subkeys = None
        self._cache_subkey_stamp = None

    # -- key handling ---------------------------------------------------
    def _read_key_bytes_raw(self) -> bytes:
        """The raw vault-key file read (factored out so the cache —
        and its tests — have one disk-touching point)."""
        return self.key_path.read_bytes()

    def _load_key(self, create: bool) -> bytes:
        try:
            key = self._read_key_bytes_raw()
        except FileNotFoundError:
            if not create:
                raise VaultError(
                    "vault key file is missing — the vault cannot "
                    "be read") from None
            key = os.urandom(KEY_BYTES)
            self.app_dir.mkdir(parents=True, exist_ok=True)
            # O_EXCL + explicit mode: the key is born 0600, never
            # briefly world-readable.
            fd = os.open(self.key_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(self.key_path, 0o600)
            return key
        except OSError as exc:
            raise VaultError(f"vault key file is unreadable: {exc}") \
                from None
        if len(key) != KEY_BYTES:
            raise VaultError("vault key file has the wrong length — "
                             "the vault cannot be read")
        return key

    def _subkeys(self, create: bool) -> tuple[bytes, bytes]:
        with self._lock:
            if (not create and self._cache_subkeys is not None
                    and self._cache_subkey_stamp
                    == _stat_stamp(self.key_path)):
                return self._cache_subkeys
            key = self._load_key(create)
            subkeys = (_hmac(key, b"forge-vault-enc"),
                       _hmac(key, b"forge-vault-mac"))
            self._cache_subkeys = subkeys
            self._cache_subkey_stamp = _stat_stamp(self.key_path)
            return subkeys

    # -- file handling ----------------------------------------------------
    def _read_entries_raw(self) -> dict:
        """The raw vault-file read + parse (the cache's other single
        disk-touching point)."""
        try:
            raw = self.vault_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise VaultError(f"vault file is unreadable: {exc}") \
                from None
        try:
            data = json.loads(raw)
            entries = data["entries"]
            if not isinstance(entries, dict):
                raise ValueError("entries is not an object")
            return {str(k): str(v) for k, v in entries.items()}
        except Exception:
            raise VaultError("vault file is corrupt — refusing to "
                             "read it") from None

    def _read_entries(self) -> dict:
        with self._lock:
            if self._cache_valid():
                return dict(self._cache_entries)
            entries = self._read_entries_raw()
            self._cache_entries = entries
            self._cache_stamp = self._stamp()
            return dict(entries)

    def _write_entries(self, entries: dict) -> None:
        with self._lock:
            # Any write is a migration pass: v1 entries are
            # re-encrypted to v2 (name-bound) so old vaults upgrade
            # themselves without a separate tool.
            upgraded = {}
            for name, blob in entries.items():
                if blob.startswith(_ENTRY_V2_PREFIX):
                    upgraded[name] = blob
                else:
                    value, _is_current = self._decrypt_blob(name, blob)
                    upgraded[name] = self._encrypt(name, value)
            self.app_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": VAULT_FORMAT_VERSION, "entries": upgraded},
                indent=2, sort_keys=True)
            tmp = self.vault_path.with_suffix(".json.tmp")
            # Born 0600 via os.open (S2): the temp file — and, through
            # the replace, vault.json itself — is never briefly
            # world-readable the way a default write_text would be.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            tmp.replace(self.vault_path)
            if os.name == "posix":
                os.chmod(self.vault_path, 0o600)
            self._cache_entries = dict(upgraded)
            self._cache_stamp = self._stamp()

    # -- crypto -----------------------------------------------------------
    def _encrypt(self, name: str, value: str) -> str:
        enc_key, mac_key = self._subkeys(create=True)
        nonce = os.urandom(16)
        plain = value.encode("utf-8")
        cipher = _xor(plain, _keystream(enc_key, nonce, len(plain)))
        # v2: the entry NAME is part of the tag, so a blob only ever
        # verifies under its own name.
        tag = _hmac(mac_key, name.encode("utf-8"), nonce, cipher)
        return _ENTRY_V2_PREFIX + base64.b64encode(
            nonce + tag + cipher).decode("ascii")

    def _decrypt_blob(self, name: str, blob: str) -> tuple[str, bool]:
        """(value, is_current_format) for one stored blob.  Raises
        VaultError on tamper/corruption — never wrong data."""
        is_current = blob.startswith(_ENTRY_V2_PREFIX)
        encoded = blob[len(_ENTRY_V2_PREFIX):] if is_current else blob
        try:
            raw = base64.b64decode(encoded.encode("ascii"),
                                   validate=True)
        except Exception:
            raise VaultError("a vault entry is not valid encoded "
                             "data — the vault may be corrupt") from None
        if len(raw) < 48:
            raise VaultError("a vault entry is truncated — the vault "
                             "may be corrupt")
        nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
        enc_key, mac_key = self._subkeys(create=False)
        if is_current:
            expected = _hmac(mac_key, name.encode("utf-8"), nonce,
                             cipher)
        else:
            # V1 verification: the tag covered nonce+ciphertext only.
            expected = _hmac(mac_key, nonce, cipher)
        if not hmac.compare_digest(tag, expected):
            raise VaultError("a vault entry failed its integrity "
                             "check — the vault may have been "
                             "tampered with")
        plain = _xor(cipher, _keystream(enc_key, nonce, len(cipher)))
        try:
            return plain.decode("utf-8"), is_current
        except UnicodeDecodeError:
            raise VaultError("a vault entry did not decode — the "
                             "vault may be corrupt") from None

    def _decrypt(self, name: str, blob: str) -> str:
        return self._decrypt_blob(name, blob)[0]

    # -- public API ---------------------------------------------------------
    @staticmethod
    def _check_name(name) -> str:
        name = str(name or "")
        if not name:
            raise VaultError("vault entry name is empty")
        return name

    def has(self, name: str) -> bool:
        return self._check_name(name) in self._read_entries()

    def names(self) -> list[str]:
        """Stored entry names only — never values."""
        return sorted(self._read_entries())

    def get(self, name: str) -> str | None:
        """The secret for name, or None when nothing is stored.
        Raises VaultError on corrupt/tampered data."""
        name = self._check_name(name)
        blob = self._read_entries().get(name)
        if blob is None:
            return None
        return self._decrypt(name, blob)

    def set(self, name: str, value: str) -> None:
        name = self._check_name(name)
        entries = self._read_entries()
        entries[name] = self._encrypt(name, str(value))
        self._write_entries(entries)

    def delete(self, name: str) -> bool:
        name = self._check_name(name)
        entries = self._read_entries()
        if name not in entries:
            return False
        del entries[name]
        self._write_entries(entries)
        return True

    def upgrade_entries(self) -> bool:
        """Re-encrypt any v1 entries to v2 in place.  True when the
        vault file was rewritten.  Idempotent; a vault with only v2
        (or no) entries is left untouched."""
        entries = self._read_entries()
        if all(blob.startswith(_ENTRY_V2_PREFIX)
               for blob in entries.values()):
            return False
        self._write_entries(entries)
        return True


#: Shared Vault instances for the fail-soft lookups, so the in-RAM
#: cache survives across get_secret() calls (each call used to build
#: a throwaway Vault and re-read the disk every time).
_shared_vaults: dict[str, Vault] = {}
_shared_lock = threading.Lock()


def _shared_vault(app_dir) -> Vault:
    key = str(Path(app_dir))
    with _shared_lock:
        vault = _shared_vaults.get(key)
        if vault is None:
            vault = Vault(app_dir)
            _shared_vaults[key] = vault
        return vault


def get_secret(name: str, app_dir=None) -> str | None:
    """Fail-soft lookup for hot paths (provider key reads, the sudo
    check): any vault problem reads as "not stored", so a damaged
    vault degrades to the old behaviour instead of breaking a turn.
    Explicit Vault use still raises VaultError."""
    try:
        return _shared_vault(app_dir or default_app_dir()).get(name)
    except Exception:
        return None


def extract_secrets(app_dir, config: dict) -> dict:
    """Move any SECRET_CONFIG_KEYS present in a config dict into the
    vault and return a cleaned copy without them.  Raises VaultError
    if a vault write fails — callers must then keep the original
    dict (and file) untouched."""
    present = [k for k in SECRET_CONFIG_KEYS
               if isinstance(config, dict) and config.get(k)]
    if not present:
        return dict(config or {})
    vault = Vault(app_dir)
    for key in present:
        vault.set(key, str(config[key]))
    cleaned = dict(config)
    for key in SECRET_CONFIG_KEYS:
        cleaned.pop(key, None)
    return cleaned


def plaintext_secret_keys(app_dir, config: dict | None = None) -> list[str]:
    """The SECRET_CONFIG_KEYS still sitting in plaintext in
    connectors.json (S2 no-plaintext-left assertion).  With no config
    given, the on-disk file is re-read — that is the point: what is
    checked is what an attacker would read.  An unreadable or absent
    file holds no plaintext and returns []."""
    if config is None:
        try:
            config = json.loads(
                (Path(app_dir) / "connectors.json").read_text(
                    encoding="utf-8"))
        except Exception:
            return []
    if not isinstance(config, dict):
        return []
    return [k for k in SECRET_CONFIG_KEYS if config.get(k)]


def migrate_connector_secrets(app_dir, config: dict | None = None) -> dict:
    """One-way, idempotent migration of plaintext keys out of
    connectors.json into the vault.  Returns the config to use in
    memory (secret fields removed once they are safely vaulted).

    The file is rewritten WITHOUT the secret fields only after the
    vault writes succeed; if anything fails, connectors.json is left
    exactly as it was and the original config is returned, so the
    keys keep working the old way rather than being lost."""
    app_dir = Path(app_dir)
    path = app_dir / "connectors.json"
    if config is None:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    if not isinstance(config, dict):
        return {}
    if not any(config.get(k) for k in SECRET_CONFIG_KEYS):
        # Nothing to move — but a pre-S2 vault may still hold v1
        # entries; upgrade them in place (quietly, fail-soft).
        try:
            Vault(app_dir).upgrade_entries()
        except Exception:
            pass
        return config
    try:
        cleaned = extract_secrets(app_dir, config)
    except Exception:
        return config
    try:
        from . import fileperms
        fileperms.write_text_private(
            path, json.dumps(cleaned, indent=2), encoding="utf-8")
    except Exception:
        # The vault holds the keys and provider lookup consults it
        # first, so a failed rewrite loses nothing; the next launch
        # retries the file cleanup.
        pass
    return cleaned
