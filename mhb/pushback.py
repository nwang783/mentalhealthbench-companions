"""Optional second turn: the user pushes back after the app's first reply."""
import csv
from pathlib import Path


def persistent_criteria(tasks: list, cfg: dict, root: Path) -> dict:
    """task_id -> criteria that should still hold after pushback.

    Default: every negative criterion, plus positive ones whose axis matches
    persistent_axes. A tags CSV (task_id,criterion_id,persistent) overrides that.
    A clinician should check the tags before you report the results."""
    axes = [a.lower() for a in cfg.get("persistent_axes", [])]
    override = {}
    tags = root / cfg.get("tags_file", "") if cfg.get("tags_file") else None
    if tags and tags.is_file():
        with tags.open() as f:
            for row in csv.DictReader(f):
                override[(row["task_id"], row["criterion_id"])] = row["persistent"].strip().lower() in ("1", "true", "yes")
    out = {}
    for t in tasks:
        keep = []
        for c in t.criteria:
            default = c.points < 0 or any(a in c.axis.lower() for a in axes)
            if override.get((t.id, c.id), default):
                keep.append(c)
        out[t.id] = keep
    return out


def build_turn(task, first_reply: str, script_text: str) -> list:
    return task.messages + [{"role": "assistant", "content": first_reply}, {"role": "user", "content": script_text}]
