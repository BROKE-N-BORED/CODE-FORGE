"""Pre-load process check (2026-11-15): before Forge loads a local
model into RAM, it looks for heavy programs already running that
would fight the model for memory, and offers to close the ones the
owner picks. /proc only, stdlib only: no new dependencies, no Qt.

The scan ranks by resident memory (RAM is the scarce resource on
an 8 GB PC; a 2B model needs ~2-3 GB of it) and shows CPU share
too. Only killable candidates are ever listed: user-owned, not
Forge itself or its children, not Ollama (killing the model host
to load a model makes no sense), not PID 1, no kernel threads,
and nothing on the denylist of session essentials whose death
logs the owner out or breaks the display or sound. When in doubt
a process is NOT listed. The owner can always close it himself
from the system monitor; Forge only offers the safe ones.

CPU% is the process's share of the WHOLE machine's CPU (the same
scale as the header gauge, which shows load divided by threads):
a single-threaded program maxing one core on a 4-thread PC reads
25%, not 100%.
"""

import os
import signal
import time
from collections import namedtuple

# A candidate must hold at least this much RAM to be worth the
# owner's attention: on an 8 GB PC, anything under 200 MB is noise
# next to a 2-3 GB model. Listing it would only bury the real
# hogs (a browser, a game, another model server).
PROC_KILL_FLOOR_BYTES = 200 * 1024 * 1024
# The owner asked for the top 5 or 6. Six: the roomier end.
PROC_KILL_MAX = 6
# Seconds between the two CPU samples the scan takes.
PROC_CPU_SAMPLE_SECONDS = 0.1

# Session essentials: killing one of these logs the owner out,
# kills the display, or breaks sound. Never listed, whatever they
# weigh. Matched against the process name (comm, truncated to 15
# chars by the kernel) and the argv[0] basename; a 15-char comm
# that is a prefix of a denylisted name counts too, since comm
# truncates long names ("gnome-keyring-d" is gnome-keyring-daemon).
PROC_DENYLIST = frozenset([
    # Display + session (Cinnamon/Mint first, generics after).
    "Xorg", "Xwayland", "cinnamon", "cinnamon-session", "muffin",
    "gnome-shell", "gnome-session", "lightdm", "gdm", "gdm3",
    "sddm", "polkitd", "at-spi-bus-launcher", "at-spi2-registryd",
    "dconf-service", "upowerd", "udisksd", "NetworkManager",
    "wpa_supplicant", "ModemManager", "bluetoothd", "colord",
    "cupsd", "avahi-daemon", "systemd", "dbus-daemon",
    "dbus-broker", "dbus-broker-launch", "rtkit-daemon",
    "accounts-daemon", "geoclue",
    # Sound.
    "pulseaudio", "pipewire", "pipewire-pulse", "wireplumber",
    # Keyring.
    "gnome-keyring-daemon",
    # The model host itself: killing Ollama to load a model
    # makes no sense.
    "ollama",
])
# Prefixes of the same families (settings daemons, gvfs, the
# systemd children): a name starting with one of these is a
# session essential too.
PROC_DENYLIST_PREFIXES = ("csd-", "gvfsd", "gnome-keyring",
                          "systemd-", "at-spi")

ProcessInfo = namedtuple(
    "ProcessInfo", ["pid", "name", "rss_bytes", "cpu_percent", "uid"])


def format_bytes(nbytes):
    """1,289,046,528 -> "1.2 GB"; 850 MB stays "850 MB"."""
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.1f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / 1024 ** 2:.0f} MB"
    return f"{nbytes / 1024:.0f} KB"


def _page_size():
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4096


def _is_denied(comm, argv0):
    """True when a process name belongs to a session essential."""
    for name in (comm or "", argv0 or ""):
        if not name:
            continue
        if name in PROC_DENYLIST:
            return True
        if name.startswith(PROC_DENYLIST_PREFIXES):
            return True
        # comm truncates at 15 chars: a full-length comm that is
        # a prefix of a denylisted name is that program.
        if len(name) == 15 and any(d.startswith(name)
                                   for d in PROC_DENYLIST):
            return True
    return False


def _parse_stat(data):
    """Parse one /proc/<pid>/stat line. Returns (comm, ppid, utime,
    stime, rss_pages) or None when the line is unusable. comm may
    hold spaces and parens, so it is taken between the first '('
    and the LAST ')'."""
    try:
        open_paren = data.index("(")
        close_paren = data.rindex(")")
        comm = data[open_paren + 1:close_paren]
        rest = data[close_paren + 1:].split()
        # After comm: state(0) ppid(1) ... utime(11) stime(12)
        # ... rss(21), 0-based in this list.
        ppid = int(rest[1])
        utime = int(rest[11])
        stime = int(rest[12])
        rss_pages = int(rest[21])
    except (ValueError, IndexError):
        return None
    return comm, ppid, utime, stime, rss_pages


def _read_uid(status_path):
    """The real UID from /proc/<pid>/status, or None."""
    try:
        with open(status_path, encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_argv0(cmdline_path):
    """(argv0 basename, empty?) from /proc/<pid>/cmdline. An empty
    cmdline means a kernel thread (or a zombie), nothing a user
    program can be."""
    try:
        with open(cmdline_path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return "", True
    if not raw:
        return "", True
    first = raw.split(b"\x00")[0].decode("utf-8", errors="replace")
    return os.path.basename(first), False


def _read_total_jiffies(proc_dir):
    """Total CPU jiffies across all cores, from /proc/stat."""
    try:
        with open(os.path.join(proc_dir, "stat"), encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                if line.startswith("cpu "):
                    return sum(int(f) for f in line.split()[1:])
    except (OSError, ValueError):
        return 0
    return 0


def read_proc_snapshot(proc_dir="/proc"):
    """One read of the process table: ({pid: record}, total
    jiffies). A record holds comm, argv0, uid, ppid, rss_bytes,
    and ticks (utime + stime). Processes that vanish mid-read, or
    whose files cannot be read, are skipped. The table is a
    moving target and a miss is not an error."""
    page_size = _page_size()
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return {}, 0
    procs = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        base = os.path.join(proc_dir, entry)
        try:
            with open(os.path.join(base, "stat"), encoding="utf-8",
                      errors="replace") as handle:
                parsed = _parse_stat(handle.read())
        except OSError:
            continue
        if parsed is None:
            continue
        comm, ppid, utime, stime, rss_pages = parsed
        argv0, cmdline_empty = _read_argv0(
            os.path.join(base, "cmdline"))
        procs[pid] = {
            "comm": comm,
            "argv0": argv0,
            "uid": _read_uid(os.path.join(base, "status")),
            "ppid": ppid,
            "rss_bytes": rss_pages * page_size,
            "ticks": utime + stime,
            "cmdline_empty": cmdline_empty,
        }
    return procs, _read_total_jiffies(proc_dir)


def cpu_percent(prev_ticks, cur_ticks, prev_total, cur_total):
    """A process's share of the whole machine's CPU between two
    samples, 0..100. Any degenerate sample (no time passed, a
    process born between samples) reads 0.0, never an error."""
    proc_delta = cur_ticks - prev_ticks
    total_delta = cur_total - prev_total
    if proc_delta <= 0 or total_delta <= 0:
        return 0.0
    return min(100.0, 100.0 * proc_delta / total_delta)


def _is_descendant_of(pid, ancestor, ppid_map):
    """True when pid's parent chain reaches ancestor. A visited
    set guards against a corrupted map looping."""
    seen = set()
    current = pid
    while current and current not in seen:
        seen.add(current)
        current = ppid_map.get(current, 0)
        if current == ancestor:
            return True
    return False


def scan_heavy_processes(proc_dir="/proc", own_pid=None, uid=None,
                         floor_bytes=PROC_KILL_FLOOR_BYTES,
                         limit=PROC_KILL_MAX,
                         sample_seconds=PROC_CPU_SAMPLE_SECONDS):
    """The killable heavyweights, heaviest first, at most `limit`.
    Two samples `sample_seconds` apart give each candidate a live
    CPU share; 0 takes one sample and reports 0.0 for CPU (the
    tests' shape). Every exclusion is applied here, so the caller
    can list the result as-is."""
    if own_pid is None:
        own_pid = os.getpid()
    if uid is None:
        uid = os.getuid()
    first, total_first = read_proc_snapshot(proc_dir)
    if sample_seconds and sample_seconds > 0:
        time.sleep(sample_seconds)
        second, total_second = read_proc_snapshot(proc_dir)
    else:
        second, total_second = first, total_first
    ppid_map = {pid: rec["ppid"] for pid, rec in second.items()}
    found = []
    for pid, rec in second.items():
        if pid <= 1 or pid == own_pid:
            continue
        if _is_descendant_of(pid, own_pid, ppid_map):
            continue
        if rec["uid"] != uid:
            continue
        if rec["cmdline_empty"]:
            continue
        if rec["rss_bytes"] < floor_bytes:
            continue
        if _is_denied(rec["comm"], rec["argv0"]):
            continue
        prev = first.get(pid)
        cpu = cpu_percent(prev["ticks"], rec["ticks"],
                          total_first, total_second) \
            if prev is not None else 0.0
        found.append(ProcessInfo(
            pid=pid,
            name=rec["argv0"] or rec["comm"],
            rss_bytes=rec["rss_bytes"],
            cpu_percent=cpu,
            uid=rec["uid"]))
    found.sort(key=lambda info: info.rss_bytes, reverse=True)
    return found[:limit]


def _pid_alive(pid):
    """True when the pid still exists (signal 0: no signal sent)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_identity(proc_dir, pid):
    """(comm, argv0, uid) for one pid, or None when it is gone."""
    base = os.path.join(proc_dir, str(pid))
    try:
        with open(os.path.join(base, "stat"), encoding="utf-8",
                  errors="replace") as handle:
            parsed = _parse_stat(handle.read())
    except OSError:
        return None
    if parsed is None:
        return None
    comm = parsed[0]
    argv0, _empty = _read_argv0(os.path.join(base, "cmdline"))
    return comm, argv0, _read_uid(os.path.join(base, "status"))


def kill_processes(pids, proc_dir="/proc", own_pid=None, uid=None,
                   term_wait=2.0, kill_wait=1.0,
                   kill_fn=None, alive_fn=None):
    """Close the given PIDs: SIGTERM, a brief wait, SIGKILL only
    for what is still alive. Every pid is re-validated at kill
    time: still there, still the owner's, still not a session
    essential, still not Forge itself. So a stale or tampered
    list cannot kill the wrong thing (defense in depth: the
    window already filtered, and the kill re-checks).

    Returns {"killed": [...], "refused": {pid: reason},
    "still_running": [...], "gone": [...]}. `kill_fn` /
    `alive_fn` are the seams the tests drive; production uses
    os.kill and signal 0."""
    if own_pid is None:
        own_pid = os.getpid()
    if uid is None:
        uid = os.getuid()
    if kill_fn is None:
        kill_fn = os.kill
    if alive_fn is None:
        alive_fn = _pid_alive
    result = {"killed": [], "refused": {}, "still_running": [],
              "gone": []}
    doomed = []
    for pid in pids:
        if not isinstance(pid, int) or pid <= 1 or pid == own_pid:
            result["refused"][pid] = "not a program Forge closes"
            continue
        identity = _read_identity(proc_dir, pid)
        if identity is None:
            result["gone"].append(pid)
            continue
        comm, argv0, proc_uid = identity
        if proc_uid != uid:
            result["refused"][pid] = "it belongs to another user"
            continue
        if _is_denied(comm, argv0):
            result["refused"][pid] = "it keeps the session running"
            continue
        doomed.append(pid)
    if not doomed:
        return result
    for pid in doomed:
        try:
            kill_fn(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    deadline = time.monotonic() + term_wait
    alive = list(doomed)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if alive_fn(pid)]
        if alive:
            time.sleep(0.1)
    for pid in alive:
        try:
            kill_fn(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    deadline = time.monotonic() + kill_wait
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if alive_fn(pid)]
        if alive:
            time.sleep(0.1)
    result["killed"] = [pid for pid in doomed if pid not in alive]
    result["still_running"] = alive
    return result
