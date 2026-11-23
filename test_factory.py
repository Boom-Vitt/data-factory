"""Run: python test_factory.py  (uses a temp store and a fake home; touches nothing real).
Runs in CI on Linux, macOS and Windows."""
import base64, json, os, subprocess, sys, tempfile, zipfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["DATA_FACTORY_HOME"] = str(tmp / "store")
os.environ["DATA_FACTORY_NO_OPEN"] = "1"  # never pop a browser from the test
import factory as f

KEY = "sk-" + "a" * 30
PNG, JPG, SHOT = (base64.b64encode(b).decode() for b in (b"pasted png", b"pasted jpg", b"tool screenshot"))


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")


home = tmp / "home"
write(home / ".claude/projects/p/s1.jsonl", [
    {"type": "user", "timestamp": "t1", "cwd": "/w", "message": {"role": "user", "content": f"hi {KEY}"}},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
    {"type": "user", "message": {"role": "user", "content": "<task-notification>done</task-notification>"}},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "injected"}},
    {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [
        {"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "hello"}]}},
    {"type": "api-request-blob", "message": {"role": "user", "content": "noise"}},
])
write(home / ".codex/sessions/2026/01/01/r1.jsonl", [
    {"type": "session_meta", "payload": {"cwd": "/c"}},
    {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sys"}]}},
    {"type": "response_item", "timestamp": "t", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}},
])
write(home / ".gemini/tmp/x/chats/g1.jsonl", [
    {"sessionId": "g"}, {"type": "user", "content": [{"text": "gq"}]}, {"type": "gemini", "content": "ga"}])
write(home / ".gemini/antigravity/brain/B1/.system_generated/logs/transcript_full.jsonl", [
    {"source": "SYSTEM", "content": "sys"}, {"source": "USER_EXPLICIT", "content": "aq"}, {"source": "MODEL", "content": "aa"}])
# pasted images: stored once by content, even when pasted twice; what a tool read is not stored
image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
write(home / ".claude/projects/p/s2.jsonl", [
    {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "look at this"}, image]}},
    {"type": "user", "message": {"role": "user", "content": [image]}},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": SHOT}}]}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not base64 !!"}}]}},
])
write(home / ".codex/sessions/2026/01/01/r2.jsonl", [
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "see"}, {"type": "input_image", "image_url": f"data:image/jpeg;base64,{JPG}"}]}},
])

f.collect(home)


def read(rel):
    return list(f.lines(f.SESSIONS / rel))


c = read("claude-code/s1.jsonl")
assert [(r["role"], r["text"]) for r in c] == [("user", "hi [REDACTED]"), ("assistant", "hello")], c
assert c[0]["cwd"] == "/w" and c[1]["model"] == "m"
x = read("codex/r1.jsonl")
assert [(r["role"], r["text"], r["cwd"]) for r in x] == [("user", "q", "/c"), ("assistant", "a", "/c")], x
assert [r["text"] for r in read("gemini/g1.jsonl")] == ["gq", "ga"]
assert [r["text"] for r in read("antigravity/B1.jsonl")] == ["aq", "aa"]

s2 = read("claude-code/s2.jsonl")
assert [r["text"] for r in s2] == ["look at this", ""] and s2[0]["media"] == s2[1]["media"], s2
stored = sorted(p.name[-4:] for p in f.MEDIA.glob("*/*"))
assert stored == [".jpg", ".png"], stored
assert (f.MEDIA / s2[0]["media"][0]).read_bytes() == b"pasted png"
assert read("codex/r2.jsonl")[0]["media"][0].endswith(".jpg") and "media" not in c[0]

# unchanged sources are skipped on the next run
before = (f.SESSIONS / "codex/r1.jsonl").stat().st_mtime_ns
f.collect(home)
assert (f.SESSIONS / "codex/r1.jsonl").stat().st_mtime_ns == before

# a store written by an older version is re-read once, even though nothing looks changed
(f.STORE / "version").unlink()
(f.SESSIONS / "codex/r1.jsonl").write_text("", encoding="utf-8")
os.utime(f.SESSIONS / "codex/r1.jsonl", ns=(before, before))
f.collect(home)
assert len(read("codex/r1.jsonl")) == 2 and (f.STORE / "version").read_text(encoding="utf-8") == f.VERSION

# web exports: ChatGPT (zip) and Claude.ai (json)
z = tmp / "chatgpt.zip"
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("conversations.json", json.dumps([{"id": "c1", "mapping": {
        "b": {"message": {"author": {"role": "assistant"}, "create_time": 2, "content": {"parts": ["A"]}}},
        "a": {"message": {"author": {"role": "user"}, "create_time": 1, "content": {"parts": ["Q", {"img": 1}]}}},
        "root": {"message": None}}}]))
f.import_export(z)
assert [(r["role"], r["text"]) for r in read("chatgpt/c1.jsonl")] == [("user", "Q"), ("assistant", "A")]
j = tmp / "conversations.json"
j.write_text(json.dumps([{"uuid": "u1", "chat_messages": [
    {"sender": "human", "text": "cq"}, {"sender": "assistant", "text": "ca"}]}]), encoding="utf-8")
f.import_export(j)
assert [(r["role"], r["text"]) for r in read("claude-chat/u1.jsonl")] == [("user", "cq"), ("assistant", "ca")]

# turns -> training pairs
assert list(f.pairs([{"role": "user", "text": "lost"}, {"role": "user", "text": "q"},
                     {"role": "assistant", "text": "a1"}, {"role": "assistant", "text": "a2"},
                     {"role": "user", "text": "unanswered"}])) == [("q", "a1\n\na2")]
f.export()
assert not list(f.lines(f.STORE / "train.jsonl")), "every prompt so far is a short steer, which is not training data"

# MCP round trip, including a hostile source name and a secret
reqs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "factory_log", "arguments": {
        "source": "../../Grok", "prompt": "needle question <script>alert(1)</script>", "response": f"answer {KEY}"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "factory_search", "arguments": {"query": "NEEDLE"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "factory_log", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 6, "method": "nope"},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "factory_show"}},
]
out = subprocess.run([sys.executable, str(Path(f.__file__).resolve()), "mcp"], capture_output=True, text=True,
                     input="".join(json.dumps(r) + "\n" for r in reqs) + "garbage\n", check=True).stdout
r = {m["id"]: m for m in map(json.loads, out.splitlines())}
assert len(r) == 7 and r[1]["result"]["serverInfo"]["name"] == "data-factory"
assert {t["name"] for t in r[2]["result"]["tools"]} == {"factory_log", "factory_search", "factory_show"}
assert "needle question" in r[4]["result"]["content"][0]["text"]
assert r[5]["error"]["code"] == -32602 and r[6]["error"]["code"] == -32601
logged = list((f.SESSIONS / "grok").glob("mcp-*.jsonl"))
assert len(logged) == 1 and KEY not in logged[0].read_text(encoding="utf-8"), "source must stay inside the store, secrets redacted"
assert not [p for p in tmp.rglob("*.jsonl") if f.STORE not in p.parents and home not in p.parents]

# the factory page: rebuilt by collect and by every tool call, prompt text escaped
page = (f.STORE / "index.html").read_text(encoding="utf-8")
assert "index.html" in r[7]["result"]["content"][0]["text"]
assert "Claude Code" in page and "Grok" in page and "__" not in page
assert "&lt;script&gt;alert(1)" in page and "<script>alert" not in page
assert page.count('class="rec ') == 9 and "Gemma 4" in page and "Claude Code" in page
prof = json.loads((f.STORE / "profile.json").read_text(encoding="utf-8"))
assert prof["asked"] == 0 and prof["steer"] >= 4, prof  # every fixture prompt is a short one
assert "import export.zip" not in page and "factory_log" not in page, "tips only for agents not yet collected"
assert page.count('class="rider"') == 3 * 7 + 3, "three crates per agent belt plus the outbound belt"

# export: requests you typed go to train.jsonl, agent briefs to their own file; noise and duplicates are dropped
LONG = "please explain in detail how the conveyor belt scheduler decides which crate goes first"


def turn(prompt, cwd=None, media=()):
    return [("user", prompt, None, None, cwd, list(media)), ("assistant", "an answer", None, None, cwd)]


f.save("codex", "typed", turn(LONG) + turn("done?")
       + turn("This session is being continued from a previous conversation that ran out of context. Summary follows.")
       + turn("Another Claude session sent a message: <teammate-message> idle notification from a worker </teammate-message>"))
f.save("codex", "typed2", turn(LONG.upper() + "  "))  # the same request again: a duplicate
f.save("claude-code", "agent-7", turn("Investigate the failing importer and report every root cause you can confirm"))
f.export()
train = list(f.lines(f.STORE / "train.jsonl"))
briefs = list(f.lines(f.STORE / "train_briefs.jsonl"))
assert [(x["kind"], x["messages"][0]["content"]) for x in train] == [("asked", LONG)], train
assert [x["kind"] for x in briefs] == ["brief"] and briefs[0]["source"] == "claude-code", briefs

# exclude: removes what is stored from that folder, blocks re-collection, leaves a sibling folder alone
app, sibling = tmp / "work" / "app", tmp / "work" / "app2"
secret_pic, shared_pic = (("image/png", base64.b64encode(b).decode()) for b in (b"client diagram", b"pasted png"))
f.save("codex", "inside", turn("q", str(app / "sub"), [secret_pic, shared_pic]))
f.save("codex", "sibling", turn("q", str(sibling)))
assert len(list(f.MEDIA.glob("*/*"))) == 3
f.exclude(str(app))
assert not (f.SESSIONS / "codex/inside.jsonl").exists() and (f.SESSIONS / "codex/sibling.jsonl").exists()
left = sorted(p.read_bytes() for p in f.MEDIA.glob("*/*"))
assert left == [b"pasted jpg", b"pasted png"], "its own image is deleted; one another conversation still uses is kept"
assert f.save("codex", "inside", turn("q", str(app / "sub"), [secret_pic])) == 0 and len(list(f.MEDIA.glob("*/*"))) == 2
assert f.save("codex", "inside", turn("q", str(app / "sub"))) == 0 and not (f.SESSIONS / "codex/inside.jsonl").exists()
assert f.save("codex", "inside", turn("q", str(app.resolve() / "x"))) == 0, "the resolved form of the folder is excluded too"
assert f.save("codex", "nofolder", turn("q")) == 2, "records without a folder are never excluded"
f.exclude(str(app), remove=True)
assert f.excluded() == [] and f.save("codex", "inside", turn("q", str(app / "sub"))) == 2

# OCR pairs: indexed by path, test beats train on overlap, missing images and prediction files ignored
ds = tmp / "ocrset"
for name in ("a.jpg", "b.jpg", "sub/c.jpg", "raw/z.jpg"):
    (ds / name).parent.mkdir(parents=True, exist_ok=True)
    (ds / name).write_bytes(b"img")
write(ds / "train_manifest.jsonl", [{"image": "a.jpg", "text": "ก"}, {"image": str(ds / "b.jpg"), "text": "ข", "source": "s"},
                                    {"image": "gone.jpg", "text": "x"}, {"image": "a.jpg", "text": ""}])
write(ds / "eval/test_set/manifest.jsonl", [{"image": "../../b.jpg", "text": "ข"}])
write(ds / "sub/val.jsonl", [{"image": "c.jpg", "text": "ค"}])
write(ds / "preds/model.jsonl", [{"id": 1, "pred": "x"}])
write(ds / "raw/dump.jsonl", [{"image": "z.jpg", "text": "z"}])
f.add_ocr(ds)
got = {Path(r["image"]).name: r["split"] for r in f.lines(f.STORE / "ocr/ocrset.jsonl")}
assert got == {"a.jpg": "train", "b.jpg": "test", "c.jpg": "val"}, got
prof = json.loads((f.STORE / "profile.json").read_text(encoding="utf-8"))
assert (prof["ocr_pairs"], prof["ocr_test"]) == (2, 1), prof
assert "2 of 2,000 examples" in (f.STORE / "index.html").read_text(encoding="utf-8")

# install: registers with the CLIs it finds, merges into the desktop config without losing other servers
fake = tmp / "fakehome"
bindir = tmp / "bin"
bindir.mkdir()
for exe in ("claude", "codex"):  # no gemini: must be reported as not installed
    if os.name == "nt":
        (bindir / f"{exe}.cmd").write_text('@echo %~n0 %*>> "%USERPROFILE%\\calls.log"\r\n', encoding="utf-8")
    else:
        (bindir / exe).write_text('#!/bin/sh\necho "$(basename "$0") $@" >> "$HOME/calls.log"\n', encoding="utf-8")
        (bindir / exe).chmod(0o755)
desk = [fake / "Library/Application Support/Claude", fake / ".config/Claude"]
for d in desk:
    d.mkdir(parents=True)
    (d / "claude_desktop_config.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "preferences": {"a": 1}}), encoding="utf-8")
# a bare environment: the real claude/codex/gemini on this machine must not be reachable
system = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] if os.name == "nt" else ["/usr/bin", "/bin"]
env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
env.update(HOME=str(fake), USERPROFILE=str(fake), APPDATA=str(fake / ".config"), PATH=os.pathsep.join([str(bindir), *system]))
script = str(Path(f.__file__).resolve())
out = subprocess.run([sys.executable, script, "install"], capture_output=True, text=True, env=env, check=True).stdout
calls = (fake / "calls.log").read_text(encoding="utf-8")
assert "claude mcp add --scope user data-factory --" in calls and "codex mcp add data-factory --" in calls, calls
assert calls.count(script + " mcp") == 2 and "Gemini CLI      not installed" in out, out
cfg = next(d for d in desk if (d / "claude_desktop_config.json.bak").exists()) / "claude_desktop_config.json"
merged = json.loads(cfg.read_text(encoding="utf-8"))
assert set(merged["mcpServers"]) == {"other", "data-factory"} and merged["preferences"] == {"a": 1}, merged
assert merged["mcpServers"]["data-factory"]["args"] == [script, "mcp"]
cfg.write_text("{ broken", encoding="utf-8")  # an unreadable config must be left alone
out = subprocess.run([sys.executable, script, "install", "desktop"], capture_output=True, text=True, env=env, check=True).stdout
assert "config left untouched" in out and cfg.read_text(encoding="utf-8") == "{ broken" and "Codex" not in out, out

print("ok")
