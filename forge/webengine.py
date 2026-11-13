"""Forge Browser behind the Browse seam — the Research web-engine
option (browser merge, 2026-11-11).

The Browse seam (forge/browse.py + plugins/browse/plugin.py) has
exactly one page-access dispatch, and Research mode rides it by
TOOL NAME (browse_open / browse_read): the worker's budget meter
and Sources-consulted list count those names, whichever engine
serves them. This module is the second engine: when the owner's
Settings → Behavior → "Research web engine" choice names
Forge Browser, the plugin's open / read / screenshot verbs are
served here by the built-in forge/browser package (keyless search
and fetch, a local CDP engine for rendering) instead of the
external Browse MCP server.

What does NOT change with the engine choice — the seam's fences
are engine-independent and the plugin applies them before either
engine is touched:

* the per-project site allowlist (.forge/browse.json) decides
  which hosts may be navigated to, under both engines;
* a wall (login / captcha / paywall) HOLDS the session under both
  engines: the package's own barrier detection feeds the same
  hold state forge/browse.py keeps, and browse_hand_back — the
  owner's approval at the existing gate — is the release;
* results are text, always. A screenshot is a file under
  .forge/artifacts/, named in the result, never image data.

What the package engine does NOT serve: clicking and typing.
Those are acting verbs on a live page; the package's session is a
reader here, so under this engine they answer in plain words and
point at the connector — Research never calls them (its fence
offers open / read only), and nothing silently pretends to act.

Failure words: a page that could not be read answers starting
"Tool error", the established prefix the Research budget meter
already treats as "never reached a page" — a failed open neither
counts against the run's budget nor joins Sources consulted.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import browse as browse_mod
from . import projects as proj
from . import recordings as recordings_mod
from .browser import fetch as _pkg_fetch
from .browser.barriers import detect_barriers
from .browser.errors import BrowserError
from .browser.render import screenshot as _pkg_screenshot
from .browser.verify import verify_in_browser as _pkg_verify

ENGINE_CONNECTOR = "browse"
ENGINE_PACKAGE = "forge-browser"
_CDP_ENGINES = ("auto", "chromium", "lightpanda")
_READ_CHARS = 12000  # the seam's read slice (fetch's own default)


# ---------- the owner's choices, as the shell hands them over ----------

def engine_for(ctx) -> str:
    """The Research web engine the plugin context names:
    ENGINE_PACKAGE when the owner chose Forge Browser, the
    connector otherwise (an unset or unrecognized value keeps
    today's behavior exactly)."""
    value = str(getattr(ctx, "research_web_engine", "") or "")
    return ENGINE_PACKAGE if value.strip().lower() == ENGINE_PACKAGE \
        else ENGINE_CONNECTOR


def cdp_engine_for(ctx) -> str:
    """The CDP engine Forge Browser renders with ("auto" unless
    the owner picked Chromium or Lightpanda in Settings)."""
    value = str(getattr(ctx, "forge_browser_engine", "auto") or "auto")
    return value if value in _CDP_ENGINES else "auto"


# ---------- transports (the seams the tests drive) ----------

def _fetch_page(url, check=None):
    return _pkg_fetch(url, max_chars=_READ_CHARS, check=check)


def _session_screenshot(url, path, engine):
    return _pkg_screenshot(url, path, engine=engine)


def _package_verify(target, workspace, engine):
    return _pkg_verify(target, workspace, engine=engine)


# ---------- the open page, per workspace ----------

_pages: dict[str, dict] = {}


def open_page(workspace, url, ctx) -> str:
    """browse_open, served by the package. The plugin has already
    run the allowlist fence on the URL itself; this fetches,
    re-running the SAME fence (allowlist + private-IP + https-only,
    from the same .forge/browse.json the plugin read) on every
    redirect hop before it is followed — a redirect to a private
    address or an unlisted host is refused, never fetched (S5-fix,
    audit W3) — remembers the page for browse_read /
    browse_screenshot, scans it for walls (a human wall sets the
    seam's hold, exactly as a server-reported wall does), and
    returns the page's first slice as text."""
    hold = browse_mod.get_hold(workspace)
    if hold is not None:
        return browse_mod.hold_refusal("browse_open", hold)
    allowlist = browse_mod.load_allowlist(workspace)
    try:
        page = _fetch_page(
            url,
            check=lambda target: browse_mod.check_open(
                target, allowlist))
    except BrowserError as exc:
        return f"Tool error: the page could not be read: {exc}"
    except Exception as exc:  # a transport surprise names itself
        return f"Tool error: the page could not be read: {exc}"
    _pages[str(workspace)] = {
        "url": page.final_url or url, "title": page.title or "",
        "markdown": page.markdown or "",
        "total_chars": page.total_chars or 0,
        "truncated": bool(page.truncated)}
    lines = [f"Opened {page.final_url or url}"
             + (f" — {page.title}" if page.title else "")
             + " (Forge Browser engine: plain fetch, unrendered).",
             "", page.markdown or "(the page returned no readable text)"]
    if page.truncated:
        lines += ["", f"(First {_READ_CHARS} of {page.total_chars} "
                      "characters — browse_read returns this same "
                      "slice; fetch the page directly for the rest.)"]
    barriers = detect_barriers(page.markdown or "", url)
    for barrier in barriers:
        if barrier.get("needs_human"):
            browse_mod.set_hold(workspace, barrier["kind"],
                                barrier["note"])
            if ctx is not None and getattr(ctx, "log", None):
                ctx.log(f"Browse: {barrier['kind']} wall detected by "
                        "Forge Browser — session HELD until the "
                        "owner continues.")
            lines += ["", browse_mod.handoff_notice(barrier["kind"])]
            break
        lines += ["", f"Note: {barrier['note']}"]
    return "\n".join(lines)


def read_page(workspace) -> str:
    """browse_read, served by the package: the open page's text
    slice, as stored by open_page."""
    hold = browse_mod.get_hold(workspace)
    if hold is not None:
        return browse_mod.hold_refusal("browse_read", hold)
    stored = _pages.get(str(workspace))
    if stored is None:
        return ("Tool error: no page is open in this project yet — "
                "browse_open a page first, then browse_read.")
    lines = [f"Page: {stored['url']}"
             + (f" — {stored['title']}" if stored["title"] else ""),
             "", stored["markdown"] or "(the page returned no "
             "readable text)"]
    if stored["truncated"]:
        lines += ["", f"(First {_READ_CHARS} of "
                      f"{stored['total_chars']} characters.)"]
    return "\n".join(lines)


def screenshot_page(workspace, ctx) -> str:
    """browse_screenshot, served by the package: a one-shot engine
    session on the open page, the PNG filed under
    .forge/artifacts/ and named — never attached as data."""
    hold = browse_mod.get_hold(workspace)
    if hold is not None:
        return browse_mod.hold_refusal("browse_screenshot", hold)
    stored = _pages.get(str(workspace))
    if stored is None:
        return ("Tool error: no page is open in this project yet — "
                "browse_open a page first, then browse_screenshot.")
    folder = proj.forge_area(workspace) / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"browse-{stamp}.png"
    counter = 1
    while path.exists():
        counter += 1
        path = folder / f"browse-{stamp}-{counter}.png"
    try:
        _session_screenshot(stored["url"], str(path),
                            cdp_engine_for(ctx))
    except BrowserError as exc:
        return f"Screenshot failed: {exc}"
    except Exception as exc:
        return f"Screenshot failed: {exc}"
    return (f"Screenshot saved to .forge/artifacts/{path.name} — "
            "the owner can open it from the project folder. "
            "(Browse results are text: the screenshot is never "
            "attached to this conversation as image data.)")


def acting_refusal(verb: str) -> str:
    """browse_click / browse_type under the package engine: plain
    words, no pretending. Acting on a live page rides the
    connector; this engine reads."""
    return (f"Browse {verb} is not served by the Forge Browser "
            "engine: it opens and reads pages (and verifies them), "
            "it does not click or type. Clicking and typing ride "
            "the Browse connector — switch Settings → Behavior → "
            "Research web engine back to \"Browse connector\" to "
            "use them.")


# ---------- verify, served by the package ----------

def verify_page(workspace, target, engine="auto") -> str:
    """verify_in_browser on the package's own engine (its #65
    contract: throwaway loopback preview, traversal refused, text
    back, screenshot filed). Composes the same shape of result
    the connector loop returns, and files the session's RECORDING
    artifact — a refused or failed attempt files its recording
    too: proof of the attempt is still proof."""
    steps = [("Verify requested", f"target: {target}")]
    try:
        result = _package_verify(target, workspace, engine)
    except BrowserError as exc:
        steps.append(("Refused", str(exc)))
        recordings_mod.file_recording(
            workspace, target, steps, [], "refused",
            engine_label="Forge Browser (built-in package)")
        return str(exc)
    except Exception as exc:
        steps.append(("Failed", str(exc)))
        recordings_mod.file_recording(
            workspace, target, steps, [], "failed",
            engine_label="Forge Browser (built-in package)")
        return f"Verify failed: {exc}"
    steps.append(("Served and opened",
                  f"{result.get('url', '')} (project preview)"))
    rendered = bool(result.get("rendered"))
    steps.append(("Read the page",
                  "rendered by the browser engine" if rendered
                  else "plain read — no engine available"))
    if result.get("snapshot"):
        steps.append(("Accessibility snapshot", "captured"))
    shot = result.get("screenshot")
    shots = []
    if shot:
        shots.append(Path(str(shot)).name)
        steps.append(("Screenshot", f"saved as {shots[0]}"))
    elif result.get("note"):
        steps.append(("Screenshot", f"not taken — {result['note']}"))
    outcome = "verified" if rendered else "verified (unrendered read)"
    recordings_mod.file_recording(
        workspace, target, steps, shots, outcome,
        engine_label="Forge Browser (built-in package)")
    lines = [f"Verified in the browser: {target}",
             "",
             "Page text (rendered by Forge Browser):" if rendered
             else "Page text (plain read — no browser engine "
                  "available):",
             str(result.get("text") or ""), ""]
    if result.get("snapshot"):
        lines += ["Accessibility snapshot:", str(result["snapshot"]), ""]
    if shot:
        lines.append(
            f"Screenshot saved to .forge/artifacts/{shots[0]} — "
            "the owner can open it from the project folder. "
            "(Verify results are text: the screenshot is never "
            "attached to this conversation as image data.)")
    elif result.get("note"):
        lines.append(f"Screenshot: {result['note']}")
    else:
        lines.append("No screenshot was taken.")
    lines.append("A recording of this verification (step log + "
                 "screenshots) was filed under .forge/artifacts/.")
    return "\n".join(lines)
