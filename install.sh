#!/usr/bin/env bash
set -euo pipefail
# Code Forge installer — and updater. This script is the dependency
# updater, not just a first installer: every run first prints what
# needs updating (system packages, the Python packages that
# requirements.txt pins, Ollama), then installs what is missing and
# upgrades what is outdated. Run it again any time to bring an
# existing install current; re-running is safe.
#   ./install.sh --list-only   Read-only: print the update listing
#                              and exit without changing anything.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }

# Ollama floor: models on the roster (e.g. lfm3) need Ollama 0.40.13
# or newer just to load, so anything older is flagged in the listing
# below. install.sh only ever CHECKS Ollama — installing or updating
# it stays a manual step from https://ollama.com/download.
OLLAMA_MIN_VERSION="0.40.13"

LIST_ONLY=0
[ "${1:-}" = "--list-only" ] && LIST_ONLY=1

# System dependencies the Forge shells out to or links against:
# git (snapshots, checkpoints, the Git panel), python3-venv (the venv
# below cannot be created without it), and the runtime libraries
# PySide6 links (Qt GUI + the QtMultimedia microphone). Install only
# what is actually missing; if that fails (no sudo/apt), say exactly
# what to install by hand and carry on — the Python side still lands.
APT_DEPS=(git python3-venv libegl1 libgl1 libxkbcommon0 libpulse0)
# Sweep R165: the probes live in a function so the END of the run
# can ask the same question again — "still missing after the install
# step" used to be invisible behind the success line.
probe_missing() {
  MISSING=()
  command -v git >/dev/null || MISSING+=(git)
  python3 -c "import ensurepip" 2>/dev/null || MISSING+=(python3-venv)
  ldconfig -p 2>/dev/null | grep -q "libEGL.so.1" || MISSING+=(libegl1)
  ldconfig -p 2>/dev/null | grep -q "libGL.so.1" || MISSING+=(libgl1)
  ldconfig -p 2>/dev/null | grep -q "libxkbcommon.so.0" || MISSING+=(libxkbcommon0)
  ldconfig -p 2>/dev/null | grep -q "libpulse.so.0" || MISSING+=(libpulse0)
}
probe_missing

# ------------------------------------------------------------------
# Update listing (read-only — this section changes nothing)
# ------------------------------------------------------------------
echo "== Checking for updates =="
SYS_OUTDATED=()
echo "System packages:"
if command -v dpkg-query >/dev/null && command -v apt-cache >/dev/null; then
  for pkg in "${APT_DEPS[@]}"; do
    installed="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)"
    [ -n "$installed" ] || continue
    candidate="$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    [ -n "$candidate" ] && [ "$candidate" != "(none)" ] || continue
    if dpkg --compare-versions "$installed" lt "$candidate"; then
      SYS_OUTDATED+=("$pkg")
      echo "  $pkg: $installed -> $candidate"
    fi
  done
  [ ${#SYS_OUTDATED[@]} -eq 0 ] && echo "  all up to date"
else
  echo "  (dpkg/apt not available — skipped)"
fi
[ ${#MISSING[@]} -gt 0 ] && echo "  (not installed yet: ${MISSING[*]} — the install step below handles those)"

VENV_EXISTED=0
[ -x "$ROOT/.venv/bin/python" ] && VENV_EXISTED=1
PY_OUTDATED=()
echo "Python packages (requirements.txt):"
if [ "$VENV_EXISTED" -eq 1 ]; then
  REQ_LIST="$(grep -oE '^[A-Za-z0-9._-]+' "$ROOT/requirements.txt" 2>/dev/null | tr 'A-Z' 'a-z' | sed 's/[-_.]/-/g' | tr '\n' ' ' || true)"
  PIP_OK=0
  OUTDATED_RAW="$("$ROOT/.venv/bin/python" -m pip list --outdated 2>/dev/null)" && PIP_OK=1 || PIP_OK=0
  if [ "$PIP_OK" -eq 1 ]; then
    while read -r pname pver platest _prest; do
      [ -n "${pname:-}" ] || continue
      case "$pname" in Package|---*) continue ;; esac
      norm="$(printf '%s' "$pname" | tr 'A-Z' 'a-z' | sed 's/[-_.]/-/g')"
      for req in $REQ_LIST; do
        if [ "$req" = "$norm" ]; then
          PY_OUTDATED+=("$pname")
          echo "  $pname: $pver -> $platest"
          break
        fi
      done
    done <<< "$OUTDATED_RAW"
    [ ${#PY_OUTDATED[@]} -eq 0 ] && echo "  all up to date"
  else
    echo "  (could not check online — no network?)"
  fi
else
  echo "  (no environment yet — the first install below creates it)"
fi

echo "Ollama:"
OLLAMA_STATE="ok"
if command -v ollama >/dev/null; then
  OLLAMA_VER="$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  if [ -z "$OLLAMA_VER" ]; then
    echo "  installed, but its version could not be read (minimum: $OLLAMA_MIN_VERSION)"
  elif dpkg --compare-versions "$OLLAMA_VER" lt "$OLLAMA_MIN_VERSION"; then
    OLLAMA_STATE="old"
    echo "  version $OLLAMA_VER is BELOW the minimum $OLLAMA_MIN_VERSION — update it from https://ollama.com/download (install.sh checks Ollama but never installs or updates it)"
  else
    echo "  version $OLLAMA_VER (minimum $OLLAMA_MIN_VERSION) — OK"
  fi
else
  OLLAMA_STATE="missing"
  echo "  not installed — get it from https://ollama.com/download (install.sh never installs Ollama itself)"
fi

if [ ${#SYS_OUTDATED[@]} -eq 0 ] && [ ${#PY_OUTDATED[@]} -eq 0 ] && [ "$OLLAMA_STATE" = "ok" ]; then
  echo "Everything is up to date — nothing needs updating."
fi

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "List-only mode: nothing was changed."
  exit 0
fi

# ------------------------------------------------------------------
# Install what is missing, upgrade what is outdated. Non-fatal, the
# same discipline as always: on failure print the exact manual
# command and carry on.
# ------------------------------------------------------------------
SYS_UPDATED=0
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "Missing system dependencies: ${MISSING[*]}"
fi
if [ ${#MISSING[@]} -gt 0 ] || [ ${#SYS_OUTDATED[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null && command -v sudo >/dev/null; then
    if sudo apt-get update -qq; then
      if [ ${#MISSING[@]} -gt 0 ]; then
        sudo apt-get install -y "${MISSING[@]}" || \
          echo "Could not install them automatically — run: sudo apt install ${MISSING[*]}" >&2
      fi
      if [ ${#SYS_OUTDATED[@]} -gt 0 ]; then
        if sudo apt-get install -y --only-upgrade "${SYS_OUTDATED[@]}"; then
          SYS_UPDATED=1
        else
          echo "Could not update them automatically — run: sudo apt install --only-upgrade ${SYS_OUTDATED[*]}" >&2
        fi
      fi
    else
      [ ${#MISSING[@]} -gt 0 ] && echo "Could not install them automatically — run: sudo apt install ${MISSING[*]}" >&2
      [ ${#SYS_OUTDATED[@]} -gt 0 ] && echo "Could not update them automatically — run: sudo apt install --only-upgrade ${SYS_OUTDATED[*]}" >&2
      true
    fi
  else
    [ ${#MISSING[@]} -gt 0 ] && echo "Install them with your package manager: ${MISSING[*]}" >&2
    [ ${#SYS_OUTDATED[@]} -gt 0 ] && echo "Update them with your package manager: ${SYS_OUTDATED[*]}" >&2
    true
  fi
fi

PY_UPDATED=0
# Sweep R164: a venv whose interpreter exists but whose pip is
# broken (an interrupted upgrade, a half-deleted .venv) used to flow
# straight through — `python3 -m venv` on an existing folder does
# not repair it, every pip step then failed softly, and run.sh died
# later with a confusing import error. Probe the health; a broken
# environment is rebuilt from scratch (and a rebuild takes the
# first-install path below).
if [ "$VENV_EXISTED" -eq 1 ] && ! "$ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "The existing Python environment looks broken (its pip does not run) — rebuilding it from scratch."
  rm -rf "$ROOT/.venv"
  VENV_EXISTED=0
fi
python3 -m venv "$ROOT/.venv"
if [ "$VENV_EXISTED" -eq 1 ]; then
  # Update run on an existing install: refresh pip, then upgrade
  # what requirements.txt pins. Non-fatal — a failed upgrade prints
  # the manual command and the plain install still runs, so the
  # environment at least satisfies requirements.txt.
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip || \
    echo "Could not upgrade pip automatically — run: $ROOT/.venv/bin/python -m pip install --upgrade pip" >&2
  if "$ROOT/.venv/bin/python" -m pip install --upgrade -r "$ROOT/requirements.txt"; then
    PY_UPDATED=1
  else
    echo "Could not upgrade the Python packages automatically — run: $ROOT/.venv/bin/python -m pip install --upgrade -r $ROOT/requirements.txt" >&2
    "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt" || \
      echo "Could not satisfy requirements.txt either — run: $ROOT/.venv/bin/python -m pip install -r $ROOT/requirements.txt" >&2
  fi
else
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
fi
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/linux-forge-agent-shell.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=CODE FORGE v2 — Linux Forge Agent
Comment=Local Ollama-powered desktop agent
Exec="$ROOT/run.sh"
Path=$ROOT
Icon=utilities-terminal
Terminal=false
Categories=Development;Utility;
DESKTOP
chmod +x "$ROOT/run.sh" "$ROOT/install.sh"
command -v ollama >/dev/null || echo "NOTE: Ollama is not installed — the Forge needs it running locally. Get it from https://ollama.com/download and start it before launching."
if [ "$VENV_EXISTED" -eq 1 ]; then
  echo "Update summary:"
  if [ ${#SYS_OUTDATED[@]} -gt 0 ]; then
    if [ "$SYS_UPDATED" -eq 1 ]; then
      echo "  system packages updated: ${SYS_OUTDATED[*]}"
    else
      echo "  system packages NOT updated: ${SYS_OUTDATED[*]} (manual command above)"
    fi
  fi
  if [ "$PY_UPDATED" -eq 1 ]; then
    echo "  Python packages upgraded to current (requirements.txt)"
  else
    echo "  Python packages NOT upgraded (manual command above)"
  fi
  if [ "$OLLAMA_STATE" = "old" ]; then
    echo "  Ollama still needs a manual update: https://ollama.com/download"
  fi
fi
# Sweep R165: the final word on system packages — re-probed AFTER
# the install step. Anything still missing gets a loud warning with
# the consequence and the exact manual command, instead of the run
# ending on a success line the machine cannot back up. (The exit
# code stays 0: the carry-on discipline is deliberate — the Python
# side may be perfectly usable, and the warning says what is not.)
probe_missing
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "WARNING: these system packages are STILL missing: ${MISSING[*]}" >&2
  echo "Without them the Forge window will fail to open (Qt cannot load without its libraries) and git features stay off." >&2
  echo "Install them by hand with: sudo apt install ${MISSING[*]}" >&2
fi
echo "Installed dependencies and desktop launcher. Start with: $ROOT/run.sh"
