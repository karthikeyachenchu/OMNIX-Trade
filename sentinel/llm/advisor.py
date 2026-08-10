"""Ollama advisor — tool-calling loop around the local model.

The model reasons with live data via read-only tools and produces advice.
It never touches the order path.
"""

from __future__ import annotations

import logging
import time

import ollama

from ..config import Settings
from .tools import TOOL_SPECS, ToolExecutor

log = logging.getLogger("sentinel.advisor")


class Advisor:
    def __init__(self, settings: Settings, executor: ToolExecutor):
        self.s = settings
        self.executor = executor
        self._resolved = False   # True once Ollama answered and we locked a model
        self.model = self._pick_model()
        self._chat_history: list[dict] = []

    def _pick_model(self, retries: int = 3) -> str:
        """Resolve the model to use. Retries a few times because a shared Ollama
        (e.g. also serving OMNIX) can be momentarily busy at startup. If Ollama
        stays unreachable we fall back but leave `_resolved` False so the next
        call re-attempts and self-heals to the preferred model once it's free."""
        last_err = None
        for attempt in range(retries):
            try:
                names = [m.model for m in ollama.list().models]
            except Exception as e:
                last_err = e
                time.sleep(1.0)
                continue
            for candidate in (self.s.llm_model, f"{self.s.llm_model}:latest"):
                if candidate in names:
                    self._resolved = True
                    return candidate
            # Ollama IS reachable, the custom model just isn't built — stop retrying
            log.warning("Model '%s' not found; using %s (run: ollama create trade-sentinel -f Modelfile)",
                        self.s.llm_model, self.s.llm_fallback)
            self._resolved = True
            return self.s.llm_fallback
        log.warning("Ollama not reachable after %d tries (%s) — will retry on demand", retries, last_err)
        self._resolved = False
        return self.s.llm_fallback

    def _ensure_model(self) -> None:
        """If startup couldn't reach Ollama, try again now (self-healing)."""
        if not self._resolved:
            self.model = self._pick_model(retries=1)

    def _run(self, messages: list[dict]) -> str:
        """Tool-call loop, capped at max_tool_rounds."""
        self._ensure_model()
        for _ in range(self.s.max_tool_rounds):
            resp = ollama.chat(model=self.model, messages=messages, tools=TOOL_SPECS,
                               options={"temperature": 0.2})
            msg = resp.message
            if not msg.tool_calls:
                return (msg.content or "").strip()
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                result = self.executor.execute(tc.function.name, dict(tc.function.arguments or {}))
                log.info("tool %s(%s)", tc.function.name, dict(tc.function.arguments or {}))
                messages.append({"role": "tool", "content": result, "tool_name": tc.function.name})
        # out of rounds — force a final answer without tools
        resp = ollama.chat(model=self.model, messages=messages, options={"temperature": 0.2})
        return (resp.message.content or "").strip()

    def advise_on_signal(self, composite) -> str:
        """Called by the engine when a strong composite signal fires."""
        prompt = (
            f"A composite {composite.label()} signal (strength {composite.score:.2f}) just fired on "
            f"{composite.name}. Verify it with your tools (technicals, sentiment, risk status) and give "
            f"your verdict in the standard format. If the signal doesn't hold up, say NO TRADE and why."
        )
        return self._run([{"role": "user", "content": prompt}])

    def chat(self, user_message: str) -> str:
        """Free-form Q&A from the dashboard chat box."""
        self._chat_history.append({"role": "user", "content": user_message})
        self._chat_history = self._chat_history[-12:]  # keep context small & fast
        answer = self._run(list(self._chat_history))
        self._chat_history.append({"role": "assistant", "content": answer})
        return answer

    def morning_briefing(self) -> str:
        return self._run([{
            "role": "user",
            "content": ("Give a pre-market / current-market briefing: check sentiment, news, and the "
                        "technicals of NIFTY 50 and BANKNIFTY. Summarize the day's bias, key levels, "
                        "and what kind of option setups to watch for. Keep it under 200 words."),
        }])
