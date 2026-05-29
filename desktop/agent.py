"""Local agent loop: llama.cpp HTTP client, tool-call execution, system-prompt
builder. Plan §5.

Talks to the llama-server subprocess over its OpenAI-compatible
``/v1/chat/completions`` endpoint with function-calling. The static persona
(persona.md) is sent as the leading system message so llama.cpp's prefix cache
hits across turns (Plan §5.4); dynamic state (recent-answers buffer, fresh
vision, telemetry) is appended as a second system message each turn.

Spoken output flows through the ``speak`` tool, not the assistant text channel,
so the orchestrator's TTS is driven by tool calls (Plan §5, persona rules).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from tools import AGENT_TOOL_SPECS, RobotTools

log = logging.getLogger("agent")

MAX_TOOL_ITERS = 5


class AgentBrain:
    def __init__(
        self,
        base_url: str,
        tools: RobotTools,
        state,
        persona_text: str,
        model: str = "local",
        temperature: float = 0.7,
        ctx_turns: int = 6,
    ):
        self.base_url = base_url.rstrip("/")
        self.tools = tools
        self.state = state
        self.persona_text = persona_text
        self.model = model
        self.temperature = temperature
        self.ctx_turns = ctx_turns
        self._client = httpx.AsyncClient(timeout=60.0)
        self._dispatch = {
            "drive": tools.drive,
            "play_animation": tools.play_animation,
            "stop": tools.stop,
            "see": tools.see,
            "speak": tools.speak,
            "set_idle_intensity": tools.set_idle_intensity,
            "queue_question": tools.queue_question,
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- prompt construction --------------------------------------------------
    def _dynamic_context(self) -> str:
        lines = ["## Current context",
                 "Recent answers the human has given you (use these; don't re-ask):",
                 self.state.render_recent_answers()]
        vision = self.state.fresh_vision()
        if vision:
            lines += ["", f"What you last saw (recent): {vision}"]
        lines += ["", f"Body telemetry: {self.state.render_telemetry_line()}"]
        return "\n".join(lines)

    def _build_messages(self, utterance: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.persona_text},
            {"role": "system", "content": self._dynamic_context()},
        ]
        for role, text in list(self.state.conversation)[-self.ctx_turns:]:
            messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": utterance})
        return messages

    # --- main entry -----------------------------------------------------------
    async def handle_utterance(self, utterance: str) -> str:
        """Run one full turn: tool loop until the agent stops calling tools.
        Returns the final assistant text (may be empty if it only used tools)."""
        self.state.add_user_turn(utterance)
        messages = self._build_messages(utterance)

        final_text = ""
        for _ in range(MAX_TOOL_ITERS):
            msg = await self._chat(messages)
            tool_calls = msg.get("tool_calls") or []
            messages.append(msg)  # echo assistant turn (with tool_calls) back

            if not tool_calls:
                final_text = (msg.get("content") or "").strip()
                break

            for call in tool_calls:
                result = await self._run_tool(call)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )

        # If the model spoke via plain content instead of the speak() tool, voice it.
        if final_text:
            await self.tools.speak(final_text)
        return final_text

    async def _run_tool(self, call: dict[str, Any]) -> str:
        fn_name = call.get("function", {}).get("name", "")
        raw_args = call.get("function", {}).get("arguments", "") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            return f"error: could not parse arguments for {fn_name}"
        handler = self._dispatch.get(fn_name)
        if handler is None:
            return f"error: unknown tool {fn_name}"
        try:
            result = await handler(**args)
        except TypeError as e:
            return f"error: bad arguments for {fn_name}: {e}"
        except Exception as e:  # noqa: BLE001 - surface tool errors to the model, don't crash
            log.warning("tool %s failed: %s", fn_name, e)
            return f"error: {fn_name} failed: {e}"
        return result if isinstance(result, str) else json.dumps(result, default=str)

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": AGENT_TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "stream": False,
        }
        resp = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]
