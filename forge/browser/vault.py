"""The browser vault — encrypted storage for site logins.

Joshua's order (2026-11-11): the browser needs somewhere for
private passwords and account information, and it needs to be
secure. Rules this module keeps:

* NOTHING is stored in plaintext. The whole entries file (sites,
  usernames, passwords, notes) is encrypted as one blob.
* The key comes from a master passphrase the user chooses, stretched
  with PBKDF2-HMAC-SHA256 (600,000 rounds, random salt). The
  passphrase itself is never written anywhere; it lives only in the
  memory of the process that opened the vault.
* Encryption is encrypt-then-MAC on standard-library primitives:
  an HMAC-SHA256 keystream (random nonce per save) for the data and
  a separate HMAC-SHA256 key authenticating salt+nonce+ciphertext.
  A wrong passphrase or a single changed byte fails the MAC check
  and the vault refuses to open — it never returns partial data.
* The vault file is written with 0600 permissions in a 0700 folder.
* Passwords never appear in return values meant for a model, in
  CLI output (listing shows site + username only), or in logs.
  BrowserSession.fill_login is the one path from vault to page,
  and it reports only how many fields it filled.

Honest scope: this protects the file at rest — someone holding the
vault file without the passphrase gets nothing. It cannot protect
against malware already running as the user while the vault is
open; no local vault can.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time

from .errors import BrowserError

_KDF_ROUNDS = 600_000


def default_vault_path():
    override = os.environ.get("FORGE_BROWSER_VAULT", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".forge", "browser-vault.json")


def _derive_keys(passphrase, salt):
    material = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, _KDF_ROUNDS, dklen=64)
    return material[:32], material[32:]


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + struct.pack(">Q", counter),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _xor(data, stream):
    return bytes(a ^ b for a, b in zip(data, stream))


class Vault:
    """An opened vault. Construct via Vault.create / Vault.open."""

    def __init__(self, path, passphrase, entries):
        self._path = path
        self._passphrase = passphrase
        self._entries = entries

    # -- file handling --------------------------------------------------
    @classmethod
    def create(cls, path, passphrase):
        if os.path.exists(path):
            raise BrowserError(f"a vault already exists at {path} — open it instead")
        if len(passphrase) < 8:
            raise BrowserError("the vault passphrase needs at least 8 characters")
        vault = cls(path, passphrase, {})
        vault.save()
        return vault

    @classmethod
    def open(cls, path, passphrase):
        if not os.path.exists(path):
            raise BrowserError(f"no vault at {path} — create one first")
        with open(path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        try:
            salt = base64.b64decode(blob["salt"])
            nonce = base64.b64decode(blob["nonce"])
            data = base64.b64decode(blob["data"])
            mac = base64.b64decode(blob["mac"])
        except Exception as exc:
            raise BrowserError("the vault file is damaged (unreadable fields)") from exc
        enc_key, mac_key = _derive_keys(passphrase, salt)
        expected = hmac.new(mac_key, salt + nonce + data, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, mac):
            raise BrowserError("wrong passphrase, or the vault file has been changed")
        plain = _xor(data, _keystream(enc_key, nonce, len(data)))
        try:
            payload = json.loads(plain.decode("utf-8"))
        except Exception as exc:
            raise BrowserError("the vault file is damaged (unreadable contents)") from exc
        return cls(path, passphrase, payload.get("entries", {}))

    def save(self):
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        enc_key, mac_key = _derive_keys(self._passphrase, salt)
        plain = json.dumps({"entries": self._entries}).encode("utf-8")
        data = _xor(plain, _keystream(enc_key, nonce, len(plain)))
        mac = hmac.new(mac_key, salt + nonce + data, hashlib.sha256).digest()
        blob = {"app": "forge-browser-vault", "version": 1,
                "salt": base64.b64encode(salt).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "data": base64.b64encode(data).decode(),
                "mac": base64.b64encode(mac).decode()}
        folder = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(folder, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(blob, handle)

    # -- entries -----------------------------------------------------------
    def set(self, site, username, password, notes=""):
        self._entries[site] = {"username": username, "password": password,
                               "notes": notes,
                               "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.save()

    def get(self, site):
        """The entry for a site, or None. FOR LOCAL USE — the caller
        must not print or forward the password."""
        return self._entries.get(site)

    def delete(self, site):
        if site in self._entries:
            del self._entries[site]
            self.save()
            return True
        return False

    def list_entries(self):
        """Site + username + notes only. Passwords are never listed."""
        return [{"site": site, "username": entry.get("username", ""),
                 "notes": entry.get("notes", ""), "updated": entry.get("updated", "")}
                for site, entry in sorted(self._entries.items())]


def fill_from_vault(session, vault, site, submit=False):
    """Fill ``site``'s login on the session's current page.

    Returns {"site":..., "filled_fields":...} — never the values.
    """
    entry = vault.get(site)
    if entry is None:
        raise BrowserError(f"the vault has no entry for {site}")
    outcome = session.fill_login(entry.get("username", ""), entry["password"],
                                 submit=submit)
    return {"site": site, **outcome}
