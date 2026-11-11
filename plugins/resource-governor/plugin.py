"""Resource Governor plugin — wraps forge.governor (the getcompute logic)."""
from forge import governor


def check_resources(args, ctx):
    model = (args.get("model") or ctx.current_model or "").strip()
    snap = governor.snapshot(ctx.base_url)
    lines = [
        f"Bench: {snap['avail_gb']} GB free of {snap['total_gb']} GB RAM | "
        f"CPU load {snap['load_1min']} on {snap['threads']} threads"
        f"{' (BUSY)' if snap['cpu_busy'] else ''} | "
        f"Ollama loaded: {', '.join(snap['ollama_loaded']) or 'none'}",
    ]
    if model:
        lines.append(governor.verdict_text(governor.verdict_for(model, snap)))
    return "\n".join(lines)


HANDLERS = {"check_resources": check_resources}
