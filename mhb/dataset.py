"""Load MentalHealthBench tasks and pick a stratified subset."""
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Criterion:
    id: str
    text: str
    points: float
    axis: str


@dataclass
class Task:
    id: str
    messages: list
    criteria: list
    acuity: str = ""
    user_profile: str = ""
    language: str = ""
    has_prior_context: bool = False

    @property
    def n_messages(self) -> int:
        return sum(1 for m in self.messages if m["role"] != "system")

    @property
    def n_user_turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _axis(value) -> str:
    # Accepts "empathy", ["axis:empathy", "theme:x"], etc.
    if isinstance(value, list):
        tagged = [v.split(":", 1)[1] for v in value if isinstance(v, str) and v.startswith("axis:")]
        value = tagged[0] if tagged else ",".join(map(str, value))
    return str(value or "unknown").removeprefix("axis:")


def _read_records(path: Path) -> list:
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix in (".jsonl", ".json"))
        return [r for f in files for r in _read_records(f)]
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = _first(data, "examples", "data", "tasks", default=[])
    return data


def load_tasks(path: Path, canary_regex: str) -> tuple[list[Task], int]:
    """Returns (tasks, number of canary lines removed)."""
    canary = re.compile(canary_regex) if canary_regex else None
    removed = 0

    def clean(text: str) -> str:
        nonlocal removed
        if not canary:
            return text
        text, n = canary.subn("", text)
        removed += n
        return text

    tasks = []
    for i, rec in enumerate(_read_records(path)):
        raw = _first(rec, "conversation", "messages", "prompt", default=[])
        if isinstance(raw, dict):  # MentalHealthBench: {"conversation": {"messages": [...]}}
            raw = raw.get("messages", [])
        messages = [{"role": m["role"], "content": clean(_text(m.get("content", "")))} for m in raw]
        if not messages or messages[-1]["role"] != "user":
            continue
        criteria = [Criterion(id=str(_first(r, "id", "criterion_id", default=f"c{j}")),
                              text=clean(str(_first(r, "criterion_text", "criterion", "text", default=""))),
                              points=float(_first(r, "points", "weight", default=0)),
                              axis=_axis(_first(r, "behavior_axis", "axis", "tags")))
                    for j, r in enumerate(_first(rec, "rubric_items", "rubrics", "rubric", default=[]))]
        if not criteria:
            continue
        tasks.append(Task(id=str(_first(rec, "id", "prompt_id", "task_id", default=f"task_{i}")),
                          messages=messages, criteria=criteria,
                          acuity=str(rec.get("acuity", "")), user_profile=str(rec.get("user_profile", "")),
                          language=str(rec.get("language", "")),
                          has_prior_context=bool(rec.get("has_prior_context", False))))
    return tasks, removed


def _contains_any(value: str, needles: list) -> bool:
    value = value.lower()
    return any(n.lower() in value for n in needles)


def _in_group(t: Task, g: dict) -> bool:
    if "user_profile" in g and not _contains_any(t.user_profile, g["user_profile"]):
        return False
    if "acuity" in g and not _contains_any(t.acuity, g["acuity"]):
        return False
    if "max_messages" in g and t.n_messages > g["max_messages"]:
        return False
    if "max_user_turns" in g and t.n_user_turns > g["max_user_turns"]:
        return False
    return True


def sample(tasks: list[Task], cfg: dict) -> list[Task]:
    langs = [l.lower() for l in cfg.get("languages", [])]
    groups = cfg.get("groups", [])
    pool = [t for t in tasks
            if (not langs or any(t.language.lower().startswith(l) for l in langs))
            and (not groups or any(_in_group(t, g) for g in groups))]
    pool.sort(key=lambda t: t.id)
    n = cfg.get("max_tasks", 0)
    if not n or n >= len(pool):
        return pool

    # Proportional allocation by acuity, largest remainder first.
    by_acuity = defaultdict(list)
    for t in pool:
        by_acuity[t.acuity].append(t)
    quota = {k: n * len(v) / len(pool) for k, v in by_acuity.items()}
    alloc = {k: int(q) for k, q in quota.items()}
    for k in sorted(quota, key=lambda k: quota[k] - alloc[k], reverse=True)[: n - sum(alloc.values())]:
        alloc[k] += 1
    rng = random.Random(cfg.get("seed", 0))
    chosen = [t for k in sorted(by_acuity) for t in rng.sample(by_acuity[k], alloc[k])]
    return sorted(chosen, key=lambda t: t.id)


def describe(tasks: list[Task]) -> str:
    """Label counts, so the user can set the sample groups in config.toml."""
    lines = [f"{len(tasks)} tasks, {sum(len(t.criteria) for t in tasks)} criteria"]
    for label, values in [("acuity", [t.acuity for t in tasks]),
                          ("user_profile", [t.user_profile for t in tasks]),
                          ("language", [t.language for t in tasks]),
                          ("behavior_axis", [c.axis for t in tasks for c in t.criteria]),
                          ("messages in prefix", [t.n_messages for t in tasks]),
                          ("user turns", [t.n_user_turns for t in tasks])]:
        lines.append(f"\n{label}:")
        for value, count in sorted(Counter(values).items(), key=lambda kv: -kv[1]):
            lines.append(f"  {value!s:<40} {count}")
    return "\n".join(lines)
