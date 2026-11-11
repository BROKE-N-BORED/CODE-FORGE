"""Central secret scrubbing (S2, audit findings S1-05/16/28).

One scrubber, used where Forge persists text — the event log
(forge/eventlog.py) and the conversation store (forge/storage.py) —
so a secret that ever enters a prompt, a tool argument, a tool
result, or a file read does not live forever in the logs, the
conversation database, and every export made from them.

Two passes, both cheap and both run only over text already being
serialised:

1. Exact-value replacement: the current values of the vault's
   SECRET_CONFIG_KEYS + the sudo password (read through the vault's
   own in-RAM cache) and of any same-named fields in a config dict
   the caller passes in are replaced with ``[redacted]``.
2. A conservative pattern pass: ``api_key`` / ``token`` /
   ``password`` followed by ``:`` or ``=`` and a value has the value
   replaced.  Ordinary prose is untouched — the tests pin that text
   with no configured secrets and no pattern match passes through
   byte-identical.

Fail-soft by contract: any scrub problem returns the input
unchanged.  Scrubbing must never break logging or saving, and it
never writes the values it hunts anywhere.
"""
from __future__ import annotations

import re

from . import vault as vault_mod

REDACTED = "[redacted]"

#: Values shorter than this are not exact-match scrubbed — a 1–3
#: character "token" would redact half the dictionary.  (The
#: pattern pass below still catches such a value when it appears
#: in an explicit key=value shape.)
_MIN_SECRET_LEN = 4

_PATTERN = re.compile(
    r"((?:api[_-]?key|token|password)\s*[:=]\s*)\S+",
    re.IGNORECASE)

# Cache for the vault-derived values, keyed by the vault files'
# (mtime, size) stamp — the same stamp the vault's own cache uses,
# so a steady-state turn pays one stat pair, no file reads.
_vault_values_cache: dict = {}


def _vault_values(app_dir=None) -> tuple:
    """The vault's current secret values (names only from
    SECRET_CONFIG_KEYS + the sudo password), cached by file stamp."""
    try:
        vault = vault_mod._shared_vault(
            app_dir or vault_mod.default_app_dir())
        stamp = vault._stamp()
    except Exception:
        return ()
    cached = _vault_values_cache.get("entry")
    if cached is not None and cached[0] == stamp \
            and cached[1] == str(vault.app_dir):
        return cached[2]
    values = []
    for name in (*vault_mod.SECRET_CONFIG_KEYS,
                 vault_mod.SUDO_PASSWORD_NAME):
        try:
            value = vault.get(name)
        except Exception:
            value = None
        if value and len(value) >= _MIN_SECRET_LEN:
            values.append(value)
    result = tuple(values)
    _vault_values_cache["entry"] = (stamp, str(vault.app_dir), result)
    return result


def secret_values(config: dict | None = None, app_dir=None) -> tuple:
    """All exact values to hunt: the vault's, plus any secret fields
    present in a caller-supplied config dict (pre-migration setups
    whose keys still live in the dict)."""
    values = list(_vault_values(app_dir))
    if isinstance(config, dict):
        for name in (*vault_mod.SECRET_CONFIG_KEYS,
                     vault_mod.SUDO_PASSWORD_NAME):
            value = config.get(name)
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN \
                    and value not in values:
                values.append(value)
    # Longest first, so a value that is a prefix of another cannot
    # half-redact the longer one.
    return tuple(sorted(values, key=len, reverse=True))


def _scrub_text_with(text, values):
    out = text
    for value in values:
        if value in out:
            out = out.replace(value, REDACTED)
    return _PATTERN.sub(r"\1" + REDACTED, out)


def _scrub_obj_with(obj, values):
    if isinstance(obj, str):
        return _scrub_text_with(obj, values)
    if isinstance(obj, dict):
        return {k: _scrub_obj_with(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj_with(v, values) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_obj_with(v, values) for v in obj)
    return obj


def scrub_text(text, config: dict | None = None, app_dir=None):
    """Scrub one string.  Non-strings pass through untouched, and
    any failure returns the input exactly as given."""
    if not isinstance(text, str) or not text:
        return text
    try:
        return _scrub_text_with(text, secret_values(config, app_dir))
    except Exception:
        return text


def scrub_obj(obj, config: dict | None = None, app_dir=None):
    """Recursively scrub every string in a JSON-shaped structure
    (dicts, lists, tuples, strings).  Other values pass through.
    Fail-soft: any failure returns the input object unchanged.  The
    secret-value list is computed once for the whole structure."""
    try:
        return _scrub_obj_with(obj, secret_values(config, app_dir))
    except Exception:
        return obj
