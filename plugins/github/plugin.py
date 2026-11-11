"""GitHub connector — public API, read-only, stdlib only."""
import json
import urllib.parse
import urllib.request

API = "https://api.github.com"

# S6 (audit S1-29): JSON bodies are read with a hard cap — at most
# MAX_BYTES + 1 bytes are ever pulled off the wire.
MAX_BYTES = 5 * 1024 * 1024


def _get(url, ctx):
    req = urllib.request.Request(url, headers={
        "User-Agent": "LinuxForgeAgent/2.0", "Accept": "application/vnd.github+json"})
    token = (ctx.config or {}).get("github_token")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large (over 5 MB) — refused")
    return json.loads(body.decode("utf-8"))


def _repo_shape_ok(repo: str) -> bool:
    """S6 (audit S1-30): the repo parameter must be exactly
    "owner/name" — two non-empty segments, no extra slashes, no
    "." or ".." segments that could walk the API path."""
    parts = repo.split("/")
    return (len(parts) == 2
            and all(part and part not in (".", "..") for part in parts))


def github_repo_info(args, ctx):
    repo = str(args.get("repo", "")).strip()
    if not _repo_shape_ok(repo):
        return "Give a repo like owner/name — exactly two parts, owner/name."
    try:
        d = _get(f"{API}/repos/{urllib.parse.quote(repo, safe='/')}", ctx)
    except Exception as exc:
        return f"GitHub lookup failed: {exc}"
    return (f"{d.get('full_name')} · ★ {d.get('stargazers_count', 0)} · "
            f"{d.get('language', '?')} · license {(d.get('license') or {}).get('spdx_id', '?')} · "
            f"last push {d.get('pushed_at', '?')}\n{d.get('description') or ''}\n{d.get('html_url', '')}")


def github_search_repos(args, ctx):
    query = urllib.parse.quote(str(args.get("query", "")).strip())
    if not query:
        return "No search query given."
    limit = int(args.get("limit") or 10)
    try:
        data = _get(f"{API}/search/repositories?q={query}&per_page={limit}&sort=stars", ctx)
    except Exception as exc:
        return f"GitHub search failed: {exc}"
    items = data.get("items", [])
    if not items:
        return "No repositories matched."
    return "\n".join(f"- {r.get('full_name')} · ★ {r.get('stargazers_count', 0)} · "
                     f"{(r.get('description') or '')[:90]}" for r in items)


HANDLERS = {"github_repo_info": github_repo_info, "github_search_repos": github_search_repos}
