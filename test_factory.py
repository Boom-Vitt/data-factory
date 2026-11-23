"""Run: python3 test_factory.py  (uses a temp store and a fake home; touches nothing real)"""
import json, os, subprocess, sys, tempfile, zipfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["DATA_FACTORY_HOME"] = str(tmp / "store")
os.environ["DATA_FACTORY_NO_OPEN"] = "1"  # never pop a browser from the test
import factory as f

KEY = "sk-" + "a" * 30


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")


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

# unchanged sources are skipped on the next run
before = (f.SESSIONS / "codex/r1.jsonl").stat().st_mtime_ns
f.collect(home)
assert (f.SESSIONS / "codex/r1.jsonl").stat().st_mtime_ns == before

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
    {"sender": "human", "text": "cq"}, {"sender": "assistant", "text": "ca"}]}]))
f.import_export(j)
assert [(r["role"], r["text"]) for r in read("claude-chat/u1.jsonl")] == [("user", "cq"), ("assistant", "ca")]

# turns -> training pairs
assert list(f.pairs([{"role": "user", "text": "lost"}, {"role": "user", "text": "q"},
                     {"role": "assistant", "text": "a1"}, {"role": "assistant", "text": "a2"},
                     {"role": "user", "text": "unanswered"}])) == [("q", "a1\n\na2")]
f.export()
assert sum(1 for _ in f.lines(f.STORE / "train.jsonl")) == 6

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
assert len(logged) == 1 and KEY not in logged[0].read_text(), "source must stay inside the store, secrets redacted"
assert not [p for p in tmp.rglob("*.jsonl") if f.STORE not in p.parents and home not in p.parents]

# the factory page: rebuilt by collect and by every tool call, prompt text escaped
page = (f.STORE / "index.html").read_text()
assert "index.html" in r[7]["result"]["content"][0]["text"]
assert "Claude Code" in page and "Grok" in page and "__" not in page
assert "&lt;script&gt;alert(1)" in page and "<script>alert" not in page
assert page.count('class="rec ') == 9 and "Gemma 4" in page and "Claude Code" in page
prof = json.loads((f.STORE / "profile.json").read_text())
assert prof["asked"] == 0 and prof["steer"] >= 4, prof  # every fixture prompt is a short one
assert "import export.zip" not in page and "factory_log" not in page, "tips only for agents not yet collected"
assert page.count('class="rider"') == 3 * 7 + 3, "three crates per agent belt plus the outbound belt"

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
prof = json.loads((f.STORE / "profile.json").read_text())
assert (prof["ocr_pairs"], prof["ocr_test"]) == (2, 1), prof
assert "2 of 2,000 examples" in (f.STORE / "index.html").read_text()

# install: registers with the CLIs it finds, merges into the desktop config without losing other servers
fake = tmp / "fakehome"
bindir = tmp / "bin"
bindir.mkdir()
for exe in ("claude", "codex"):  # no gemini: must be reported as not installed
    (bindir / exe).write_text('#!/bin/sh\necho "$(basename "$0") $@" >> "$HOME/calls.log"\n')
    (bindir / exe).chmod(0o755)
desk = [fake / "Library/Application Support/Claude", fake / ".config/Claude"]
for d in desk:
    d.mkdir(parents=True)
    (d / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "preferences": {"a": 1}}))
env = {"HOME": str(fake), "PATH": f"{bindir}:/usr/bin:/bin", "APPDATA": str(fake / ".config")}
script = str(Path(f.__file__).resolve())
out = subprocess.run([sys.executable, script, "install"], capture_output=True, text=True, env=env, check=True).stdout
calls = (fake / "calls.log").read_text()
assert "claude mcp add --scope user data-factory --" in calls and "codex mcp add data-factory --" in calls, calls
assert calls.count(script + " mcp") == 2 and "Gemini CLI      not installed" in out, out
cfg = next(d for d in desk if (d / "claude_desktop_config.json.bak").exists()) / "claude_desktop_config.json"
merged = json.loads(cfg.read_text())
assert set(merged["mcpServers"]) == {"other", "data-factory"} and merged["preferences"] == {"a": 1}, merged
assert merged["mcpServers"]["data-factory"]["args"] == [script, "mcp"]
cfg.write_text("{ broken")  # an unreadable config must be left alone
out = subprocess.run([sys.executable, script, "install", "desktop"], capture_output=True, text=True, env=env, check=True).stdout
assert "config left untouched" in out and cfg.read_text() == "{ broken" and "Codex" not in out, out

print("ok")
