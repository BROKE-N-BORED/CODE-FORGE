"""Hugging Face connector — public API, read-only, stdlib only."""
import json
import urllib.parse
import urllib.request

API = "https://huggingface.co/api"

# S6 (audit S1-29): JSON bodies are read with a hard cap — at most
# MAX_BYTES + 1 bytes are ever pulled off the wire.
MAX_BYTES = 5 * 1024 * 1024


def _get(url, ctx):
    req = urllib.request.Request(url, headers={"User-Agent": "LinuxForgeAgent/2.0"})
    token = (ctx.config or {}).get("hf_token")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large (over 5 MB) — refused")
    return json.loads(body.decode("utf-8"))


def hf_search_models(args, ctx):
    query = urllib.parse.quote(str(args.get("query", "")).strip())
    if not query:
        return "No search query given."
    limit = int(args.get("limit") or 10)
    try:
        data = _get(f"{API}/models?search={query}&limit={limit}&sort=downloads&direction=-1", ctx)
    except Exception as exc:
        return f"Hugging Face search failed: {exc}"
    if not data:
        return "No models matched."
    lines = []
    for m in data:
        lines.append(f"- {m.get('id')} · downloads {m.get('downloads', 0)} · "
                     f"likes {m.get('likes', 0)} · task {m.get('pipeline_tag', '?')}")
    return "\n".join(lines)


def hf_model_info(args, ctx):
    repo = str(args.get("repo_id", "")).strip()
    if "/" not in repo:
        return "Give a repo id like owner/name."
    try:
        data = _get(f"{API}/models/{urllib.parse.quote(repo, safe='')}?blobs=true", ctx)
    except Exception as exc:
        return f"Hugging Face lookup failed: {exc}"
    lines = [f"{data.get('id')} · downloads {data.get('downloads', 0)} · "
             f"license {(data.get('cardData') or {}).get('license', '?')}"]
    gguf, other = [], 0
    for s in data.get("siblings", []):
        size = s.get("size") or 0
        if s.get("rfilename", "").lower().endswith(".gguf"):
            gguf.append(f"  {s['rfilename']} — {size / 1e9:.2f} GB")
        else:
            other += 1
    if gguf:
        lines.append("GGUF files:")
        lines.extend(gguf[:24])
    lines.append(f"({other} non-GGUF files)")
    return "\n".join(lines)


HANDLERS = {"hf_search_models": hf_search_models, "hf_model_info": hf_model_info}
