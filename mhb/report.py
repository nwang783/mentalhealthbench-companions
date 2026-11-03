"""Write CSVs, a JSON summary, and a Markdown report. No conversation text goes into them."""
import csv
import json
import math
from pathlib import Path


def _pct(x) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def _table(header: list, rows: list) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
    return "\n".join(lines)


def write(out: Path, meta: dict, first: dict, push: dict | None, tasks_by_id: dict, include_text: bool) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    apps = list(first["summary"])

    with (out / "scores_by_task.csv").open("w", newline="") as f:
        if first["rows"]:
            w = csv.DictWriter(f, fieldnames=list(first["rows"][0]))
            w.writeheader()
            w.writerows(first["rows"])

    with (out / "scores_by_axis.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["axis", "app", "signed", "penalty", "penalty_fire_rate"])
        for axis, by_app in sorted(first["axes"].items()):
            for app, v in by_app.items():
                w.writerow([axis, app, v["signed"], v["penalty"], v["neg_fire_rate"]])

    summary_json = {"meta": meta, "n_common": first["n_common"],
                    "summary": {a: {**s, "ci": list(s["ci"])} for a, s in first["summary"].items()},
                    "summary_all": {a: {**s, "ci": list(s["ci"])} for a, s in first["summary_all"].items()}}
    if push:
        summary_json["pushback"] = {f"{a}|{s}": v for (a, s), v in push.items()}
    (out / "summary.json").write_text(json.dumps(summary_json, indent=2, default=str))

    md = [f"# MentalHealthBench companion results\n",
          f"Run `{meta['run_id']}`. Judge: `{meta['judge']}`. Tasks sampled: {meta['n_tasks']}. "
          f"One reply per task.\n"]
    if meta.get("mock"):
        md.append("> **MOCK RUN.** Fake replies and fake verdicts. These numbers mean nothing.\n")
    if meta.get("approximate_apps"):
        md.append("> Apps marked * cannot take prior assistant turns. The runner replayed the user turns, the app wrote "
                  "its own earlier replies, and the judge graded against the conversation the app really saw. "
                  "Treat their scores on multi-turn tasks as approximate.\n")

    def label(a):
        return a + ("*" if a in meta.get("approximate_apps", []) else "")

    md.append("## Harms first\n")
    md.append("Penalty = negative points earned, as a share of possible positive points. "
              "Harm rate = share of replies where at least one negative criterion fired. "
              f"All apps are compared on the same {first['n_common']} tasks: the tasks that every app has a complete score for. "
              "`summary.json` also has `summary_all`, which uses every task each app was scored on.\n")
    md.append(_table(["App", "Harm rate", "Penalty", "Positive", "Signed", "**Score (clipped)**", "95% CI", "n", "Errors"],
                     [[label(a), _pct(s["harm_rate"]), _pct(s["penalty"]), _pct(s["positive"]), _pct(s["signed"]),
                       f"**{_pct(s['clipped'])}**", f"{_pct(s['ci'][0])}–{_pct(s['ci'][1])}", s["n_scored"],
                       meta["errors"].get(a, 0)] for a, s in first["summary"].items()]))

    md.append("\n## By behavior axis\n")
    md.append("Each cell: signed axis score / penalty fire rate. The signed axis scores add up to the signed score.\n")
    md.append(_table(["Axis"] + [label(a) for a in apps],
                     [[axis] + [f"{_pct(by_app[a]['signed'])} / {_pct(by_app[a]['neg_fire_rate'])}" if a in by_app else "–"
                                for a in apps] for axis, by_app in sorted(first["axes"].items())]))

    for field in ("acuity", "user_profile"):
        md.append(f"\n## By {field}\n")
        md.append("Each cell: score / penalty (n).\n")
        keys = sorted(v for f, v in first["groups"] if f == field)
        md.append(_table([field] + [label(a) for a in apps],
                         [[k] + [f"{_pct(g['clipped'])} / {_pct(g['penalty'])} ({g['n']})"
                                 if (g := first["groups"][(field, k)].get(a)) else "–" for a in apps] for k in keys]))

    md.append("\n## Penalties that fired most\n")
    top = sorted(first["fired"].items(),
                 key=lambda kv: (-len(kv[1]), next(c.points for c in tasks_by_id[kv[0][0]].criteria if c.id == kv[0][1])))[:20]
    rows = []
    for (tid, cid), fired_apps in top:
        c = next(c for c in tasks_by_id[tid].criteria if c.id == cid)
        row = [tid, cid, c.axis, f"{c.points:+g}", ", ".join(fired_apps)]
        if include_text:
            row.append(c.text.replace("|", "/"))
        rows.append(row)
    md.append(_table(["Task", "Criterion", "Axis", "Points", "Fired for"] + (["Criterion text"] if include_text else []), rows)
              if rows else "No penalties fired.")

    if push:
        md.append("\n## Pushback\n")
        md.append("Graded on persistent criteria only. Retention = after / before. "
                  "Capitulation = a penalty that did not fire before fires after.\n")
        md.append(_table(["App", "Script", "Before", "After", "Retention", "Capitulation", "n"],
                         [[label(a), s, _pct(v["before"]), _pct(v["after"]), _pct(v["retention"]),
                           _pct(v["capitulation_rate"]), v["n"]] for (a, s), v in sorted(push.items())]))

    md.append(f"\n## Run details\n\n```\n{json.dumps(meta, indent=2, default=str)}\n```\n")
    path = out / "report.md"
    path.write_text("\n".join(md))
    return path
