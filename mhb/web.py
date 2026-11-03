"""Small password-protected web front end: start runs, watch progress, read reports.

Start it with:  DEMO_PASSWORD=... python3 -m mhb.web
One run at a time. Raw replies and verdicts are never served, because they hold dataset text.
"""
import csv
import hashlib
import hmac
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .util import load_env

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
LOGS = RESULTS / "web_logs"
COOKIE = "mhb_session"
SESSION_DAYS = 14
DOWNLOADS = {"report.md", "summary.json", "scores_by_task.csv", "scores_by_axis.csv"}
RUN_ID = re.compile(r"^(mock-)?\d{8}-\d{6}$")

load_env(ROOT / ".env")
PASSWORD = os.environ.get("DEMO_PASSWORD", "")
SECRET = hashlib.sha256(("mhb-web:" + os.environ.get("SESSION_SECRET", PASSWORD)).encode()).digest()
MAX_TASKS = int(os.environ.get("MAX_TASKS", "150"))


# --- Sessions: a signed cookie that holds only an expiry time ---

def make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(SECRET, exp.encode(), "sha256").hexdigest()


def valid_token(token: str) -> bool:
    exp, _, sig = token.partition(".")
    good = hmac.new(SECRET, exp.encode(), "sha256").hexdigest()
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, good)


# --- The one background run ---

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.log = None
        self.args = []
        self.started = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list) -> str | None:
        with self.lock:
            if self.running():
                return "A run is in progress. Wait for it to finish, or stop it."
            LOGS.mkdir(parents=True, exist_ok=True)
            self.log = LOGS / time.strftime("%Y%m%d-%H%M%S.log")
            self.args, self.started = args, time.time()
            out = self.log.open("w")
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen([sys.executable, "-m", "mhb", "--yes", *args], cwd=ROOT, env=env,
                                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            out.close()
            return None

    def stop(self) -> None:
        with self.lock:
            if self.running():
                self.proc.terminate()

    def status(self) -> dict:
        if not self.log:
            return {"state": "idle"}
        text = self.log.read_text(errors="replace") if self.log.exists() else ""
        lines = []
        for line in text.replace("\r", "\n").splitlines():
            # Keep only the newest progress line of each step ("  replies: 3/40 ...").
            m = re.match(r"\s+([\w ]+): \d+/\d+", line)
            if m and lines and lines[-1].startswith(m.group(0).split(":")[0] + ":"):
                lines[-1] = line
            elif line.strip():
                lines.append(line)
        done = re.search(r"Report: results/([^/\s]+)/report\.md", text)
        if self.running():
            state = "running"
        elif done:
            state = "done"
        else:
            state = "stopped" if self.proc.returncode in (-15, 143) else "failed"
        return {"state": state, "args": " ".join(self.args), "elapsed": int(time.time() - self.started),
                "tail": "\n".join(lines[-16:]), "run_id": done.group(1) if done else None}


JOB = Job()


# --- Data for the pages ---

def config() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text())


def list_runs() -> list:
    runs = []
    for d in sorted(RESULTS.glob("*/summary.json"), reverse=True):
        try:
            data = json.loads(d.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if RUN_ID.match(d.parent.name):
            runs.append((d.parent.name, data))
    return runs


def pct(x) -> str:
    return "–" if x is None or x != x else f"{100 * x:.1f}%"


# --- A small Markdown renderer for the reports: headings, quotes, tables, code, bold, inline code ---

def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def markdown(md: str) -> str:
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append(f"<pre>{html.escape(chr(10).join(lines[i + 1:j]))}</pre>")
            i = j + 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            head = "".join(f"<th>{inline(c)}</th>" for c in rows[0]) if rows else ""
            body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
            out.append(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            continue
        if m := re.match(r"^(#{1,3}) (.*)", line):
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(out)


# --- Pages ---

STYLE = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#3e63dd;--accent-text:#fff;
--bad:#c23b3b;--good:#2f7d4f;--code:#f0f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--text:#ececee;--muted:#9a9aa2;--line:#2e2e33;
--accent:#7b97f0;--accent-text:#0d0d10;--bad:#f07a7a;--good:#6fcf97;--code:#26262a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:16px 16px 48px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0 16px}
header h1{font-size:20px;margin:0}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 16px}
.card h2{font-size:17px;margin:0 0 12px}
label{display:block;font-weight:600;font-size:14px;margin:12px 0 6px}
.hint{color:var(--muted);font-size:13px;font-weight:400;overflow-wrap:anywhere}
input[type=number],input[type=password],select{width:100%;font-size:16px;padding:12px;border-radius:10px;
border:1px solid var(--line);background:var(--bg);color:var(--text)}
.checks label{display:flex;align-items:center;gap:12px;font-weight:400;margin:0;padding:10px 0;border-bottom:1px solid var(--line)}
.checks label:last-child{border-bottom:0}
input[type=checkbox]{width:22px;height:22px;accent-color:var(--accent)}
button,.btn{display:block;width:100%;text-align:center;font-size:16px;font-weight:600;padding:14px;margin-top:16px;
border:0;border-radius:12px;background:var(--accent);color:var(--accent-text);text-decoration:none;cursor:pointer}
button.secondary{background:transparent;color:var(--bad);border:1px solid var(--line)}
.linkbtn{background:none;border:0;color:var(--accent);padding:0;margin:0;width:auto;font-weight:400;font-size:15px}
pre{background:var(--code);padding:12px;border-radius:10px;overflow-x:auto;font-size:13px;white-space:pre-wrap;word-break:break-word}
code{background:var(--code);padding:1px 5px;border-radius:5px;font-size:.9em;word-break:break-all}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0 16px}
table{border-collapse:collapse;font-size:14px;min-width:100%}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}
blockquote{margin:8px 0;padding:10px 12px;border-left:3px solid var(--accent);background:var(--code);border-radius:6px}
.run{display:block;padding:12px 0;border-bottom:1px solid var(--line);text-decoration:none;color:var(--text)}
.run:last-child{border-bottom:0}
.run .id{font-weight:600}.run .meta{color:var(--muted);font-size:13px}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--code);color:var(--muted);margin-left:6px}
.state{font-weight:600}.state.failed,.state.stopped{color:var(--bad)}.state.done{color:var(--good)}
.error{color:var(--bad);font-weight:600}
.card h3{font-size:15px;margin:18px 0 2px}.card h3:first-of-type{margin-top:4px}
.bars{margin:8px 0 4px}
.bar{display:grid;grid-template-columns:minmax(0,9.5em) 1fr 3.6em;align-items:center;gap:8px;margin:7px 0;font-size:14px}
.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:14px;background:var(--code);border-radius:7px;overflow:hidden}
.bar-fill{height:100%;border-radius:7px}
.bar-value{text-align:right;font-variant-numeric:tabular-nums}.bar-value.neg{color:var(--bad)}
.c0{background:#4c6ef5}.c1{background:#e8590c}.c2{background:#2b8a3e}.c3{background:#ae3ec9}.c4{background:#1098ad}.c5{background:#c2255c}
@media (prefers-color-scheme:dark){.c0{background:#7b97f0}.c1{background:#ff922b}.c2{background:#51cf66}.c3{background:#da77f2}.c4{background:#3bc9db}.c5{background:#f06595}}
.report h2{font-size:18px;margin-top:28px}.report h3{font-size:16px}
"""


def page(title: str, body: str, auth: bool = True) -> str:
    logout = ('<form method="post" action="/logout"><button class="linkbtn">Log out</button></form>' if auth else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>"
            f'<header><h1><a href="/" style="color:inherit;text-decoration:none">MentalHealthBench</a></h1>{logout}</header>'
            f"{body}</main></body></html>")


def login_page(error: str = "") -> str:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return page("Log in", f"""<div class="card"><h2>Log in</h2>{err}
<form method="post" action="/login"><label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button>Log in</button></form></div>""", auth=False)


def home_page(error: str = "") -> str:
    cfg = config()
    apps = "".join(f'<label><input type="checkbox" name="apps" value="{html.escape(a["name"])}" checked>'
                   f'<span>{html.escape(a["name"])}<br><span class="hint">{html.escape(a.get("model", a["provider"]))}</span></span></label>'
                   for a in cfg["apps"])
    mode = cfg["judge"].get("mode", "decisions")
    opts = "".join(f'<option value="{m}"{" selected" if m == mode else ""}>{label}</option>'
                   for m, label in (("decisions", "Decisions API (fast, cheap)"),
                                    ("responses", "Paper's grader prompt (exact replication, slower)")))
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    runs = "".join(run_row(rid, data) for rid, data in list_runs()) or '<p class="hint">No runs yet.</p>'
    return page("MentalHealthBench", f"""
<div class="card" id="status"><h2>Current run</h2><div id="status-body">Loading…</div></div>
<div class="card"><h2>Start a run</h2>{err}
<form method="post" action="/run">
<label for="limit">Tasks <span class="hint">1 to {MAX_TASKS}. Each task is sent to every app you pick. Start small.</span></label>
<input id="limit" name="limit" type="number" inputmode="numeric" min="1" max="{MAX_TASKS}" value="10" required>
<label>Apps</label><div class="checks">{apps}</div>
<label for="mode">Judge</label><select id="mode" name="mode">{opts}</select>
<div class="checks" style="margin-top:12px"><label><input type="checkbox" name="mock" value="1">
Mock run <span class="hint">fake replies and grades, no cost</span></label></div>
<button>Start run</button></form>
<p class="hint">Finished work is cached. A repeated run costs nothing for the replies and grades it already has.</p></div>
<div class="card"><h2>Results</h2>{runs}</div>
<script>
async function poll(){{
  let s; try {{ s = await (await fetch('/api/status')).json(); }} catch(e) {{ return setTimeout(poll, 5000); }}
  const b = document.getElementById('status-body');
  const esc = t => t.replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]);
  if (s.state === 'idle') {{ b.innerHTML = '<p class="hint">No run since the server started.</p>'; return; }}
  let h = `<p><span class="state ${{s.state}}">${{s.state}}</span> · ${{Math.floor(s.elapsed/60)}}m ${{s.elapsed%60}}s<br><span class="hint">${{esc(s.args)}}</span></p><pre>${{esc(s.tail)}}</pre>`;
  if (s.state === 'running') h += '<form method="post" action="/stop"><button class="secondary">Stop run</button></form>';
  if (s.run_id) h += `<a class="btn" href="/runs/${{s.run_id}}">Open report</a>`;
  b.innerHTML = h;
  if (s.state === 'running') setTimeout(poll, 3000);
  else if (!sessionStorage.getItem('seen-' + s.run_id)) {{ try {{ sessionStorage.setItem('seen-' + s.run_id, 1); }} catch(e) {{}} if (s.run_id) location.reload(); }}
}}
poll();
</script>""")


def run_row(rid: str, data: dict) -> str:
    meta = data.get("meta", {})
    best = ", ".join(f"{html.escape(a)} {pct(s.get('clipped'))}" for a, s in data.get("summary", {}).items())
    judge = meta.get("judge", {})
    tag = '<span class="pill">mock</span>' if meta.get("mock") else ""
    return (f'<a class="run" href="/runs/{rid}"><div class="id">{rid}{tag}</div>'
            f'<div class="meta">{meta.get("n_tasks", "?")} tasks · judge {html.escape(str(judge.get("mode", "")))}</div>'
            f'<div class="meta">{best}</div></a>')


def bars(rows: list, color: dict, scale: float) -> str:
    """Horizontal bars, one per app, each labeled with its value."""
    out = []
    for app, v in rows:
        width = 0 if v is None or v != v else max(0.0, min(1.0, v / scale)) * 100
        neg = " neg" if v is not None and v == v and v < 0 else ""
        out.append(f'<div class="bar"><div class="bar-label">{html.escape(app)}</div><div class="bar-track">'
                   f'<div class="bar-fill c{color.get(app, 0)}" style="width:{width:.1f}%"></div></div>'
                   f'<div class="bar-value{neg}">{pct(v)}</div></div>')
    return '<div class="bars">' + "".join(out) + "</div>"


def report_page(rid: str) -> str | None:
    path = RESULTS / rid / "report.md"
    if not RUN_ID.match(rid) or not path.is_file():
        return None
    try:
        summary = json.loads((RESULTS / rid / "summary.json").read_text()).get("summary", {})
    except (OSError, json.JSONDecodeError):
        summary = {}
    axes = {}
    if (RESULTS / rid / "scores_by_axis.csv").is_file():
        with (RESULTS / rid / "scores_by_axis.csv").open() as f:
            for row in csv.DictReader(f):
                axes.setdefault(row["axis"], {})[row["app"]] = float(row["signed"])
    color = {a: i % 6 for i, a in enumerate(summary)}
    top = ""
    if summary:
        metrics = [("Score", "clipped", "Higher is better. The paper's headline number."),
                   ("Harm rate", "harm_rate", "Lower is better. Share of replies where a penalty fired."),
                   ("Penalty", "penalty", "Lower is better. Penalty points as a share of possible points.")]
        groups = "".join(f'<h3>{title}</h3><p class="hint">{hint}</p>'
                         + bars([(a, s.get(key)) for a, s in summary.items()], color, 1.0)
                         for title, key, hint in metrics)
        top += f'<div class="card"><h2>Overview</h2>{groups}</div>'
    if axes:
        scale = max((v for by_app in axes.values() for v in by_app.values()), default=0) or 1
        groups = "".join(f'<h3>{html.escape(axis.replace("_", " "))}</h3>'
                         + bars([(a, by_app.get(a)) for a in summary or by_app], color, scale)
                         for axis, by_app in sorted(axes.items()))
        top += ('<div class="card"><h2>By behavior axis</h2><p class="hint">Signed axis score: points earned minus '
                'penalties, as a share of the task\'s possible points. The axes add up to the signed score, so the '
                'bars are scaled to the largest axis value. Below zero means penalties outweighed points.</p>'
                f'{groups}</div>')
    files = " · ".join(f'<a href="/runs/{rid}/{f}">{f}</a>' for f in sorted(DOWNLOADS) if (RESULTS / rid / f).is_file())
    return page(f"Run {rid}", f'<p><a href="/">← All runs</a></p>{top}<div class="card report">{markdown(path.read_text())}'
                              f'<p class="hint">Download: {files}</p></div>')


# --- HTTP ---

class Handler(BaseHTTPRequestHandler):
    server_version = "mhb"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def authed(self) -> bool:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and valid_token(value):
                return True
        return False

    def send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict = None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to: str, headers: dict = None):
        self.send(303, "", headers={"Location": to, **(headers or {})})

    def form(self) -> dict:
        n = min(int(self.headers.get("Content-Length") or 0), 10_000)
        return parse_qs(self.rfile.read(n).decode(errors="replace"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/login":
            return self.send(200, login_page())
        if not self.authed():
            return self.redirect("/login")
        if path == "/":
            return self.send(200, home_page())
        if path == "/api/status":
            return self.send(200, json.dumps(JOB.status()), "application/json")
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "runs":
            body = report_page(parts[1])
            return self.send(200, body) if body else self.send(404, page("Not found", "<p>No such run.</p>"))
        if len(parts) == 3 and parts[0] == "runs" and RUN_ID.match(parts[1]) and parts[2] in DOWNLOADS:
            f = RESULTS / parts[1] / parts[2]
            if f.is_file():
                ctype = {"md": "text/markdown", "json": "application/json", "csv": "text/csv"}[f.suffix[1:]]
                return self.send(200, f.read_bytes(), ctype + "; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="{parts[1]}-{parts[2]}"'})
        self.send(404, page("Not found", "<p>Not found.</p>"))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            given = (self.form().get("password") or [""])[0]
            if PASSWORD and hmac.compare_digest(given.encode(), PASSWORD.encode()):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"{COOKIE}={make_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}{secure}"
                return self.redirect("/", {"Set-Cookie": cookie})
            time.sleep(1)  # slows down password guessing
            return self.send(401, login_page("Wrong password."))
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if not self.authed():
            return self.redirect("/login")
        if path == "/stop":
            JOB.stop()
            return self.redirect("/")
        if path == "/run":
            f = self.form()
            known = {a["name"] for a in config()["apps"]}
            apps = [a for a in f.get("apps", []) if a in known]
            try:
                limit = int((f.get("limit") or ["0"])[0])
            except ValueError:
                limit = 0
            mode = (f.get("mode") or ["decisions"])[0]
            if not apps:
                return self.send(400, home_page("Pick at least one app."))
            if not 1 <= limit <= MAX_TASKS or mode not in ("decisions", "responses"):
                return self.send(400, home_page(f"Tasks must be from 1 to {MAX_TASKS}."))
            args = ["--limit", str(limit), "--apps", ",".join(apps), "--judge-mode", mode]
            if f.get("mock"):
                args.append("--mock")
            error = JOB.start(args)
            return self.send(409, home_page(error)) if error else self.redirect("/")
        self.send(404, page("Not found", "<p>Not found.</p>"))


def main():
    if not PASSWORD:
        sys.exit("Set DEMO_PASSWORD (in .env or the environment) before you start the web app.")
    port = int(os.environ.get("PORT", "8080"))
    RESULTS.mkdir(exist_ok=True)
    print(f"Listening on http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
