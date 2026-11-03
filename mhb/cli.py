"""Entry point: load, sample, preflight, generate, judge, score, report."""
import argparse
import json
import os
import shutil
import sys
import time
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import pushback, report, score
from .adapters import make_adapter
from .dataset import Criterion, describe, load_tasks, sample
from .judge import Judge, format_prefix
from .util import APIError, Cache, Progress, cache_key, load_env

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = {"", "sk-...", "your-key-here"}


def parse_args(argv):
    p = argparse.ArgumentParser(prog="run.sh", description="Run MentalHealthBench against companion apps.")
    p.add_argument("--mock", action="store_true", help="offline test on the synthetic sample: no keys, no cost")
    p.add_argument("--inspect", action="store_true", help="print dataset label counts and exit")
    p.add_argument("--yes", "-y", action="store_true", help="skip the cost confirmation")
    p.add_argument("--limit", type=int, default=0, help="use only the first N sampled tasks (quick trial)")
    p.add_argument("--apps", default="", help="comma-separated app names from config.toml (default: all)")
    p.add_argument("--max-tasks", type=int, help="override [sample].max_tasks (0 = every task that matches a group)")
    p.add_argument("--judge-mode", choices=["decisions", "responses"], help="override [judge].mode")
    p.add_argument("--config", default="config.toml")
    return p.parse_args(argv)


def run_jobs(label, jobs, workers_for, fn):
    """jobs: list of (pool_name, payload). Each pool gets its own thread pool size.
    fn(payload) -> (result, cached: bool). Returns {payload_key: result or Exception}."""
    results = {}
    if not jobs:
        return results
    progress = Progress(label, len(jobs))
    pools = {name: ThreadPoolExecutor(max_workers=workers_for(name)) for name in {n for n, _ in jobs}}
    futures = {pools[name].submit(fn, payload): payload for name, payload in jobs}
    try:
        for fut in as_completed(futures):
            payload = futures[fut]
            try:
                value, cached = fut.result()
                results[payload["key"]] = value
                progress.tick(cached=cached)
            except APIError as e:
                results[payload["key"]] = e
                progress.tick(error=True)
    finally:
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
    return results


def preflight(adapters, judge, mock):
    """One tiny call to each app and to the judge. Skips apps that fail."""
    if mock:
        return adapters
    print("Preflight checks:")
    ping = [{"role": "user", "content": "Reply with one word: ok"}]
    try:
        judge.grade(ping, "ok", [Criterion(id="ping", text="The reply contains the word ok.", points=1, axis="ping")])
        print(f"  judge {judge.model} ({judge.mode}): ok")
    except APIError as e:
        sys.exit(f"  judge {judge.model}: FAILED\n    {e}\n"
                 f"Fix [judge].model in config.toml or OPENAI_API_KEY in .env, then run again.")
    good = []
    for a in adapters:
        try:
            a.respond(ping)
            print(f"  {a.name} ({a.model}): ok")
            good.append(a)
        except APIError as e:
            print(f"  {a.name} ({a.model}): FAILED, skipping this app\n    {e}")
    if not good:
        sys.exit("No app passed the preflight check. Fix the model ids in config.toml or the key in .env.")
    return good


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    load_env(ROOT / ".env")
    cfg = tomllib.loads((ROOT / args.config).read_text())
    if args.max_tasks is not None:
        cfg["sample"]["max_tasks"] = args.max_tasks
    if args.judge_mode:
        cfg["judge"]["mode"] = args.judge_mode
    mock = args.mock

    # --- Load and sample tasks ---
    ds_path = ROOT / ("data/sample" if mock else cfg["dataset"]["path"])
    tasks, canary_hits = load_tasks(ds_path, cfg["dataset"].get("canary_regex", "")) if ds_path.exists() else ([], 0)
    if not tasks:
        print(f"No tasks found in {ds_path.relative_to(ROOT)}/.\n"
              "Download MentalHealthBench and put its .jsonl or .json file in that folder.\n"
              "To test the pipeline first, with no keys and no cost, run:  ./run.sh --mock")
        return 1
    if args.inspect:
        print(describe(tasks))
        return 0
    selected = tasks if mock else sample(tasks, cfg["sample"])
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print("The sample groups in config.toml matched no tasks. Run ./run.sh --inspect to see the label values.")
        return 1
    by_id = {t.id: t for t in selected}

    # --- Build apps and judge, check keys ---
    wanted = {n.strip() for n in args.apps.split(",") if n.strip()}
    app_cfgs = [a for a in cfg["apps"] if not wanted or a["name"] in wanted]
    adapters = [make_adapter(a, cfg.get("generation", {}), ROOT, mock) for a in app_cfgs]
    judge = Judge(cfg["judge"], ROOT, mock)
    if not mock:
        needed = {"OPENAI_API_KEY"} | {a.key_env for a in adapters}
        missing = sorted(k for k in needed if os.environ.get(k, "").strip() in PLACEHOLDER)
        if missing:
            print(f"Missing in .env: {', '.join(missing)}")
            return 1
    adapters = preflight(adapters, judge, mock)
    by_name = {a.name: a for a in adapters}

    push_cfg = cfg.get("pushback", {})
    push_on = bool(push_cfg.get("enabled"))
    scripts = {s["id"]: s["text"] for s in push_cfg.get("scripts", [])} if push_on else {}
    push_apps = [a for a in adapters if a.supports_prefix] if push_on else []
    persistent = pushback.persistent_criteria(selected, push_cfg, ROOT) if push_on else {}

    # --- Estimate and confirm ---
    runnable = {a.name: [t for t in selected if a.can_run(t)] for a in adapters}
    skipped = {n: len(selected) - len(ts) for n, ts in runnable.items() if len(ts) < len(selected)}

    def n_calls(crits):  # judge requests for one reply
        return -(-len(crits) // judge.batch_size) if crits else 0

    n_resp = sum(len(ts) for ts in runnable.values())
    n_judge = sum(n_calls(t.criteria) for ts in runnable.values() for t in ts)
    n_resp_push = len(selected) * len(push_apps) * len(scripts)
    n_judge_push = sum(n_calls(persistent.get(t.id, [])) for t in selected) * len(push_apps) * len(scripts)
    # Each request carries the rules, the conversation, and the reply (about 2500 chars),
    # plus the criteria it grades.
    judge_tokens = sum((len(judge.template) + len(format_prefix(t.messages)) + 2500) * n_calls(t.criteria)
                       + sum(len(c.text) + 200 for c in t.criteria)
                       for ts in runnable.values() for t in ts) / 4
    print(f"\nTasks: {len(selected)}  ({dict(Counter(t.acuity for t in selected))})")
    print(f"Apps: {', '.join(a.name for a in adapters)}")
    if skipped:
        print(f"Tasks skipped because the app limits message length: {skipped}")
    print(f"App replies: {n_resp + n_resp_push}   Judge requests: {n_judge + n_judge_push}  (mode: {judge.mode})")
    print(f"Approx. judge input tokens: {judge_tokens / 1e6:.2f}M (before cache hits)")
    price = cfg.get("pricing", {}).get(judge.model)
    if price:
        print(f"Approx. judge cost: ${judge_tokens / 1e6 * price['input'] + n_judge * 150 / 1e6 * price.get('output', 0):.2f}")
    if canary_hits:
        print(f"Removed {canary_hits} canary line(s) from the data.")
    if not (mock or args.yes) and sys.stdin.isatty():
        if input("\nContinue? [y/N] ").strip().lower() not in ("y", "yes"):
            return 0

    run_id = ("mock-" if mock else "") + time.strftime("%Y%m%d-%H%M%S")
    out = ROOT / "results" / run_id
    cache = Cache(ROOT / "results" / ("mock_cache.sqlite" if mock else "cache.sqlite"))
    errors = Counter()

    # --- Step 1: one reply per task per app ---
    def gen_job(p):
        key = cache_key("reply", p["app"].fingerprint(), p["messages"])
        hit = cache.get(key)
        if hit:
            return hit, True
        value = p["app"].respond(p["messages"])
        cache.put(key, value)
        return value, False

    print("\nStep 1: app replies")
    jobs = [(a.name, {"key": (a.name, t.id), "app": a, "messages": t.messages}) for a in adapters for t in runnable[a.name]]
    replies = run_jobs("replies", jobs, lambda n: by_name[n].workers, gen_job)

    # --- Step 1b: pushback replies ---
    push_replies = {}
    if push_on:
        jobs = []
        for a in push_apps:
            for t in selected:
                first = replies.get((a.name, t.id))
                if isinstance(first, dict):
                    for sid, text in scripts.items():
                        jobs.append((a.name, {"key": (a.name, t.id, sid), "app": a,
                                              "messages": pushback.build_turn(t, first["text"], text)}))
        print("Step 1b: pushback replies")
        push_replies = run_jobs("pushback replies", jobs, lambda n: by_name[n].workers, gen_job)

    # --- Step 2: judge every criterion, judge.batch_size criteria per request ---
    def judge_job(p):
        key = cache_key("grade", judge.fingerprint(), p["prefix"], p["completion"],
                        [(c.text, c.points) for c in p["criteria"]])
        hit = cache.get(key)
        if hit:
            return hit, True
        value = judge.grade(p["prefix"], p["completion"], p["criteria"])
        cache.put(key, value)
        return value, False

    def batches(key, prefix, completion, criteria):
        for i in range(0, len(criteria), judge.batch_size):
            chunk = criteria[i:i + judge.batch_size]
            yield ("judge", {"key": key + (i,), "prefix": prefix, "completion": completion, "criteria": chunk})

    jobs = []
    for (app, tid), r in replies.items():
        if isinstance(r, dict):
            # Replay apps return the conversation they really saw; grade against that.
            prefix = r.get("prefix") or by_id[tid].messages
            jobs += batches((app, tid), prefix, r["text"], by_id[tid].criteria)
    for (app, tid, sid), r in push_replies.items():
        if isinstance(r, dict):
            prefix = pushback.build_turn(by_id[tid], replies[(app, tid)]["text"], scripts[sid])
            jobs += batches((app, tid, sid), prefix, r["text"], persistent[tid])
    print("Step 2: judging")
    batch_results = run_jobs("judge requests", jobs, lambda _: judge.workers, judge_job)

    # One verdict per criterion: (app, task, crit) or (app, task, script, crit).
    criteria_of = {p["key"]: p["criteria"] for _, p in jobs}
    verdicts = {}
    for bkey, result in batch_results.items():
        for j, c in enumerate(criteria_of[bkey]):
            verdicts[bkey[:-1] + (c.id,)] = result[j] if isinstance(result, list) else result

    # --- Step 3: score and report ---
    for key, v in list(replies.items()) + list(push_replies.items()):
        if isinstance(v, Exception):
            errors[key[0]] += 1
    for key, v in batch_results.items():
        if isinstance(v, Exception):
            errors[key[0]] += 1
    first_grades = {k: v["met"] for k, v in verdicts.items() if len(k) == 3 and isinstance(v, dict)}
    push_grades = {k: v["met"] for k, v in verdicts.items() if len(k) == 4 and isinstance(v, dict)}

    first = score.score_first_turn(selected, [a.name for a in adapters], first_grades,
                                   cfg.get("report", {}).get("bootstrap_samples", 1000))
    push = score.score_pushback(selected, [a.name for a in push_apps], list(scripts), first_grades,
                                push_grades, persistent) if push_on else None

    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    _write_raw(raw, replies, push_replies, verdicts)
    usage = Counter()
    for v in list(replies.values()) + list(push_replies.values()) + list(verdicts.values()):
        if isinstance(v, dict):
            usage["input"] += v["usage"]["input"]
            usage["output"] += v["usage"]["output"]
    meta = {"run_id": run_id, "mock": mock, "judge": judge.fingerprint(), "n_tasks": len(selected),
            "apps": {a.name: a.fingerprint() for a in adapters},
            "approximate_apps": [a.name for a in adapters if not a.supports_prefix],
            "errors": dict(errors), "skipped_tasks": skipped, "tokens_reported_by_apis": dict(usage),
            "sample": cfg["sample"] if not mock else "synthetic sample"}
    path = report.write(out, meta, first, push, by_id, cfg.get("report", {}).get("include_criterion_text", False))
    shutil.copy(path, ROOT / "results" / ("latest_mock_report.md" if mock else "latest_report.md"))

    print(f"\nDone. Report: {path.relative_to(ROOT)}")
    print(f"{'App':<24}{'Harm rate':>11}{'Penalty':>10}{'Score':>9}{'n':>6}")
    for app, s in first["summary"].items():
        print(f"{app:<24}{s['harm_rate']:>10.1%}{s['penalty']:>10.1%}{s['clipped']:>9.1%}{s['n_scored']:>6}")
    if errors:
        print(f"Errors: {dict(errors)}. Run again to retry them; finished work is cached.")
    return 0


def _write_raw(raw: Path, replies: dict, push_replies: dict, verdicts: dict) -> None:
    """Full replies and verdicts. results/ is gitignored; do not share this folder."""
    with (raw / "replies.jsonl").open("w") as f:
        for key, v in list(replies.items()) + list(push_replies.items()):
            f.write(json.dumps({"key": key, **(v if isinstance(v, dict) else {"error": str(v)})}) + "\n")
    with (raw / "verdicts.jsonl").open("w") as f:
        for key, v in verdicts.items():
            f.write(json.dumps({"key": key, **(v if isinstance(v, dict) else {"error": str(v)})}) + "\n")
