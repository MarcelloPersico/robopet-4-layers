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

import base64
import json
import logging
from typing import Any

import httpx

from observatory import emit, get_observatory
from tools import AGENT_TOOL_SPECS, RobotTools

log = logging.getLogger("agent")

MAX_TOOL_ITERS = 5


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """The most recent user message as plain text, with any image_url blocks
    stripped (REDACTED — never put base64 frame bytes on the dashboard). Used
    only to build the Observatory chat-request detail (Plan §11)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content[:200]
        if isinstance(content, list):  # multimodal: keep text parts, drop images
            text = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
            return (text + " [+image]")[:200]
        return ""
    return ""


class _SpeakArgStreamer:
    """Pulls the spoken ``text`` out of a *streaming* speak() tool-call argument
    JSON, so TTS can start before the full tool call arrives.

    The model streams arguments like ``{"text": "Hello. How are you?"}`` in
    fragments. We re-decode the value on each fragment and return only the newly
    revealed suffix, stopping cleanly on an incomplete trailing escape.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._valpos: int | None = None  # index where the text value starts
        self._emitted = 0
        self._done = False

    def push(self, chunk: str) -> str:
        self._buf += chunk
        if self._done:
            return ""
        if self._valpos is None:
            key = self._buf.find('"text"')
            if key < 0:
                return ""
            colon = self._buf.find(":", key + 6)
            if colon < 0:
                return ""
            q = self._buf.find('"', colon + 1)
            if q < 0:
                return ""
            self._valpos = q + 1
        decoded, end = self._decode(self._buf, self._valpos)
        if end is not None:
            self._done = True
        new = decoded[self._emitted:]
        self._emitted = len(decoded)
        return new

    @staticmethod
    def _decode(s: str, start: int) -> tuple[str, int | None]:
        """Decode a JSON string body from ``start``. Returns (text_so_far,
        end_index) where end_index is the closing quote position or None if the
        value is still open. Stops before an incomplete trailing escape."""
        out: list[str] = []
        i, n = start, len(s)
        while i < n:
            c = s[i]
            if c == "\\":
                if i + 1 >= n:
                    break  # dangling backslash: wait for more
                e = s[i + 1]
                if e == "u":
                    if i + 6 > n:
                        break  # incomplete \uXXXX
                    try:
                        out.append(chr(int(s[i + 2:i + 6], 16)))
                    except ValueError:
                        out.append(" ")
                    i += 6
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                            "\\": "\\", "/": "/", "b": " ", "f": " "}.get(e, e))
                i += 2
                continue
            if c == '"':
                return "".join(out), i
            out.append(c)
            i += 1
        return "".join(out), None


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
        stream: bool = True,
        reasoning_effort: str = "none",
    ):
        self.base_url = base_url.rstrip("/")
        self.tools = tools
        self.state = state
        self.persona_text = persona_text
        self.model = model
        self.temperature = temperature
        self.ctx_turns = ctx_turns
        # OpenAI-compatible reasoning hint sent on every completion. "none" tells a
        # reasoning-capable server (LM Studio, recent llama.cpp) NOT to emit a
        # chain-of-thought before answering. The pet wants terse tool calls, not a
        # thinking trace — and the streaming loop discards reasoning_content anyway,
        # so leaving thinking on is pure latency (≈3× slower turns; see Plan §5.6).
        # Set to "low"/"medium"/"high" only for a model that benefits from it; ""
        # omits the field entirely for servers that reject it.
        self.reasoning_effort = reasoning_effort
        # Stream the LLM reply and feed speak() text to TTS sentence-by-sentence
        # so audio starts before a long answer finishes (Plan §5.6). Requires a
        # streamable TTS; falls back to the buffered path otherwise.
        self.stream = stream
        self._client = httpx.AsyncClient(timeout=60.0)
        # Map tool name -> bound handler. Built with getattr so a partial tools
        # object (e.g. a test double) that omits a newer tool simply doesn't
        # register it, rather than crashing construction; the live RobotTools
        # implements them all. Unregistered names are reported by _run_tool.
        self._dispatch = {
            name: getattr(tools, name)
            for name in ("drive", "play_animation", "stop", "see", "speak",
                         "set_idle_intensity", "set_emotion", "look", "queue_question")
            if hasattr(tools, name)
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
        if self.stream and getattr(self.tools, "tts_streamable", False):
            return await self._handle_streaming(messages)
        return await self._handle_buffered(messages)

    async def deliver_answer(self, topic: str, resolution: str) -> str:
        """A human just answered a question the pet had deferred (over MCP). Drive
        one turn so the pet reacts *now* — say it in its own words, move if it
        fits — reusing the same tool loop and streaming TTS as a spoken turn.

        The answer is already in the recent-answers buffer (tools.resolve_*), so
        this only injects a one-off framing message; it doesn't touch conversation
        history. Plan §5.5."""
        framed = (
            "Your human just got back to you about something you'd set aside and "
            "weren't sure about.\n"
            f"What it was about: {topic}\n"
            f"Their answer: {resolution}\n"
            "React now, in character and out loud: tell them what you learned in "
            "one short line, and add a small movement only if it fits. Keep it brief."
        )
        messages = self._build_messages(framed)
        if self.stream and getattr(self.tools, "tts_streamable", False):
            return await self._handle_streaming(messages)
        return await self._handle_buffered(messages)

    async def _handle_buffered(self, messages: list[dict[str, Any]]) -> str:
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

            # Unified-vision: if see() captured frames, attach them as an image
            # message so the multimodal model sees pixels on the next iteration.
            # No-op in split mode (the VLM already returned a text caption).
            self._attach_pending_images(messages)

        # If the model spoke via plain content instead of the speak() tool, voice it.
        if final_text:
            await self.tools.speak(final_text)
        return final_text

    # --- streaming turn (speak() text reaches TTS as it generates) ------------
    async def _handle_streaming(self, messages: list[dict[str, Any]]) -> str:
        final_text = ""
        spoke_any = False
        for _ in range(MAX_TOOL_ITERS):
            msg = await self._chat_stream(messages)  # feeds speak() text to TTS live
            self.tools.speak_flush()                 # play the trailing sentence
            tool_calls = msg.get("tool_calls") or []
            messages.append(
                {"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls or None}
            )

            if not tool_calls:
                final_text = (msg.get("content") or "").strip()
                break

            for call in tool_calls:
                fn = call.get("function", {})
                if fn.get("name") == "speak":
                    # Already voiced incrementally during streaming; just record
                    # the turn + ack the tool call (don't re-synthesize it).
                    text = self._speak_text(fn.get("arguments"))
                    if text:
                        self.state.add_assistant_turn(text)
                        spoke_any = True
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id", ""), "content": "spoke"}
                    )
                else:
                    result = await self._run_tool(call)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                    )
            self._attach_pending_images(messages)

        # Fallback: model answered in plain content and never called speak().
        if final_text and not spoke_any:
            await self.tools.speak(final_text)
        return final_text

    @staticmethod
    def _speak_text(raw_args: Any) -> str:
        try:
            return (json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})).get("text", "").strip()
        except (json.JSONDecodeError, AttributeError):
            return ""

    async def _chat_stream(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Stream a completion; feed any speak() text to TTS as it arrives.
        Returns the fully assembled assistant message (content + tool_calls)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": AGENT_TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "stream": True,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # Observatory tap (Plan §11): the brain's RECEIVING view. Redacted — only
        # message/tool counts + the last user text (no image bytes). No-op when off.
        if get_observatory().enabled:
            user = _last_user_text(messages)
            emit("lmstudio", "recv", "chat-request",
                 f"{len(messages)} msgs, {len(AGENT_TOOL_SPECS)} tools: {user[:60]}",
                 {"messages": len(messages), "tools": len(AGENT_TOOL_SPECS), "user": user})
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        speakers: dict[int, _SpeakArgStreamer] = {}

        async with self._client.stream(
            "POST", f"{self.base_url}/v1/chat/completions", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    choice = json.loads(data)["choices"][0]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                        if slot["name"] == "speak":
                            inc = speakers.setdefault(idx, _SpeakArgStreamer()).push(fn["arguments"])
                            if inc:
                                self.tools.speak_feed(inc)  # → TTS, plays per sentence
                                # Observatory tap (Plan §11): the streamed speak()
                                # feed bypasses _run_tool, so catch it here.
                                emit("lmstudio", "exec", "speak-feed", inc[:80], {"text": inc})

        tool_calls = [
            {"id": s["id"], "type": "function",
             "function": {"name": s["name"], "arguments": s["args"]}}
            for s in calls.values() if s["name"]
        ]
        content = "".join(content_parts)
        # Observatory tap (Plan §11): the brain's SENDING view (content size +
        # tool-call names). No-op when the dashboard is disabled.
        names = [tc["function"]["name"] for tc in tool_calls]
        emit("lmstudio", "send", "chat-response",
             f"{len(content)} chars, tools: {names or '-'}",
             {"chars": len(content), "tool_calls": names})
        return {"role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls or None}

    def _attach_pending_images(self, messages: list[dict[str, Any]]) -> None:
        take = getattr(self.tools, "take_pending_images", None)
        if take is None:
            return
        for frame in take():
            b64 = base64.b64encode(frame).decode("ascii")
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here is your current camera view:"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            })

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
        # Observatory tap (Plan §11): the agent-originated tool path (kept distinct
        # from the MCP-originated one in mcp_server.py). All no-op when off.
        emit("lmstudio", "exec", "tool:" + fn_name, f"{fn_name}(...)", args)
        try:
            result = await handler(**args)
        except TypeError as e:
            emit("lmstudio", "exec", "tool-error", f"{fn_name}: {e}", {"error": str(e)})
            return f"error: bad arguments for {fn_name}: {e}"
        except Exception as e:  # noqa: BLE001 - surface tool errors to the model, don't crash
            log.warning("tool %s failed: %s", fn_name, e)
            emit("lmstudio", "exec", "tool-error", f"{fn_name}: {e}", {"error": str(e)})
            return f"error: {fn_name} failed: {e}"
        out = result if isinstance(result, str) else json.dumps(result, default=str)
        emit("lmstudio", "exec", "tool-result", out[:80], {"result": out})
        return out

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": AGENT_TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "stream": False,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # Observatory tap (Plan §11): brain's RECEIVING view (redacted). No-op when off.
        if get_observatory().enabled:
            user = _last_user_text(messages)
            emit("lmstudio", "recv", "chat-request",
                 f"{len(messages)} msgs, {len(AGENT_TOOL_SPECS)} tools: {user[:60]}",
                 {"messages": len(messages), "tools": len(AGENT_TOOL_SPECS), "user": user})
        resp = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        # Observatory tap (Plan §11): brain's SENDING view.
        if get_observatory().enabled:
            names = [tc.get("function", {}).get("name", "")
                     for tc in (msg.get("tool_calls") or [])]
            content = msg.get("content") or ""
            emit("lmstudio", "send", "chat-response",
                 f"{len(content)} chars, tools: {names or '-'}",
                 {"chars": len(content), "tool_calls": names})
        return msg
