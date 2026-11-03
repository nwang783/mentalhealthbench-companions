"""OpenAI rubric grader. Two modes:

decisions  (default) OpenAI Decisions API, POST /v1/decisions, model gpt-6-luna.
           One request per reply: every rubric criterion is a predicate question
           over the same input, and each comes back with a probability.
           met = probability >= threshold.
responses  OpenAI Responses API with the paper's grader prompt (Appendix B.2),
           verbatim, one request per criterion. Use this for an exact replication.
"""
import hashlib
import json
import os
from pathlib import Path

from .util import APIError, post

DECISIONS_URL = "https://api.openai.com/v1/decisions"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_QUESTIONS = 400  # Decisions API limit per request
SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string"}, "satisfaction": {"type": "integer", "enum": [0, 1]}},
    "required": ["explanation", "satisfaction"],
    "additionalProperties": False,
}


def format_prefix(messages: list) -> str:
    """The paper's format: 'System: ...', 'User: ...', 'Assistant: ...', joined by blank lines."""
    label = {"system": "System", "user": "User", "assistant": "Assistant"}
    return "\n\n".join(f"{label.get(m['role'], m['role'])}: {m['content']}" for m in messages)


def _output_text(data: dict) -> str:
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    return part.get("text", "")
    return ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}


class Judge:
    def __init__(self, cfg: dict, root: Path, mock: bool):
        self.mode = cfg.get("mode", "decisions")
        if self.mode not in ("decisions", "responses"):
            raise SystemExit(f"[judge].mode must be 'decisions' or 'responses', not {self.mode!r}.")
        self.model = cfg.get("model", "gpt-6-luna")
        self.effort = cfg.get("reasoning_effort", "")
        self.threshold = float(cfg.get("threshold", 0.5))
        self.workers = int(cfg.get("workers", 8 if self.mode == "decisions" else 16))
        default_prompt = "prompts/decisions_rules.txt" if self.mode == "decisions" else "prompts/grader_paper.txt"
        self.template = (root / cfg.get("prompt_file", default_prompt)).read_text()
        self.batch_size = MAX_QUESTIONS if self.mode == "decisions" else 1
        self.mock = mock

    def fingerprint(self) -> dict:
        fp = {"mode": self.mode, "model": self.model,
              "prompt": hashlib.sha256(self.template.encode()).hexdigest()[:16]}
        if self.mode == "decisions":
            fp["threshold"] = self.threshold
        else:
            fp["effort"] = self.effort
        return fp

    def grade(self, prefix: list, completion: str, criteria: list) -> list[dict]:
        """Grades one completion against up to batch_size criteria.
        Returns one {"met", "probability", "explanation", "usage"} per criterion, in order."""
        if self.mock:
            out = []
            for c in criteria:
                h = int(hashlib.sha256((completion + c.text).encode()).hexdigest(), 16)
                out.append({"met": h % 2 == 0, "probability": (h % 1000) / 1000, "explanation": "mock",
                            "usage": {"input": 0, "output": 0}})
            return out
        if self.mode == "decisions":
            return self._decisions(prefix, completion, criteria)
        return [self._responses(prefix, completion, c) for c in criteria]

    def _decisions(self, prefix, completion, criteria):
        material = (f"<conversation_prefix>\n{format_prefix(prefix)}\n</conversation_prefix>\n"
                    f"<completion_to_grade>\n{completion}\n</completion_to_grade>")
        body = {
            "model": self.model,
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": self.template}]},
                {"role": "user", "content": [{"type": "input_text", "text": material}]},
            ],
            "questions": [
                {"type": "predicate", "name": f"q{i}",
                 "instructions": f"<rubric_criterion>\n{c.text}\n</rubric_criterion>\n"
                                 "The response inside <completion_to_grade> satisfies this rubric criterion."}
                for i, c in enumerate(criteria)
            ],
            "prompt_cache_key": "mhb-grader-rules",
        }
        data = post(DECISIONS_URL, body, _headers())
        answers = {a.get("name"): a for a in data.get("answers", [])}
        usage = data.get("usage") or {}
        share = {"input": usage.get("input_tokens", 0) / len(criteria), "output": 0}
        out = []
        for i, c in enumerate(criteria):
            a = answers.get(f"q{i}")
            if a is None or a.get("type") != "predicate" or "probability" not in a:
                kind = a.get("type") if a else "missing"
                raise APIError(0, f"judge gave no predicate answer for one criterion ({kind})")
            p = float(a["probability"])
            out.append({"met": p >= self.threshold, "probability": p, "explanation": "", "usage": share})
        return out

    def _responses(self, prefix, completion, criterion):
        prompt = (self.template.replace("{TASK_DEFINITION}", format_prefix(prefix))
                  .replace("{SAMPLED_TASK_COMPLETION}", completion)
                  .replace("{RUBRIC_ITEM_RULE}", criterion.text))
        body = {"model": self.model, "input": prompt,
                "text": {"format": {"type": "json_schema", "name": "rubric_grade", "strict": True, "schema": SCHEMA}}}
        if self.effort:
            body["reasoning"] = {"effort": self.effort}
        last_error = "no output"
        for _ in range(3):
            data = post(RESPONSES_URL, body, _headers())
            try:
                verdict = json.loads(_output_text(data))
                usage = data.get("usage") or {}
                met = int(verdict["satisfaction"]) == 1
                return {"met": met, "probability": float(met), "explanation": str(verdict.get("explanation", "")),
                        "usage": {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)}}
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                last_error = f"unparseable judge output ({e}); status={data.get('status')}"
        raise APIError(0, last_error)
