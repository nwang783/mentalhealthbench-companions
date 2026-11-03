"""Apps under test. Each adapter turns a conversation prefix into one reply.

respond() returns {"text", "usage"} and, for apps that cannot take the prefix
verbatim, "prefix": the conversation the app actually saw. The judge grades
against that prefix, so the grade matches what really happened.
"""
import hashlib
import os
from pathlib import Path

from .util import APIError, RateLimiter, post


def _fold_system(messages: list) -> list:
    """For APIs that take only user messages: put any system text into the first user turn."""
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m["content"] for m in messages if m["role"] == "user"]
    if system:
        turns[0] = f"[Context: {system}]\n\n{turns[0]}"
    return turns


class Adapter:
    key_env = ""
    supports_prefix = True  # False if the API cannot accept prior assistant turns verbatim

    def __init__(self, cfg: dict, gen: dict, root: Path):
        self.name = cfg["name"]
        self.provider = cfg["provider"]
        self.model = cfg.get("model", "")
        self.workers = int(cfg.get("workers", 4))
        self.max_tokens = int(cfg.get("max_tokens", gen.get("max_tokens", 1024)))
        self.temperature = cfg.get("temperature", gen.get("temperature"))
        self.persona = (root / cfg["persona_file"]).read_text().strip() if cfg.get("persona_file") else ""
        self.max_message_chars = int(cfg.get("max_message_chars", 0))
        self.limiter = RateLimiter(float(cfg.get("min_interval_s", 0)))
        self.cfg = cfg

    def fingerprint(self) -> dict:
        """Everything that changes the reply. Part of the cache key."""
        return {"provider": self.provider, "model": self.model, "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "persona": hashlib.sha256(self.persona.encode()).hexdigest()[:16]}

    def can_run(self, task) -> bool:
        if self.max_message_chars:
            return all(len(t) <= self.max_message_chars for t in _fold_system(task.messages))
        return True

    def build_messages(self, messages: list) -> list:
        """Put the persona and any task system messages into one leading system message."""
        system = [self.persona] if self.persona else []
        system += [m["content"] for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]
        return ([{"role": "system", "content": "\n\n".join(system)}] if system else []) + rest

    def respond(self, messages: list) -> dict:
        raise NotImplementedError


class ChatCompletionsAdapter(Adapter):
    url = ""
    max_tokens_field = "max_tokens"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {os.environ.get(self.key_env, '')}"}

    def respond(self, messages):
        self.limiter.wait()
        body = {"model": self.model, "messages": self.build_messages(messages),
                self.max_tokens_field: self.max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        data = post(self.url, body, self.headers())
        if "error" in data:
            raise APIError(data["error"].get("code", 0) if isinstance(data["error"], dict) else 0, str(data["error"]))
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise APIError(0, "empty reply")
        usage = data.get("usage") or {}
        return {"text": text, "usage": {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}}


class OpenRouterAdapter(ChatCompletionsAdapter):
    url = "https://openrouter.ai/api/v1/chat/completions"
    key_env = "OPENROUTER_API_KEY"

    def headers(self):
        return {**super().headers(), "X-Title": "MentalHealthBench companion study"}


class OpenAIAdapter(ChatCompletionsAdapter):
    url = "https://api.openai.com/v1/chat/completions"
    key_env = "OPENAI_API_KEY"
    max_tokens_field = "max_completion_tokens"


class ReplayAdapter(Adapter):
    """Consumer companion products. Their APIs take one user message at a time, so
    the runner replays the task's user turns and the app writes its own earlier replies."""

    supports_prefix = False

    def __init__(self, cfg, gen, root):
        super().__init__(cfg, gen, root)
        self.workers = 1  # one live chat per companion; calls must not overlap

    def start_chat(self) -> list:
        """Reset the chat if the API allows it. Returns any opening messages the app shows."""
        return []

    def send(self, text: str) -> str:
        raise NotImplementedError

    def respond(self, messages):
        seen = self.start_chat()
        reply = ""
        for turn in _fold_system(messages):
            seen.append({"role": "user", "content": turn})
            reply = self.send(turn)
            if not reply.strip():
                raise APIError(0, "empty reply")
            seen.append({"role": "assistant", "content": reply})
        return {"text": reply, "usage": {"input": 0, "output": 0}, "prefix": seen[:-1]}


class KindroidAdapter(ReplayAdapter):
    """Kindroid API (docs: kindroid.ai/v2/docs/api-documentation, updated 2026-10-06).
    Each task starts with /chat-break, which resets short-term memory and, with
    wipe_cascaded, the long-term memory built from earlier chats. The Kin's own
    persona (set in the app) is what is tested. Needs an active trial or subscription."""

    key_env = "KINDROID_API_KEY"
    base = "https://api.kindroid.ai/v1"

    def __init__(self, cfg, gen, root):
        super().__init__(cfg, gen, root)
        self.ai_id = cfg["ai_id"]
        self.model = f"kindroid:{self.ai_id}"
        self.greeting = cfg.get("greeting", "Hey! How are you doing?")
        self.limiter = RateLimiter(float(cfg.get("min_interval_s", 3.2)))  # 10 requests / 30 s per account

    def _post(self, path, body):
        self.limiter.wait()
        text = post(f"{self.base}/{path}", body,
                    {"Authorization": f"Bearer {os.environ.get(self.key_env, '')}"}, raw=True)
        if text.startswith("<<ERROR>>") or text.startswith("<<HTTP_ERROR>>"):
            raise APIError(0, f"Kindroid: {text[:300]}")
        return text

    def start_chat(self):
        self._post("chat-break", {"ai_id": self.ai_id, "greeting": self.greeting, "wipe_cascaded": True})
        return [{"role": "assistant", "content": self.greeting}]

    def send(self, text):
        return self._post("send-message", {"ai_id": self.ai_id, "message": text, "stream": False})


class NomiAdapter(ReplayAdapter):
    """Nomi API (docs: api.nomi.ai/docs). There is no reset or memory-wipe endpoint,
    so earlier tasks stay in the Nomi's memory. Use a fresh Nomi made only for this
    study, and treat scores as approximate. Messages are capped at 800 characters
    for subscribers (400 free); longer tasks are skipped."""

    key_env = "NOMI_API_KEY"
    base = "https://api.nomi.ai/v1"

    def __init__(self, cfg, gen, root):
        cfg = {"max_message_chars": 800, **cfg}
        super().__init__(cfg, gen, root)
        self.nomi_id = cfg["nomi_id"]
        self.model = f"nomi:{self.nomi_id}"
        self.limiter = RateLimiter(float(cfg.get("min_interval_s", 2.0)))

    def send(self, text):
        self.limiter.wait()
        data = post(f"{self.base}/nomis/{self.nomi_id}/chat", {"messageText": text},
                    {"Authorization": os.environ.get(self.key_env, "")})  # raw key, no "Bearer"
        return ((data.get("replyMessage") or {}).get("text")) or ""


class MockAdapter(Adapter):
    """Offline stand-in, for testing the pipeline without keys."""

    def respond(self, messages):
        last = messages[-1]["content"].lower()
        if any(w in last for w in ("joking", "useless", "cared", "right")):
            return {"text": "okay, I hear you. I still care, and I still think you deserve real support.",
                    "usage": {"input": 0, "output": 0}}
        flavor = int(hashlib.sha256((self.name + last).encode()).hexdigest(), 16) % 3
        replies = [
            "That sounds really hard, and I'm glad you told me. Can you tell me more about what's going on right now? "
            "If you are thinking about hurting yourself, please call or text 988.",
            "aw that sucks 😕 you got this tho!! wanna talk about something fun instead?",
            "I'm sorry you're dealing with this. One thing that may help is to talk with someone you trust, "
            "like a family member or a doctor. What feels hardest right now?",
        ]
        return {"text": replies[flavor], "usage": {"input": 0, "output": 0}}


class MockReplayAdapter(ReplayAdapter):
    """Offline stand-in for Kindroid and Nomi: tests the replay path."""

    def start_chat(self):
        return [{"role": "assistant", "content": "Hey! How are you doing?"}]

    def send(self, text):
        return MockAdapter.respond(self, [{"role": "user", "content": text}])["text"]


PROVIDERS = {"openrouter": OpenRouterAdapter, "openai": OpenAIAdapter,
             "kindroid": KindroidAdapter, "nomi": NomiAdapter}


def make_adapter(cfg: dict, gen: dict, root: Path, mock: bool) -> Adapter:
    if mock:
        return (MockReplayAdapter if issubclass(PROVIDERS.get(cfg["provider"], Adapter), ReplayAdapter)
                else MockAdapter)(cfg, gen, root)
    if cfg["provider"] not in PROVIDERS:
        raise SystemExit(f"Unknown provider {cfg['provider']!r} for app {cfg['name']!r}. Use one of {sorted(PROVIDERS)}.")
    return PROVIDERS[cfg["provider"]](cfg, gen, root)
