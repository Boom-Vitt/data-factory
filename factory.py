#!/usr/bin/env python3
"""data-factory: every AI conversation on this machine, in one JSONL store you own.

  factory.py collect            sweep local agent transcripts into the store
  factory.py import FILE        add a ChatGPT / Claude.ai export (.zip or conversations.json)
  factory.py export             write train.jsonl (prompt -> answer pairs, chat format)
  factory.py search QUERY       find past questions and results
  factory.py mcp                run as an MCP server (tools: factory_log, factory_search)

Store: ~/.data-factory (override with DATA_FACTORY_HOME). Stdlib only.
"""
import argparse, json, os, re, sys, zipfile
from datetime import datetime, timezone
from pathlib import Path

STORE = Path(os.environ.get("DATA_FACTORY_HOME") or Path.home() / ".data-factory")
SESSIONS = STORE / "sessions"

# ponytail: catches common key shapes only, not every secret. Run a real scanner
# (gitleaks/trufflehog) over the store before it ever leaves this machine.
SECRETS = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?<=[Bb]earer )[A-Za-z0-9._~+/-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)


# Harness chatter stored under the user role (<system-reminder>, <task-notification>,
# <environment_context>...). Hyphen/underscore tag names, so pasted HTML is kept.
HARNESS = re.compile(r"\s*<[a-z]+[-_][a-z_-]+>")


def redact(s):
    return SECRETS.sub("[REDACTED]", s)


def text_of(content):
    """Plain text of a message body; tool calls, tool results and images are dropped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
            and b.get("type", "text") in ("text", "input_text", "output_text")
        )
    return ""


def lines(path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


# --- parsers: each yields (role, text, timestamp, model, cwd) -----------------

def claude(path):
    for d in lines(path):
        m = d.get("message")
        if d.get("type") in ("user", "assistant") and isinstance(m, dict) and not d.get("isMeta"):
            yield m.get("role"), text_of(m.get("content")), d.get("timestamp"), m.get("model"), d.get("cwd")


def codex(path):
    cwd = model = None
    for d in lines(path):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        cwd = p["cwd"] if isinstance(p.get("cwd"), str) else cwd
        model = p["model"] if isinstance(p.get("model"), str) else model
        if d.get("type") == "response_item" and p.get("type") == "message":
            yield p.get("role"), text_of(p.get("content")), d.get("timestamp"), model, cwd


def gemini(path):
    roles = {"user": "user", "gemini": "assistant", "model": "assistant", "assistant": "assistant"}
    for d in lines(path):
        yield roles.get(d.get("type")), text_of(d.get("content")), d.get("timestamp"), d.get("model"), None


def antigravity(path):
    for d in lines(path):
        src = str(d.get("source"))  # USER_EXPLICIT / MODEL / SYSTEM
        yield ("user" if src.startswith("USER") else "assistant" if src == "MODEL" else None), text_of(d.get("content")), d.get("created_at"), d.get("model"), None


# name -> (glob under $HOME, parser, session id from path)
SOURCES = {
    "claude-code": (".claude/projects/**/*.jsonl", claude, lambda p: p.stem),
    "codex": (".codex/sessions/**/*.jsonl", codex, lambda p: p.stem),
    "gemini": (".gemini/tmp/*/chats/*.jsonl", gemini, lambda p: p.stem),
    "antigravity": (".gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl",
                    antigravity, lambda p: p.parents[2].name),
}


def clean_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(s)).strip(".-")[:120] or "unknown"


def save(source, session, msgs):
    """Write one session file (atomically). Returns the number of messages kept."""
    recs = [
        {"source": source, "session": session, "role": role, "text": redact(text),
         "ts": ts, "model": model, "cwd": cwd}
        for role, text, ts, model, cwd in msgs
        if role in ("user", "assistant") and isinstance(text, str) and text.strip()
        and not (role == "user" and HARNESS.match(text))
    ]
    if recs:
        out = SESSIONS / clean_name(source) / f"{clean_name(session)}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
        os.replace(tmp, out)
    return len(recs)


def collect(home=None):
    home = Path(home or Path.home())
    for source, (pattern, parser, sid) in SOURCES.items():
        seen = updated = msgs = 0
        for src in home.glob(pattern):
            seen += 1
            out = SESSIONS / source / f"{clean_name(sid(src))}.jsonl"
            try:
                if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
                    continue
                n = save(source, sid(src), parser(src))
            except OSError as e:
                print(f"  skip {src}: {e}", file=sys.stderr)
                continue
            updated += bool(n)
            msgs += n
        print(f"{source:12} {seen:6} sessions found, {updated:6} updated, {msgs:8} messages written")


# --- web exports --------------------------------------------------------------

def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None


def web(conv):
    """One conversation from a ChatGPT or Claude.ai export -> (source, session, msgs)."""
    if "mapping" in conv:  # ChatGPT
        # ponytail: flattens the reply tree by time, so regenerated answers all appear in order.
        nodes = sorted((n["message"] for n in conv["mapping"].values() if n.get("message")),
                       key=lambda m: m.get("create_time") or 0)
        return "chatgpt", conv.get("conversation_id") or conv.get("id"), [
            ((m.get("author") or {}).get("role"),
             "\n".join(p for p in (m.get("content") or {}).get("parts") or [] if isinstance(p, str)),
             iso(m.get("create_time")), (m.get("metadata") or {}).get("model_slug"), None)
            for m in nodes]
    if "chat_messages" in conv:  # Claude.ai
        return "claude-chat", conv.get("uuid"), [
            ("user" if m.get("sender") == "human" else m.get("sender"),
             m.get("text") or text_of(m.get("content")), m.get("created_at"), None, None)
            for m in conv["chat_messages"]]
    return None


def import_export(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if re.search(r"(^|/)conversations[^/]*\.json$", n)]
            convs = [c for n in names for c in json.loads(z.read(n))]
    else:
        convs = json.loads(Path(path).read_text())
    done = msgs = 0
    for conv in convs:
        parsed = web(conv) if isinstance(conv, dict) else None
        if parsed:
            n = save(*parsed)
            done += bool(n)
            msgs += n
    print(f"imported {done} of {len(convs)} conversations, {msgs} messages")
    if convs and not done:
        sys.exit("no conversation matched a known format (ChatGPT or Claude.ai export)")


# --- training export, search, log ---------------------------------------------

def pairs(recs):
    """(prompt, answer) per turn: a user message plus every assistant message until the next one."""
    prompt, answer = None, []
    for r in [*recs, {"role": "user", "text": None}]:  # sentinel flushes the last turn
        if r["role"] == "user":
            if prompt and answer:
                yield prompt, "\n\n".join(answer)
            prompt, answer = r["text"], []
        else:
            answer.append(r["text"])


def export():
    out, n = STORE / "train.jsonl", 0
    STORE.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for p in sorted(SESSIONS.glob("*/*.jsonl")):
            for prompt, answer in pairs(lines(p)):
                f.write(json.dumps({"source": p.parent.name, "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer}]}, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} training pairs -> {out}")


def search(query, limit=20):
    # ponytail: linear scan of every file. Add SQLite FTS when this gets slow.
    q, hits = query.lower(), []
    for p in sorted(SESSIONS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        for r in lines(p):
            i = r["text"].lower().find(q)
            if i >= 0:
                snippet = " ".join(r["text"][max(0, i - 80): i + 200].split())
                hits.append(f"[{r['source']} {r['session']} {r.get('ts') or ''}] {r['role']}: {snippet}")
                if len(hits) >= limit:
                    return hits
    return hits


def log(source, prompt, response, model=None):
    """Append one prompt/result pair. For agents that keep no local transcript."""
    now = datetime.now(timezone.utc)
    out = SESSIONS / clean_name(source.lower()) / f"mcp-{now:%Y-%m-%d}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as f:
        for role, text in (("user", prompt), ("assistant", response)):
            f.write(json.dumps({"source": out.parent.name, "session": out.stem, "role": role,
                                "text": redact(text), "ts": now.isoformat(), "model": model,
                                "cwd": None}, ensure_ascii=False) + "\n")
    return f"saved to {out}"


# --- MCP server (stdio, newline-delimited JSON-RPC) ---------------------------

TOOLS = [
    {"name": "factory_log",
     "description": "Save a user prompt and your final result into the data factory. "
                    "Call once after answering, if this agent is not already collected from disk.",
     "inputSchema": {"type": "object", "required": ["source", "prompt", "response"], "properties": {
         "source": {"type": "string", "description": "agent name, e.g. grok, cowork, chatgpt"},
         "prompt": {"type": "string"}, "response": {"type": "string"},
         "model": {"type": "string"}}}},
    {"name": "factory_search",
     "description": "Search every past AI conversation in the data factory for a phrase.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}},
]


def handle(method, p):
    if method == "initialize":
        return {"protocolVersion": p.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "data-factory", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        a = p.get("arguments") or {}
        if p.get("name") == "factory_log":
            text = log(str(a["source"]), str(a["prompt"]), str(a["response"]), a.get("model"))
        elif p.get("name") == "factory_search":
            text = "\n".join(search(str(a["query"]), int(a.get("limit") or 20))) or "no matches"
        else:
            raise NotImplementedError(f"unknown tool {p.get('name')}")
        return {"content": [{"type": "text", "text": text}]}
    raise NotImplementedError(f"unknown method {method}")


def mcp():
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict) or "id" not in req:
            continue  # notifications need no reply
        reply = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            reply["result"] = handle(req.get("method"), req.get("params") or {})
        except NotImplementedError as e:
            reply["error"] = {"code": -32601, "message": str(e)}
        except Exception as e:  # bad arguments, disk errors: report, keep serving
            reply["error"] = {"code": -32602, "message": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    sub.add_parser("import").add_argument("file")
    sub.add_parser("export")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=20)
    sub.add_parser("mcp")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect()
    elif a.cmd == "import":
        import_export(a.file)
    elif a.cmd == "export":
        export()
    elif a.cmd == "search":
        print("\n".join(search(a.query, a.limit)) or "no matches")
    elif a.cmd == "mcp":
        mcp()


if __name__ == "__main__":
    main()
