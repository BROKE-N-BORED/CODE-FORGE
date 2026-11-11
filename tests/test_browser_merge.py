"""Browser merge integration tests (2026-11-11).

Pins the three behaviors the forge/browser v0.2 merge adds behind
the existing Browse seam, with fake transports on BOTH sides (no
network, no MCP subprocess, no real browser):

* the Research web-engine option — off (default): browse calls
  hit the Browse connector path exactly as before; on: the
  built-in package serves open / read behind the SAME allowlist
  fence and the SAME wall hold;
* verification recordings — a verify session (the #65 loop or the
  package's verify) files ONE recording artifact (step log +
  screenshots, honestly not a video); a failed or refused verify
  files its attempt too;
* the settings surface — the two engine choices round-trip
  through SettingsDialog with today's behavior as the default,
  and PluginContext carries them with connector defaults.

The package's own suite (tests/test_browser.py +
tests/test_browser2.py) pins the package itself; this file pins
the seam it landed behind.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forge import artifacts as artifacts_mod  # noqa: E402
from forge import browse as browse_mod  # noqa: E402
from forge import recordings as recordings_mod  # noqa: E402
from forge import verify as verify_mod  # noqa: E402
from forge import webengine  # noqa: E402
from forge.browser.errors import BrowserError  # noqa: E402
from forge.browser.fetch import Page  # noqa: E402
from forge.plugins import PluginContext  # noqa: E402


def _load_browse_plugin():
    spec = importlib.util.spec_from_file_location(
        "forge_browse_plugin_under_test",
        ROOT / "plugins" / "browse" / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_browse_plugin()


class _FakeServer:
    """The connector side's transport double: records every tool
    call and answers in the fake Browse server's words."""

    def __init__(self):
        self.calls = []

    def call_tool(self, tool, args):
        self.calls.append((tool, args))
        return {"navigate": "did navigate",
                "snapshot": "did snapshot",
                "screenshot": "did screenshot"}.get(tool, "ok")


def _fake_page(markdown="# Hello\n\nsome readable text",
               url="https://example.com/page"):
    return Page(url=url, final_url=url, title="Example Page",
                markdown=markdown, links=[],
                total_chars=len(markdown), offset=0,
                truncated=False, rendered=False)


class _SeamCase(unittest.TestCase):
    """Shared harness: a temp workspace with an allowlist, DNS
    stubbed the way the S5 fence tests require, and a factory for
    plugin contexts at either engine setting."""

    def setUp(self):
        self._dns_patch = mock.patch.object(
            browse_mod.socket, "getaddrinfo",
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("93.184.216.34", 0))])
        self._dns_patch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / ".forge").mkdir()
        (self.ws / ".forge" / "browse.json").write_text(
            json.dumps({"allowlist": ["example.com"]}),
            encoding="utf-8")
        self.server = _FakeServer()
        self._server_patch = mock.patch.object(
            plugin, "get_server", lambda name, spec: self.server)
        self._server_patch.start()

    def tearDown(self):
        browse_mod.clear_hold(self.ws)
        webengine._pages.pop(str(self.ws), None)
        self._server_patch.stop()
        self._dns_patch.stop()
        self._tmp.cleanup()

    def _ctx(self, engine=""):
        config = {"mcp_servers": {"browse": {
                      "command": "python3", "args": ["fake.py"]}},
                  "browse": {"server": "browse"}}
        return PluginContext(workspace=str(self.ws), config=config,
                             log=lambda m: None,
                             research_web_engine=engine)


class EngineRoutingTests(_SeamCase):

    def test_default_engine_hits_the_connector(self):
        # Option OFF (the default): the old path, byte for byte —
        # the package's fetch is never touched.
        with mock.patch.object(
                webengine, "_fetch_page",
                side_effect=AssertionError("package fetch called")):
            result = plugin.browse_open(
                {"url": "https://example.com/page"}, self._ctx())
        self.assertIn("did navigate", result)
        self.assertEqual([c[0] for c in self.server.calls],
                         ["navigate"])

    def test_package_engine_serves_open_and_read(self):
        # Option ON: the package fetches; the MCP server is never
        # called; read returns the page open stored.
        with mock.patch.object(webengine, "_fetch_page",
                               return_value=_fake_page()):
            opened = plugin.browse_open(
                {"url": "https://example.com/page"},
                self._ctx("forge-browser"))
            read = plugin.browse_read({}, self._ctx("forge-browser"))
        self.assertIn("Opened https://example.com/page", opened)
        self.assertIn("some readable text", opened)
        self.assertIn("some readable text", read)
        self.assertEqual(self.server.calls, [])

    def test_allowlist_fence_applies_to_the_package_too(self):
        # The engine swap changes WHO fetches, never WHAT may be
        # fetched: an unlisted host is refused before either side.
        with mock.patch.object(
                webengine, "_fetch_page",
                side_effect=AssertionError("package fetch called")):
            result = plugin.browse_open(
                {"url": "https://evil.example.net/"},
                self._ctx("forge-browser"))
        self.assertIn("not on this project's Browse site "
                      "allowlist", result)
        self.assertEqual(self.server.calls, [])

    def test_package_fetch_failure_is_a_tool_error(self):
        # A failed open must not count as a Research page op nor
        # join Sources consulted: the worker treats results
        # starting "Tool error" as never-reached-a-page.
        with mock.patch.object(
                webengine, "_fetch_page",
                side_effect=BrowserError("connection refused")):
            result = plugin.browse_open(
                {"url": "https://example.com/page"},
                self._ctx("forge-browser"))
        self.assertTrue(result.startswith("Tool error"), result)

    def test_package_wall_sets_the_shared_hold(self):
        # A captcha in a package-fetched page HOLDS the session in
        # the same state the connector's walls use — and the next
        # verb is refused by it, exactly as on the old path.
        page = _fake_page("# Hold on\n\nverify you are human")
        with mock.patch.object(webengine, "_fetch_page",
                               return_value=page):
            opened = plugin.browse_open(
                {"url": "https://example.com/page"},
                self._ctx("forge-browser"))
        self.assertIsNotNone(browse_mod.get_hold(self.ws))
        self.assertIn("HELD", opened)
        refused = plugin.browse_read({}, self._ctx("forge-browser"))
        self.assertIn("HELD", refused)

    def test_package_engine_click_is_a_plain_limitation(self):
        result = plugin.browse_click(
            {"selector": "#go"}, self._ctx("forge-browser"))
        self.assertIn("not served by the Forge Browser engine",
                      result)
        self.assertEqual(self.server.calls, [])

    def test_package_read_with_no_page_open_is_a_tool_error(self):
        result = plugin.browse_read({}, self._ctx("forge-browser"))
        self.assertTrue(result.startswith("Tool error"), result)


class RecordingTests(_SeamCase):

    def _recordings(self):
        return [e for e in artifacts_mod.list_artifacts(self.ws)
                if e["kind"] == "recording"]

    def _recording_text(self, entry):
        path = self.ws / ".forge" / "artifacts" / entry["file"]
        return path.read_text(encoding="utf-8")

    def test_verify_loop_files_recording_with_steps_and_shot(self):
        png_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                   "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5E"
                   "rkJggg==")
        replies = {"open": "opened",
                   "read": "the page's own words",
                   "screenshot": f"data:image/png;base64,{png_b64}"}

        def call(key, args):
            return replies[key]

        (self.ws / "index.html").write_text(
            "<h1>hi</h1>", encoding="utf-8")
        result = verify_mod.run_verify(
            "index.html", str(self.ws), call)
        self.assertIn("Verified in the browser", result)
        entries = self._recordings()
        self.assertEqual(len(entries), 1)
        text = self._recording_text(entries[0])
        self.assertIn("not a video", text)
        self.assertIn("Outcome: verified", text)
        self.assertIn("Open the page", text)
        self.assertIn("Read the page", text)
        self.assertIn("Screenshot", text)
        shots = [n for n in text.splitlines()
                 if n.startswith("- .forge/artifacts/")]
        self.assertEqual(len(shots), 1)
        self.assertIn("verify-", shots[0])
        self.assertIn(".png", shots[0])

    def test_failed_verify_files_its_attempt(self):
        def call(key, args):
            return "MCP tool error: the browser is gone"

        result = verify_mod.run_verify(
            "https://example.com/page", str(self.ws), call)
        self.assertTrue(result.startswith("Verify failed"), result)
        entries = self._recordings()
        self.assertEqual(len(entries), 1)
        text = self._recording_text(entries[0])
        self.assertIn("Outcome: failed", text)
        self.assertIn("Open the page", text)

    def test_refused_verify_files_its_attempt(self):
        result = verify_mod.run_verify(
            "../outside.html", str(self.ws), lambda k, a: "unused")
        self.assertIn("outside this project", result)
        entries = self._recordings()
        self.assertEqual(len(entries), 1)
        self.assertIn("Outcome: refused",
                      self._recording_text(entries[0]))

    def test_package_verify_needs_no_connector_and_records(self):
        # Forge Browser's verify runs with NO MCP config at all —
        # the package needs no connector — and still records.
        shot = self.ws / ".forge" / "artifacts" / "verify-page.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        outcome = {"target": "index.html",
                   "url": "http://127.0.0.1:9/index.html",
                   "text": "rendered words", "rendered": True,
                   "snapshot": "- heading \"hi\"",
                   "screenshot": str(shot), "note": ""}
        ctx = PluginContext(workspace=str(self.ws), config={},
                            log=lambda m: None,
                            research_web_engine="forge-browser")
        with mock.patch.object(webengine, "_package_verify",
                               return_value=outcome):
            result = plugin.verify_in_browser(
                {"target": "index.html"}, ctx)
        self.assertIn("Verified in the browser: index.html", result)
        self.assertIn("rendered words", result)
        entries = self._recordings()
        self.assertEqual(len(entries), 1)
        text = self._recording_text(entries[0])
        self.assertIn("Outcome: verified", text)
        self.assertIn("verify-page.png", text)

    def test_package_verify_refusal_records_the_attempt(self):
        ctx = PluginContext(workspace=str(self.ws), config={},
                            log=lambda m: None,
                            research_web_engine="forge-browser")
        with mock.patch.object(
                webengine, "_package_verify",
                side_effect=BrowserError(
                    "verify target '../x' resolves outside this "
                    "project — refused")):
            result = plugin.verify_in_browser(
                {"target": "../x"}, ctx)
        self.assertIn("outside this project", result)
        entries = self._recordings()
        self.assertEqual(len(entries), 1)
        self.assertIn("Outcome: refused",
                      self._recording_text(entries[0]))

    def test_recording_document_names_itself_honestly(self):
        text = recordings_mod.build_text(
            "index.html", [("Open the page", "opened it")], [],
            "verified")
        self.assertIn("step log", text)
        self.assertIn("not a video", text)
        self.assertIn("None — no screenshot was saved", text)


class SettingsAndContextTests(unittest.TestCase):

    def test_recording_is_a_registered_artifact_kind(self):
        self.assertIn("recording", artifacts_mod.KINDS)

    def test_context_defaults_keep_the_connector(self):
        ctx = PluginContext(workspace="w")
        self.assertEqual(ctx.research_web_engine, "")
        self.assertEqual(ctx.forge_browser_engine, "auto")
        self.assertEqual(webengine.engine_for(ctx), "browse")
        self.assertEqual(webengine.cdp_engine_for(ctx), "auto")

    def test_engine_choice_parsing(self):
        ctx = PluginContext(workspace="w",
                            research_web_engine="forge-browser",
                            forge_browser_engine="lightpanda")
        self.assertEqual(webengine.engine_for(ctx), "forge-browser")
        self.assertEqual(webengine.cdp_engine_for(ctx), "lightpanda")
        bogus = PluginContext(workspace="w",
                              research_web_engine="???",
                              forge_browser_engine="???")
        self.assertEqual(webengine.engine_for(bogus), "browse")
        self.assertEqual(webengine.cdp_engine_for(bogus), "auto")

    def test_settings_dialog_round_trips_engine_keys(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from forge.main import SettingsDialog
        app = QApplication.instance() or QApplication([])
        dialog = SettingsDialog(None, {})
        values = dialog.values()
        # Defaults: today's behavior, exactly.
        self.assertEqual(values["research_web_engine"], "browse")
        self.assertEqual(values["forge_browser_engine"], "auto")
        # The owner picks Forge Browser + Lightpanda; both save.
        idx = dialog.research_web_engine.findData("forge-browser")
        dialog.research_web_engine.setCurrentIndex(idx)
        idx = dialog.forge_browser_engine.findData("lightpanda")
        dialog.forge_browser_engine.setCurrentIndex(idx)
        values = dialog.values()
        self.assertEqual(values["research_web_engine"],
                         "forge-browser")
        self.assertEqual(values["forge_browser_engine"], "lightpanda")
        # And a saved choice preselects on the next open.
        dialog2 = SettingsDialog(None, values)
        self.assertEqual(
            dialog2.research_web_engine.currentData(), "forge-browser")
        self.assertEqual(
            dialog2.forge_browser_engine.currentData(), "lightpanda")
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
