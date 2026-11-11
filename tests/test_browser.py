"""Offline tests for forge.browser (Item 1: search + fetch).

Everything here runs without the internet: the search parsers are fed
fixture responses, and fetch/CLI run against a throwaway stdlib HTTP
server on 127.0.0.1 serving a fixture page — the same pattern Forge's
#65 preview server uses. Live-web behaviour is a separate smoke check,
never a unit test.
"""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.browser import BrowserError, fetch, search  # noqa: E402
from forge.browser.html2md import html_to_markdown  # noqa: E402
from forge.browser.search import (  # noqa: E402
    parse_bing_html,
    parse_ddg_html,
    parse_searxng_json,
)

FIXTURE_HTML = """<!DOCTYPE html>
<html><head><title>Fixture Page</title><style>body{}</style>
<script>var secret = "should not appear";</script></head>
<body>
<nav><a href="/menu">Menu noise</a></nav>
<main>
<h1>Real Heading</h1>
<p>First paragraph with <strong>bold</strong> and a
<a href="/docs/page.html">relative link</a> plus an
<a href="https://example.org/abs">absolute link</a>.</p>
<ul><li>item one</li><li>item two</li></ul>
<ol><li>first</li><li>second</li></ol>
<pre>code line 1
code line 2</pre>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>alpha</td><td>1</td></tr></table>
</main>
<footer>Footer noise</footer>
</body></html>"""

DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone&amp;rut=x">First Result</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Snippet   one text.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2Ftwo">Second Result</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=y">Snippet two text.</a>
</div>"""

DDG_LITE_HTML = """
<table>
<tr><td><a rel="nofollow" class="result-link"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Follama.com%2F&amp;rut=abc">Ollama Home</a></td></tr>
<tr><td class="result-snippet">Run models   locally.</td></tr>
<tr><td><a rel="nofollow" class="result-link"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2Flite">Lite Two</a></td></tr>
<tr><td class="result-snippet">Second snippet.</td></tr>
</table>"""

BING_HTML = """
<ol><li class="b_algo"><h2><a href="https://example.org/bing-one">Bing One</a></h2>
<div class="b_caption"><p> Bing   snippet one. </p></div></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9leGFtcGxlLm5ldC90d28">Bing Two</a></h2>
<p>Snippet two.</p></li></ol>"""

SEARXNG_JSON = json.dumps({
    "query": "test",
    "results": [
        {"title": "S Result", "url": "https://example.org/s",
         "content": "searxng   snippet", "engines": ["duckduckgo"]},
        {"title": "No URL", "content": "skipped"},
    ],
})


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page.html":
            body = FIXTURE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path.startswith("/search"):
            body = SEARXNG_JSON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"nope"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class HtmlToMarkdownTests(unittest.TestCase):
    def test_structure_noise_and_links(self):
        title, markdown, links = html_to_markdown(FIXTURE_HTML, "http://site.test/dir/")
        self.assertEqual(title, "Fixture Page")
        self.assertIn("# Real Heading", markdown)
        self.assertIn("**bold**", markdown)
        self.assertIn("- item one", markdown)
        self.assertIn("1. first", markdown)
        self.assertIn("```\ncode line 1", markdown)
        self.assertIn("| Name | Value |", markdown)
        self.assertIn("| alpha | 1 |", markdown)
        self.assertNotIn("should not appear", markdown)
        self.assertNotIn("Menu noise", markdown)
        self.assertNotIn("Footer noise", markdown)
        urls = [link["url"] for link in links]
        self.assertIn("http://site.test/docs/page.html", urls)
        self.assertIn("https://example.org/abs", urls)


class SearchParserTests(unittest.TestCase):
    def test_ddg_html_unwraps_real_urls(self):
        results = parse_ddg_html(DDG_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "First Result")
        self.assertEqual(results[0].url, "https://example.org/one")
        self.assertEqual(results[0].snippet, "Snippet one text.")
        self.assertEqual(results[1].url, "https://example.net/two")

    def test_ddg_max_results(self):
        self.assertEqual(len(parse_ddg_html(DDG_HTML, 1)), 1)

    def test_ddg_lite_markup(self):
        results = parse_ddg_html(DDG_LITE_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://ollama.com/")
        self.assertEqual(results[0].snippet, "Run models locally.")
        self.assertEqual(results[1].title, "Lite Two")

    def test_bing_markup_and_ck_unwrap(self):
        results = parse_bing_html(BING_HTML, 5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://example.org/bing-one")
        self.assertEqual(results[0].snippet, "Bing snippet one.")
        self.assertEqual(results[1].url, "https://example.net/two")
        self.assertEqual(results[1].engine, "bing")

    def test_searxng_json(self):
        results = parse_searxng_json(SEARXNG_JSON, 5)
        self.assertEqual(len(results), 1)  # the URL-less result is skipped
        self.assertEqual(results[0].url, "https://example.org/s")
        self.assertEqual(results[0].snippet, "searxng snippet")
        self.assertEqual(results[0].engine, "duckduckgo")

    def test_searxng_bad_json_is_plain_error(self):
        with self.assertRaises(BrowserError):
            parse_searxng_json("not json", 5)

    def test_empty_query_refused(self):
        with self.assertRaises(BrowserError):
            search("   ")


class SearxngBackendTests(_ServerCase):
    def test_search_uses_configured_local_instance(self):
        old = os.environ.get("FORGE_SEARXNG_URL")
        os.environ["FORGE_SEARXNG_URL"] = self.base
        try:
            results = search("anything", max_results=5)
        finally:
            if old is None:
                del os.environ["FORGE_SEARXNG_URL"]
            else:
                os.environ["FORGE_SEARXNG_URL"] = old
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "S Result")


class FetchTests(_ServerCase):
    def test_fetch_local_page(self):
        page = fetch(self.base + "/page.html")
        self.assertEqual(page.title, "Fixture Page")
        self.assertIn("# Real Heading", page.markdown)
        self.assertEqual(page.final_url, self.base + "/page.html")
        self.assertFalse(page.truncated)
        self.assertIsNone(page.next_offset)

    def test_fetch_paging_covers_the_whole_page(self):
        full = fetch(self.base + "/page.html", max_chars=100000)
        seen = ""
        offset = 0
        while True:
            page = fetch(self.base + "/page.html", max_chars=50, offset=offset)
            seen += page.markdown
            if page.next_offset is None:
                break
            offset = page.next_offset
        self.assertEqual(seen, full.markdown)
        self.assertTrue(full.total_chars > 50)

    def test_fetch_refuses_non_http(self):
        with self.assertRaises(BrowserError):
            fetch("file:///etc/passwd")

    def test_fetch_404_is_plain_error(self):
        with self.assertRaises(BrowserError) as caught:
            fetch(self.base + "/missing")
        self.assertIn("404", str(caught.exception))


class CliTests(_ServerCase):
    def _run_cli(self, *argv):
        root = os.path.join(os.path.dirname(__file__), "..")
        return subprocess.run(
            [sys.executable, "-m", "forge.browser", *argv],
            cwd=root, capture_output=True, text=True, timeout=30,
        )

    def test_cli_fetch_json(self):
        proc = self._run_cli("fetch", self.base + "/page.html", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["title"], "Fixture Page")
        self.assertIn("# Real Heading", data["markdown"])

    def test_cli_fetch_refusal_exits_2(self):
        proc = self._run_cli("fetch", "file:///etc/passwd")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Forge Browser:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
