#!/usr/bin/env python3
"""data-factory: every AI conversation on this machine, in one JSONL store you own.

  factory.py collect            sweep local agent transcripts into the store
  factory.py import FILE        add a ChatGPT / Claude.ai export (.zip or conversations.json)
  factory.py ocr DIR            index image + correct-text pairs found in DIR (for OCR training)
  factory.py export             write train.jsonl (prompt -> answer pairs, chat format)
  factory.py search QUERY       find past questions and results
  factory.py show               open the factory page (a local HTML view of the store)
  factory.py mcp                run as an MCP server (factory_log, factory_search, factory_show)
  factory.py install [NAME]     connect that MCP server to Claude Code, Codex, Gemini CLI, Claude desktop

Store: ~/.data-factory (override with DATA_FACTORY_HOME). Stdlib only.
"""
import argparse, html, json, math, os, re, shutil, subprocess, sys, webbrowser, zipfile
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
                mtime = src.stat().st_mtime_ns
                if out.exists() and out.stat().st_mtime_ns >= mtime:
                    continue
                n = save(source, sid(src), parser(src))
                if n:  # keep the transcript's own time: "latest sessions" then means last activity
                    os.utime(out, ns=(mtime, mtime))
            except OSError as e:
                print(f"  skip {src}: {e}", file=sys.stderr)
                continue
            updated += bool(n)
            msgs += n
        print(f"{source:12} {seen:6} sessions found, {updated:6} updated, {msgs:8} messages written")
    profile()
    dashboard()


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


# --- OCR pairs ----------------------------------------------------------------

SPLITS = ("train", "val", "test")


def add_ocr(folder):
    """Index every {"image": ..., "text": ...} record in the JSONL manifests under `folder`."""
    # ponytail: stores the path to each image, not a copy (datasets run to tens of GB).
    # Re-run after moving the folder. Split is guessed from the manifest path; test wins on overlap.
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")
    found, missing = {}, 0
    for man in sorted(root.rglob("*.jsonl")):
        rel = man.relative_to(root)
        if any(part.startswith(".") or part in ("node_modules", "raw") for part in rel.parts) or man.stem.endswith("_raw"):
            continue  # hidden folders, dependencies, untouched downloads and pre-cleanup manifests
        where = str(rel).lower()
        split = "test" if "test" in where else "val" if "val" in where else "train"
        for r in lines(man):
            img, text = r.get("image"), r.get("text")
            if not (isinstance(img, str) and isinstance(text, str) and text.strip()):
                continue
            path = next((p for p in (Path(img), root / img, man.parent / img) if p.is_file()), None)
            if path is None:
                missing += 1
                continue
            key = str(path.resolve())
            if key not in found or SPLITS.index(split) > SPLITS.index(found[key]["split"]):
                found[key] = {"image": key, "text": text, "source": str(r.get("source") or rel.parent), "split": split}
    if not found:
        sys.exit(f"no image + text pairs found under {root} ({missing} records pointed at missing images)")
    out = STORE / "ocr" / f"{clean_name(root.name)}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in found.values()))
    os.replace(tmp, out)
    n = {k: sum(r["split"] == k for r in found.values()) for k in SPLITS}
    print(f"{len(found):,} image + text pairs -> {out}")
    print(f"  train {n['train']:,}   val {n['val']:,}   test {n['test']:,} (never train on test)   missing images skipped: {missing:,}")
    profile()
    dashboard()


# --- the factory page ---------------------------------------------------------

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="120">
<title>data-factory</title><style>
:root{--bg:#e6e9eb;--ink:#14171a;--dim:#56606a;--line:#c3c9ce;--floor:#d3d8dc;--joint:#bcc3c9;--wall:#aeb6bd;
--belt:#2a2e32;--hazard:#f2c200;--glow:#fff6c9;--tag:#ffffff;--ok:#2f7d6b;--bad:#a63f2b;--rz:40deg;--rx:58deg;--s:1}
@media(prefers-color-scheme:dark){:root{--bg:#14171a;--ink:#e8ecef;--dim:#9aa6b0;--line:#2c3339;--floor:#1f2428;
--joint:#2b3238;--wall:#59636c;--belt:#0b0d0e;--glow:#ffd96a;--tag:#262c31;--ok:#3d9c86;--bad:#c9573f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Sukhumvit Set",Thonburi,sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 56px}
h1,h2,h3,.tag b,.n,.chip{font-family:"Avenir Next Condensed","DIN Condensed","Roboto Condensed","Arial Narrow",system-ui,sans-serif}
h1{font-size:44px;line-height:1;margin:0;font-weight:700;letter-spacing:.01em}
.sub{margin:10px 0 0;max-width:68ch;font-size:18px}.note,.lead{color:var(--dim);margin:4px 0 0;max-width:70ch;font-size:15px}
h2{font-size:26px;font-weight:600;margin:44px 0 0}.lead{margin-bottom:12px}
.stage{position:relative;height:calc(__SH__px*var(--s));margin:18px 0 0;overflow:hidden;background:var(--floor);
border-top:6px solid var(--hazard);perspective:1800px;cursor:grab;touch-action:pan-y;user-select:none;-webkit-user-select:none}
.stage:active{cursor:grabbing}
.world{position:absolute;left:50%;top:57%;width:__GW__px;height:__GD__px;margin:calc(__GD__px/-2) 0 0 calc(__GW__px/-2);
transform:scale(var(--s)) rotateX(var(--rx)) rotateZ(var(--rz));transform-style:preserve-3d}
.ground{position:absolute;inset:-400px;background-image:linear-gradient(var(--joint) 2px,transparent 2px),
linear-gradient(90deg,var(--joint) 2px,transparent 2px);background-size:80px 80px}
.zone{position:absolute;border:3px dashed var(--hazard)}
.cube,.rider,.tag,.puff{position:absolute;left:var(--x);top:var(--y)}
.cube{width:var(--w);height:var(--d);transform:translateZ(var(--z));transform-style:preserve-3d}
.cube i{position:absolute;display:block;background-color:var(--c);box-shadow:inset 0 0 0 1px rgba(0,0,0,.16)}
.cube .t{inset:0;transform:translateZ(var(--h))}
.cube .f,.cube .b{left:0;width:100%;height:var(--h);filter:brightness(.8)}
.cube .f{top:calc(100% - var(--h));transform-origin:bottom;transform:rotateX(-90deg)}
.cube .b{top:0;transform-origin:top;transform:rotateX(90deg)}
.cube .r,.cube .l{top:0;height:100%;width:var(--h);filter:brightness(.62)}
.cube .r{left:calc(100% - var(--h));transform-origin:right;transform:rotateY(90deg)}
.cube .l{left:0;transform-origin:left;transform:rotateY(-90deg)}
.fac .f,.fac .b{background-image:repeating-linear-gradient(90deg,transparent 0 16px,var(--glow) 16px 34px);
background-size:100% 26px;background-position:0 42%;background-repeat:no-repeat}
.fac .r,.fac .l{background-image:repeating-linear-gradient(0deg,transparent 0 16px,var(--glow) 16px 34px);
background-size:26px 100%;background-position:42% 0;background-repeat:no-repeat}
.rider{transform-style:preserve-3d;animation:ride var(--t) linear var(--dl) infinite}
@keyframes ride{from{transform:translate3d(0,0,8px)}to{transform:translate3d(var(--len),0,8px)}}
.tag{transform:translateZ(var(--z)) rotateZ(calc(-1*var(--rz))) rotateX(calc(-1*var(--rx))) translate(-50%,-100%);
transform-origin:0 0;white-space:nowrap;background:var(--tag);color:var(--ink);padding:3px 9px 4px;font-size:12px;line-height:1.25;
border-left:4px solid var(--c,var(--hazard));box-shadow:0 1px 0 rgba(0,0,0,.25)}
.tag b{display:block;font-size:17px;font-weight:600;letter-spacing:.01em}
.puff{width:26px;height:26px;margin:-13px;border-radius:50%;background:var(--dim);opacity:0;animation:puff 5s linear var(--dl) infinite}
@keyframes puff{0%{opacity:0;transform:translateZ(var(--z)) rotateZ(calc(-1*var(--rz))) rotateX(calc(-1*var(--rx))) scale(.5)}
15%{opacity:.45}100%{opacity:0;transform:translateZ(calc(var(--z) + 120px)) rotateZ(calc(-1*var(--rz))) rotateX(calc(-1*var(--rx))) scale(2)}}
@media(prefers-reduced-motion:reduce){.rider,.puff{animation:none}}
.ctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0 0}
.ctl span{color:var(--dim);font-size:15px;margin-right:auto;max-width:60ch}
button{font:inherit;font-size:15px;color:var(--ink);background:var(--tag);border:1px solid var(--line);padding:6px 14px;cursor:pointer}
button:hover{border-color:var(--ink)}button:focus-visible{outline:3px solid var(--hazard);outline-offset:2px}
.recs{list-style:none;margin:0;padding:0}
.rec{position:relative;display:grid;grid-template-columns:1fr 300px;gap:6px 36px;padding:16px 0 16px 20px;border-top:1px solid var(--line)}
.rec::before{content:"";position:absolute;left:0;top:16px;bottom:16px;width:6px;background:var(--k)}
.s-ready{--k:var(--ok)}.s-prep,.s-more{--k:var(--hazard)}.s-missing{--k:var(--bad)}.s-no{--k:var(--dim)}
.rec h3{margin:0;font-size:21px;font-weight:600}.rec p{margin:4px 0 0}.rec .next{color:var(--dim);font-size:15px}
.chip{display:inline-block;font-size:15px;font-weight:600;padding:1px 9px 2px;background:var(--k);color:#fff}
.s-prep .chip,.s-more .chip{color:#14171a}.s-no .chip{color:var(--bg)}
.meter{height:8px;background:var(--line);margin:10px 0 4px}.meter i{display:block;height:100%;background:var(--k)}
.rec .cnt,.rec .model{font-size:14px;margin:2px 0 0}.rec .model{color:var(--dim)}
.more{margin:0;padding-left:20px;max-width:75ch}.more li{margin:6px 0}
code{font:13px ui-monospace,Menlo,Consolas,monospace;background:var(--tag);padding:2px 6px;overflow-wrap:anywhere}
.box{overflow-x:auto}table{border-collapse:collapse;width:100%}
td,th{padding:8px 14px 8px 0;text-align:left;border-top:1px solid var(--line);vertical-align:top}
th{font-size:14px;color:var(--dim);font-weight:500;border-top:2px solid var(--ink)}
.n{text-align:right;font-size:19px;white-space:nowrap}.t,.a{white-space:nowrap}.t{color:var(--dim)}
.a i{display:inline-block;width:10px;height:10px;margin-right:8px;background:var(--c)}
.bar{width:38%;min-width:90px}.bar i{display:block;height:10px;margin-top:9px;background:var(--c)}
.q{overflow-wrap:anywhere}footer{color:var(--dim);font-size:14px;margin-top:36px}
@media(max-width:720px){.rec{grid-template-columns:1fr}h1{font-size:36px}}
</style><main><h1>data-factory</h1>
<p class="sub">Your factory holds __SESSIONS__ conversations (__MESSAGES__ messages) from __NSOURCES__, and has packed __PAIRS__ question-and-answer pairs for training.__OCRLINE__</p>
<p class="note">Updated __NOW__. Everything on this page stays on your computer.</p>
<div class="stage" role="img" aria-label="3D factory floor: each AI agent is a machine sending crates of conversations into the factory, which ships training pairs. The same numbers are in the tables below."><div class="world"><div class="ground"></div>__SCENE__</div></div>
<div class="ctl"><span>Each machine is one AI agent: taller means more messages, a faster belt means a busier agent. Drag the floor to look around.</span>
<button type="button" data-d="-30">Turn left</button><button type="button" data-d="30">Turn right</button><button type="button" data-d="0">Reset view</button></div>
<h2>What you can train with this data</h2>
<p class="lead">Counted from your own conversations. The bar shows how many examples you have against a sensible minimum. Fine-tuning needs a GPU; searching and testing do not.</p>
<ul class="recs">__RECS__</ul>
<h2>Add more conversations</h2><p class="lead">More agents in the factory means better answers above.</p><ul class="more">__MORE__</ul>
<h2>What each agent sent in</h2><div class="box"><table><tr><th>Agent</th><th class="n">Conversations</th><th class="n">Messages</th><th></th></tr>__SOURCES__</table></div>
<h2>Latest conversations</h2><div class="box"><table><tr><th>Last activity</th><th>Agent</th><th>It started with</th></tr>__RECENT__</table></div>
<footer>Stored in __STORE__</footer></main>
<script>
const st=document.querySelector('.stage'),R=document.documentElement.style;let rz=40,rx=58;
try{[rz,rx]=JSON.parse(sessionStorage.df)}catch(e){}
const set=()=>{R.setProperty('--rz',rz+'deg');R.setProperty('--rx',rx+'deg');try{sessionStorage.df=JSON.stringify([rz,rx])}catch(e){}};
const fit=()=>R.setProperty('--s',Math.min(1,st.clientWidth/__FIT__));
st.onpointerdown=e=>{st.setPointerCapture(e.pointerId);let x=e.clientX,y=e.clientY;
st.onpointermove=m=>{rz-=(m.clientX-x)*.4;rx=Math.max(30,Math.min(80,rx-(m.clientY-y)*.3));x=m.clientX;y=m.clientY;set()}};
st.onpointerup=st.onpointercancel=()=>st.onpointermove=null;addEventListener('resize',fit);fit();set();
document.querySelectorAll('.ctl button').forEach(b=>b.onclick=()=>{+b.dataset.d?rz+=+b.dataset.d:(rz=40,rx=58);set()});
const ago=new Intl.RelativeTimeFormat('en',{numeric:'auto'});
document.querySelectorAll('[data-t]').forEach(e=>{const s=e.dataset.t-Date.now()/1000,a=Math.abs(s);
const [n,u]=a<3600?[s/60,'minute']:a<86400?[s/3600,'hour']:[s/86400,'day'];e.title=e.textContent;e.textContent=ago.format(Math.round(n),u)});
</script></html>"""

PAINTS = ["#2f7d6b", "#2b5fa8", "#a63f2b", "#6a5a9c", "#b07a1e", "#1f7f8c", "#8a3f6b", "#55626e"]  # machine enamel
CRATE = "#c9a063"
NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "antigravity": "Antigravity",
         "chatgpt": "ChatGPT", "claude-chat": "Claude chat", "grok": "Grok", "cowork": "Cowork"}


def nice(source):
    return NAMES.get(source, source)


def cube(x, y, w, d, h, c, z=0, cls=""):
    return (f'<div class="cube {cls}" style="--x:{x:.0f}px;--y:{y:.0f}px;--w:{w:.0f}px;--d:{d:.0f}px;'
            f'--h:{h:.0f}px;--z:{z:.0f}px;--c:{c}"><i class="t"></i><i class="f"></i><i class="b"></i>'
            f'<i class="r"></i><i class="l"></i></div>')


def tag(x, y, z, title, note, c="var(--hazard)"):
    return (f'<div class="tag" style="--x:{x:.0f}px;--y:{y:.0f}px;--z:{z:.0f}px;--c:{c}">'
            f'<b>{html.escape(title)}</b>{html.escape(note)}</div>')


def riders(x, y, length, seconds, c):
    return "".join(
        f'<div class="rider" style="--x:{x:.0f}px;--y:{y:.0f}px;--len:{length:.0f}px;--t:{seconds:.1f}s;'
        f'--dl:{-seconds * k / 3:.1f}s">{cube(0, 0, 12, 12, 10, c)}</div>' for k in range(3))


def scene(agents, total, pairs_n):
    """The factory floor: one machine per agent, belts into the factory, crates of training pairs out.
    Returns (html, floor width, floor depth). Units are px on the floor plane; z is up."""
    agents = agents or [("nothing collected yet", 0, 0)]
    n = len(agents)
    slot = max(52, min(96, 440 // n))  # floor depth per machine
    mw, depth, fx, fw, fh = slot * .62, n * slot, 420, 190, 110
    top = max(m for _, _, m in agents) or 1
    cy = 40 + depth / 2
    parts = []
    for i, (name, _, m) in enumerate(agents):
        y, c = 40 + i * slot, PAINTS[i % len(PAINTS)]
        h = 26 + 120 * math.log1p(m) / math.log1p(top)
        seconds = 9 - 5 * m / top  # busier agents ship faster
        parts += [cube(40, y + (slot - mw) / 2, mw, mw, h, c),
                  cube(40 + mw, y + slot / 2 - 9, fx - 40 - mw, 18, 8, "var(--belt)"),
                  riders(40 + mw / 2, y + slot / 2 - 6, fx - 20 - mw / 2, seconds, c),
                  tag(40 + mw / 2, y + slot / 2, h + 10, nice(name), f"{m:,} messages", c)]
    parts += [
        cube(fx, 40, fw, depth, fh, "var(--wall)", cls="fac"),
        cube(fx - 8, 32, fw + 16, depth + 16, 8, "var(--hazard)", z=fh),            # roof
        cube(fx + fw - 52, 50, 22, 22, 74, "var(--wall)", z=fh + 8),                # chimneys
        cube(fx + fw - 52, 84, 22, 22, 52, "var(--wall)", z=fh + 8),
        f'<div class="puff" style="--x:{fx + fw - 41}px;--y:61px;--z:{fh + 96}px;--dl:0s"></div>',
        f'<div class="puff" style="--x:{fx + fw - 41}px;--y:61px;--z:{fh + 96}px;--dl:-2.5s"></div>',
        f'<div class="puff" style="--x:{fx + fw - 41}px;--y:95px;--z:{fh + 74}px;--dl:-1.2s"></div>',
        tag(fx + fw / 2 - 20, cy + 10, fh + 22, "data-factory", f"{total:,} messages stored"),
        cube(fx + fw, cy - 9, 120, 18, 8, "var(--belt)"),                           # outbound belt
        riders(fx + fw - 14, cy - 6, 126, 5, CRATE),
        f'<div class="zone" style="left:{fx + fw + 108}px;top:{cy - 52:.0f}px;width:104px;height:104px"></div>',
        cube(fx + fw + 122, cy - 38, 34, 34, 28, CRATE), cube(fx + fw + 122, cy + 4, 34, 34, 28, CRATE),
        cube(fx + fw + 162, cy - 18, 34, 34, 28, CRATE), cube(fx + fw + 130, cy - 18, 34, 34, 28, CRATE, z=28),
        tag(fx + fw + 160, cy, 70, "Training pairs", f"{pairs_n:,} ready", CRATE),
    ]
    return "".join(parts), fx + fw + 250, depth + 80


# ponytail: keyword heuristics, tuned on Claude Code / Codex / Orca transcripts. Good enough to size
# a dataset; label a sample by hand before trusting the split for real training.
MACHINE = re.compile(r"(another claude session sent a message|<teammate-message|──── from:|you have \d+ orchestration"
                     r"|please carry out this task from my|read (and fully execute )?/\S+|background \(from the session"
                     r"|you are )")
THAI = re.compile(r"[฀-๿]")


def profile():
    """Count what kinds of question-and-answer examples the store holds, for the training advice."""
    c = dict.fromkeys(("asked", "thai", "steer", "brief", "review", "summary", "ocr", "ocr_pairs", "ocr_test"), 0)
    for p in (STORE / "ocr").glob("*.jsonl"):
        for r in lines(p):
            c["ocr_test" if r.get("split") == "test" else "ocr_pairs"] += 1
    for p in SESSIONS.glob("*/*.jsonl"):
        worker = p.name.startswith("agent-")  # a Claude Code subagent: its prompt was written by another AI
        for prompt, _ in pairs(lines(p)):
            lo = " ".join(prompt.split()).lower()
            if worker:
                c["brief"] += 1
            elif lo.startswith("this session is being continued"):
                c["summary"] += 1
            elif re.match(r"(transcribe|ocr|read) (all |the )?text (in|from) (this|the) (image|photo|scan|picture)", lo):
                c["ocr"] += 1
            elif MACHINE.match(lo):
                pass  # one agent talking to another
            elif len(lo) < 60:
                c["steer"] += 1
            else:
                c["asked"] += 1
                c["thai"] += bool(THAI.search(prompt))
                c["review"] += bool(re.search(r"review|audit|ตรวจ", lo))
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "profile.json").write_text(json.dumps(c))
    return c


STATUS = {"ready": "Ready now", "prep": "Needs preparation", "more": "Needs more examples",
          "missing": "Not collected yet", "no": "Not recommended"}


def recommendations(c, messages):
    """What this store can train. Base models are open-weight releases checked 2026-11; update as they change."""
    typed = c["asked"] + c["steer"]
    rows = [  # title, what it gives you, examples you have, sensible minimum, forced status, start from, next step
        ("Search everything you ever asked", "Find any past question or answer by meaning, in Thai or English.",
         messages, 0, None, "Use BGE-M3 or SEA-Embedding as they are",
         "No training. Index the store with an embedding model and search it."),
        ("A test to judge any model before you switch", "Your own real requests, replayed on a candidate model, show whether it is good enough for you.",
         c["asked"], 200, None, "Works with any model", "Set 200 varied requests aside and never train on them."),
        ("Send easy prompts to a cheap model", "A tiny model reads each prompt and decides whether it needs the expensive model at all.",
         typed, 2000, "prep" if typed >= 2000 else None, "Start from Gemma 4 E2B, the smallest current Gemma, as a classifier",
         "Label each prompt easy or hard. A large model can label them in one pass."),
        ("Write reports in your format", "Turn a task brief into a structured findings report in the style your agents already use.",
         c["brief"] + c["review"], 1000, None, "Start from Qwen3.5-9B or Gemma 4 12B with LoRA. For speed: Nemotron 3.5 Lightning (3B active)",
         "This teaches the format, not the investigation: the tool use behind each report is not stored."),
        ("Summarise long sessions", "Compress a long conversation into the short summary an agent needs to carry on.",
         c["summary"], 500, "prep" if c["summary"] >= 500 else None, "Start from Gemma 4 E4B or Qwen3.5-9B with LoRA",
         "Link each stored summary to the conversation it summarises."),
        ("A Thai assistant in your own words", "Answers Thai requests the way you ask and the way you like them answered.",
         c["thai"], 3000, None, "Start from Qwen3.5-9B or Gemma 4 12B with LoRA",
         "Keep collecting, and import your ChatGPT and Gemini history."),
        ("Read Thai text from images (OCR)", f"A small vision model that replaces paid OCR calls. The factory saw {c['ocr']:,} of them.",
         c.get("ocr_pairs", 0), 2000, None if c.get("ocr_pairs") else "missing",
         "Start from Typhoon OCR2 (2B) or dots.mocr with LoRA. Smaller: PaddleOCR-VL-1.6 (0.9B) or Nemotron 3.5 Parse (0.6B); test their Thai first",
         f"{c.get('ocr_test', 0):,} more pairs are held out as a test set. Never train on those." if c.get("ocr_pairs")
         else "Index a folder of images and their correct text: factory.py ocr FOLDER"),
        ("Thai speech to text", "Transcribe Thai speech and dialects without a paid service.",
         0, 0, "missing", "Start from Typhoon ASR Real-time (115M) or Typhoon Whisper Turbo; Typhoon Isan for the Isan dialect", "Add audio clips together with their correct transcripts."),
        ("Replace your coding agent", "A local model that edits code and runs tools the way Claude Code or Codex does.",
         0, 0, "no", "",
         "Tool calls are not stored, and no model you can host yourself matches these agents yet."),
    ]
    out, e = "", html.escape
    for title, what, have, target, forced, model, step in rows:
        status = forced or ("ready" if have >= target else "more")
        meter = ""
        if target:
            meter = (f'<div class="meter"><i style="width:{min(100, 100 * have / target):.0f}%"></i></div>'
                     f'<p class="cnt">' + (f"{have:,} examples; {target:,} is enough" if have >= target
                                      else f"{have:,} of {target:,} examples") + "</p>")
        elif have:
            meter = f'<p class="cnt">{have:,} messages to search</p>'
        out += (f'<li class="rec s-{status}"><div><h3>{e(title)}</h3><p>{e(what)}</p><p class="next">{e(step)}</p></div>'
                f'<div><span class="chip">{STATUS[status]}</span>{meter}<p class="model">{e(model)}</p></div></li>')
    return out


def more_sources(have):
    """How to bring in agents that are not in the store yet."""
    here = Path(__file__).resolve()
    tips = [
        ("chatgpt", f"<b>ChatGPT:</b> ask for your data export in its settings, then run <code>python3 {html.escape(str(here))} import export.zip</code>"),
        ("claude-chat", f"<b>Claude chat:</b> export your data in its settings, then run <code>python3 {html.escape(str(here))} import conversations.json</code>"),
        ("grok", "<b>Grok, Cowork and others:</b> connect this factory to them as an MCP server and ask them to save each answer with <code>factory_log</code>"),
    ]
    out = "".join(f"<li>{tip}</li>" for key, tip in tips if key not in have)
    return out or "<li>Every agent this factory knows about is already sending conversations in.</li>"


def dashboard():
    """Rebuild the one-page HTML view of the store. Returns its path."""
    # ponytail: rescans every session file on each rebuild (fine at ~100 MB). Cache counts if it grows to GBs.
    e = html.escape
    files = sorted(SESSIONS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    per = {}  # agent -> [sessions, messages]
    for p in files:
        s = per.setdefault(p.parent.name, [0, 0])
        s[0] += 1
        s[1] += p.read_bytes().count(b"\n")
    agents = sorted(((k, s, m) for k, (s, m) in per.items()), key=lambda a: -a[2])
    paint = {k: PAINTS[i % len(PAINTS)] for i, (k, _, _) in enumerate(agents)}
    total = sum(m for _, _, m in agents)
    top = max((m for _, _, m in agents), default=0) or 1
    sources = "".join(
        f'<tr style="--c:{paint[k]}"><td class="a"><i></i>{e(nice(k))}</td><td class="n">{s:,}</td><td class="n">{m:,}</td>'
        f'<td class="bar"><i style="width:{100 * m / top:.1f}%"></i></td></tr>' for k, s, m in agents)
    recent = ""
    for p in files[:25]:
        first = next((r for r in lines(p) if r.get("role") == "user"), {})
        snippet = " ".join(str(first.get("text") or "(no prompt)").split())[:160]
        mtime = p.stat().st_mtime
        recent += (f'<tr style="--c:{paint[p.parent.name]}"><td class="t" data-t="{mtime:.0f}">'
                   f'{datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}</td>'
                   f'<td class="a"><i></i>{e(nice(p.parent.name))}</td><td class="q">{e(snippet)}</td></tr>')
    train = STORE / "train.jsonl"
    pairs_n = train.read_bytes().count(b"\n") if train.exists() else 0
    try:  # written by collect; counting needs a full parse, too slow for every tool call
        counts = json.loads((STORE / "profile.json").read_text())
    except (OSError, ValueError):
        counts = profile()
    floor, gw, gd = scene(agents, total, pairs_n)
    fill = {
        "__NOW__": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "__SESSIONS__": f"{len(files):,}", "__MESSAGES__": f"{total:,}", "__PAIRS__": f"{pairs_n:,}",
        "__NSOURCES__": f"{len(per)} AI agent" + "s" * (len(per) != 1), "__STORE__": e(str(STORE)),
        "__SCENE__": floor, "__GW__": str(gw), "__GD__": str(gd),
        "__SH__": f"{(gw + gd) * .36 + 140:.0f}", "__FIT__": f"{(gw + gd) * .72:.0f}",
        "__OCRLINE__": f" It also indexes {counts['ocr_pairs']:,} images with their correct text for OCR training." if counts.get("ocr_pairs") else "",
        "__RECS__": recommendations(counts, total), "__MORE__": more_sources(per),
        "__SOURCES__": sources or '<tr><td colspan="4">Nothing collected yet. Run: factory.py collect</td></tr>',
        "__RECENT__": recent or '<tr><td colspan="3">No conversations yet.</td></tr>',
    }
    page = re.sub("|".join(fill), lambda m: fill[m.group()], PAGE)
    STORE.mkdir(parents=True, exist_ok=True)
    out = STORE / "index.html"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(page)
    os.replace(tmp, out)
    return out


def show(open_browser=True):
    page = dashboard()
    if open_browser and not os.environ.get("DATA_FACTORY_NO_OPEN"):
        webbrowser.open(page.as_uri())
    return f"factory page: {page}"


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
    {"name": "factory_show",
     "description": "Open the data factory page in the user's browser: totals per agent and the latest sessions.",
     "inputSchema": {"type": "object", "properties": {}}},
]
OPENED = False  # the page pops up on the first tool call of each server session


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
        global OPENED
        a = p.get("arguments") or {}
        if p.get("name") == "factory_log":
            text = log(str(a["source"]), str(a["prompt"]), str(a["response"]), a.get("model"))
        elif p.get("name") == "factory_search":
            text = "\n".join(search(str(a["query"]), int(a.get("limit") or 20))) or "no matches"
        elif p.get("name") == "factory_show":
            text = "opened in the browser"
        else:
            raise NotImplementedError(f"unknown tool {p.get('name')}")
        # every call refreshes the page (an open tab reloads itself); pop it up once per session
        text += "\n" + show(open_browser=p.get("name") == "factory_show" or not OPENED)
        OPENED = True
        return {"content": [{"type": "text", "text": text}]}
    raise NotImplementedError(f"unknown method {method}")


def mcp():
    out = os.fdopen(os.dup(1), "w")  # replies go here; anything else printed (browser launchers) goes to stderr
    os.dup2(2, 1)
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
        print(json.dumps(reply), file=out, flush=True)


def install(only=()):
    """Connect this MCP server to every AI client found on this machine (or only those named)."""
    py, me, tag = sys.executable or "python3", str(Path(__file__).resolve()), "data-factory"
    clis = {  # client -> (program, remove an older entry, add)
        "Claude Code": ("claude", ["mcp", "remove", tag, "-s", "user"], ["mcp", "add", "--scope", "user", tag, "--", py, me, "mcp"]),
        "Codex": ("codex", ["mcp", "remove", tag], ["mcp", "add", tag, "--", py, me, "mcp"]),
        "Gemini CLI": ("gemini", ["mcp", "remove", "-s", "user", tag], ["mcp", "add", "-s", "user", tag, py, me, "mcp"]),
    }
    wanted = lambda name: not only or any(o.lower() in name.lower() for o in only)
    for name, (exe, remove, add) in clis.items():
        if not wanted(name):
            continue
        path = shutil.which(exe)
        if not path:
            print(f"{name:15} not installed, skipped")
            continue
        try:
            subprocess.run([path, *remove], capture_output=True, timeout=120)
            r = subprocess.run([path, *add], capture_output=True, text=True, timeout=120)
            why = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
            print(f"{name:15} " + ("connected. Start a new session to use it." if r.returncode == 0 else f"failed: {why[0]}"))
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"{name:15} failed: {e}")
    name = "Claude desktop"
    if wanted(name):
        base = {"darwin": Path.home() / "Library/Application Support", "win32": Path(os.environ.get("APPDATA") or Path.home())}
        cfg = base.get(sys.platform, Path.home() / ".config") / "Claude" / "claude_desktop_config.json"
        if not cfg.parent.is_dir():
            print(f"{name:15} not installed, skipped")
            return
        try:  # merge into the app's config; never overwrite a file we cannot parse
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
            data.setdefault("mcpServers", {})[tag] = {"command": py, "args": [me, "mcp"]}
            if cfg.exists():
                shutil.copy2(cfg, cfg.with_name(cfg.name + ".bak"))
            tmp = cfg.with_name(cfg.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, cfg)
            print(f"{name:15} connected. Quit and reopen the app to load it. (previous config saved as {cfg.name}.bak)")
        except (ValueError, AttributeError, TypeError, OSError) as e:
            print(f"{name:15} skipped, config left untouched: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
    sub.add_parser("import").add_argument("file")
    sub.add_parser("ocr").add_argument("folder")
    sub.add_parser("export")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=20)
    sub.add_parser("show")
    sub.add_parser("install").add_argument("only", nargs="*", metavar="NAME", help="e.g. claude, codex, gemini")
    sub.add_parser("mcp")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect()
    elif a.cmd == "import":
        import_export(a.file)
    elif a.cmd == "ocr":
        add_ocr(a.folder)
    elif a.cmd == "export":
        export()
    elif a.cmd == "search":
        print("\n".join(search(a.query, a.limit)) or "no matches")
    elif a.cmd == "install":
        install(a.only)
    elif a.cmd == "show":
        print(show())
    elif a.cmd == "mcp":
        mcp()


if __name__ == "__main__":
    main()
