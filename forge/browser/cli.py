"""Command line for Forge Browser: ``python3 -m forge.browser ...``

    search "query" [--max 5] [--json]        keyless web search
    fetch URL [--offset N] [--max-chars N]   page -> clean Markdown
    fetch-any URL [--json]                   any file type -> text/result
    render URL [--json]                      page after JavaScript (CDP engine)
    snapshot URL                             accessibility tree + elements
    screenshot URL --out shot.png            PNG of the rendered page
    describe FILE                            vision-seat description of an image
    transcribe FILE                          local transcription of audio
    research "topic" [--queries "a,b"]       prints the plan (the gate)
    research "topic" --yes [--out report.md] runs the approved plan
    verify page.html --workspace DIR         #65-style project page check
    engines                                  what this machine has
    vault create|list                        the encrypted login vault
    vault add SITE                           prompts for user + password
    vault remove SITE

Human-readable output by default; --json prints the exact dicts the
library returns. Vault passwords are prompted with no echo and are
never accepted as command-line arguments. Errors print one plain
line to stderr and exit 2.
"""

import argparse
import getpass
import json
import os
import sys

from .errors import BrowserError
from .fetch import fetch
from .search import search


def _print_json(data):
    print(json.dumps(data, indent=2))


def _cmd_search(args):
    results = search(args.query, max_results=args.max)
    if args.json:
        _print_json([r.to_dict() for r in results])
    elif not results:
        print("No results.")
    else:
        for index, result in enumerate(results, 1):
            print(f"{index}. {result.title}")
            print(f"   {result.url}")
            if result.snippet:
                print(f"   {result.snippet}")
    return 0


def _cmd_fetch(args):
    page = fetch(args.url, max_chars=args.max_chars, offset=args.offset)
    if args.json:
        _print_json(page.to_dict())
        return 0
    print(f"# {page.title or page.final_url}")
    print(f"Source: {page.final_url}")
    print()
    print(page.markdown)
    if page.truncated:
        print()
        print(f"[... {page.total_chars - (page.offset + len(page.markdown))} more chars — "
              f"fetch again with --offset {page.next_offset}]")
    return 0


def _cmd_fetch_any(args):
    from .files import fetch_any
    result = fetch_any(args.url)
    if args.json:
        _print_json(result.to_dict())
        return 0
    print(f"{result.kind}: {result.final_url} ({result.size_bytes} bytes)")
    if result.saved_path:
        print(f"Saved: {result.saved_path}")
    for note in result.notes:
        print(f"Note: {note}")
    if result.text:
        print()
        print(result.text)
    return 0


def _cmd_render(args):
    from .render import render
    page = render(args.url, max_chars=args.max_chars, offset=args.offset)
    if args.json:
        _print_json(page.to_dict())
        return 0
    print(f"# {page.title or page.final_url}")
    print(f"Source: {page.final_url} (rendered)")
    print()
    print(page.markdown)
    if page.truncated:
        print(f"\n[... more — render again with --offset {page.next_offset}]")
    return 0


def _cmd_snapshot(args):
    from .render import snapshot
    data = snapshot(args.url)
    if args.json:
        _print_json(data)
        return 0
    print(f"# {data['title'] or data['url']}  (engine: {data['engine']})")
    print(data["snapshot"])
    print()
    print("Elements:")
    for element in data["elements"]:
        print(f"[{element['i']}] {element['tag']} {element['text']}".rstrip())
    return 0


def _cmd_screenshot(args):
    from .render import screenshot
    path = screenshot(args.url, args.out)
    print(f"Screenshot saved: {path}")
    return 0


def _cmd_describe(args):
    from .media import describe_saved_image, pick_vision_model
    if not pick_vision_model():
        raise BrowserError(
            "no local vision seat is configured — Ollama is not answering "
            "or no vision model is installed (see BROWSER.md: vision seat)")
    text = describe_saved_image(args.file)
    if not text:
        raise BrowserError("the vision seat returned no description")
    print(text)
    return 0


def _cmd_transcribe(args):
    from .media import transcribe_saved_audio
    text = transcribe_saved_audio(args.file)
    if not text:
        raise BrowserError(
            "no local transcription backend answered — install whisper or "
            "set FORGE_TRANSCRIBE_CMD (see BROWSER.md)")
    print(text)
    return 0


def _cmd_research(args):
    from .research import format_plan, make_plan, run_research
    queries = [q.strip() for q in (args.queries or "").split(",") if q.strip()] or None
    plan = make_plan(args.topic, queries=queries, max_pages=args.max_pages)
    if not args.yes:
        print(format_plan(plan))
        print()
        print("Plan only — nothing was searched. Run again with --yes to approve and run it.")
        return 0
    report, _sources = run_research(plan, approved=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"Report written: {args.out}")
    else:
        print(report)
    return 0


def _cmd_verify(args):
    from .verify import verify_in_browser
    result = verify_in_browser(args.target, args.workspace)
    if args.json:
        _print_json(result)
        return 0
    print(f"Verified: {result['target']} (rendered: {result['rendered']})")
    if result.get("screenshot"):
        print(f"Screenshot: {result['screenshot']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
    print()
    print(result["text"])
    return 0


def _cmd_engines(args):
    from .engines import engine_status, format_status
    status = engine_status()
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return 0


def _vault_passphrase(confirm=False):
    phrase = os.environ.get("FORGE_BROWSER_VAULT_PASSPHRASE", "")
    if phrase:
        return phrase
    phrase = getpass.getpass("Vault passphrase: ")
    if confirm:
        again = getpass.getpass("Repeat passphrase: ")
        if phrase != again:
            raise BrowserError("the passphrases did not match")
    return phrase


def _cmd_vault(args):
    from .vault import Vault, default_vault_path
    path = default_vault_path()
    if args.action == "create":
        Vault.create(path, _vault_passphrase(confirm=True))
        print(f"Vault created: {path}")
        return 0
    vault = Vault.open(path, _vault_passphrase())
    if args.action == "list":
        entries = vault.list_entries()
        if not entries:
            print("The vault is empty.")
        for entry in entries:
            print(f"{entry['site']} — user: {entry['username']}"
                  + (f" ({entry['notes']})" if entry["notes"] else ""))
        return 0
    if args.action == "add":
        username = input(f"Username for {args.site}: ").strip()
        password = getpass.getpass("Password (not shown): ")
        notes = input("Notes (optional): ").strip()
        vault.set(args.site, username, password, notes)
        print(f"Saved: {args.site}")
        return 0
    if args.action == "remove":
        print("Removed." if vault.delete(args.site) else "No such entry.")
        return 0
    raise BrowserError(f"unknown vault action {args.action}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="forge.browser",
        description="Forge Browser — Code Forge's own keyless, multimodal web layer.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="search the web (no API key)")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("fetch", help="fetch one page as clean Markdown")
    p.add_argument("url")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_fetch)

    p = sub.add_parser("fetch-any", help="fetch any file type (PDF, image, audio, data...)")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_fetch_any)

    p = sub.add_parser("render", help="fetch the page after JavaScript, via a local engine")
    p.add_argument("url")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("snapshot", help="accessibility snapshot + numbered elements")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("screenshot", help="save a PNG screenshot of a rendered page")
    p.add_argument("url")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_screenshot)

    p = sub.add_parser("describe", help="describe an image with the local vision seat")
    p.add_argument("file")
    p.set_defaults(func=_cmd_describe)

    p = sub.add_parser("transcribe", help="transcribe audio with a local backend")
    p.add_argument("file")
    p.set_defaults(func=_cmd_transcribe)

    p = sub.add_parser("research", help="plan-gated research with a cited report")
    p.add_argument("topic")
    p.add_argument("--queries", default="", help="comma-separated searches (default: the topic)")
    p.add_argument("--max-pages", type=int, default=6)
    p.add_argument("--yes", action="store_true", help="approve the plan and run it")
    p.add_argument("--out", default="", help="write the report to this file")
    p.set_defaults(func=_cmd_research)

    p = sub.add_parser("verify", help="check a project page the #65 way")
    p.add_argument("target")
    p.add_argument("--workspace", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("engines", help="what browser power this machine has")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_engines)

    p = sub.add_parser("vault", help="the encrypted login vault")
    p.add_argument("action", choices=["create", "list", "add", "remove"])
    p.add_argument("site", nargs="?", default="")
    p.set_defaults(func=_cmd_vault)

    args = parser.parse_args(argv)
    if getattr(args, "action", "") in ("add", "remove") and not args.site:
        print("Forge Browser: vault add/remove need a SITE", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except BrowserError as exc:
        print(f"Forge Browser: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
