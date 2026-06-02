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

import ast
import base64
import json
import logging
import re
from typing import Any

import httpx

from observatory import emit, get_observatory
from tools import AGENT_TOOL_SPECS, RobotTools

log = logging.getLogger("agent")

MAX_TOOL_ITERS = 5

# Some local models leak chain-of-thought into the assistant *content* even with
# "thinking" turned off (observed: Gemma emitting `<|thought| Thinking Process: …`
# and taking ~45 s). Never let reasoning reach the voice or the memory stream.
_THINK_BLOCK = re.compile(
    r"<\|?\s*(?:think|thought|reasoning)\s*\|?>.*?<\|?\s*/\s*(?:think|thought|reasoning)\s*\|?>",
    re.IGNORECASE | re.DOTALL,
)
_THINK_OPEN = re.compile(
    r"<\|?\s*(?:think|thought|reasoning)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
# Gemma's "channel" thinking segments, e.g. `<|channel>thought\n…<channel|>`.
_CHANNEL = re.compile(r"<\|channel>.*?(?:<channel\|>|$)", re.IGNORECASE | re.DOTALL)


def _strip_reasoning(text: str) -> str:
    """Strip leaked chain-of-thought (closed ``<think>…</think>`` blocks, Gemma
    ``<|channel>…`` segments, and any stray unclosed ``<|thought|…`` marker through
    end-of-text) so the pet never speaks or stores its private reasoning. Benign
    ``<`` in normal text is left alone (the marker must be immediately followed by
    think/thought/reasoning/channel)."""
    if not text or "<" not in text:
        return text
    text = _THINK_BLOCK.sub("", text)
    text = _CHANNEL.sub("", text)
    text = _THINK_OPEN.sub("", text)
    return text.strip()


# --- tolerate tool calls emitted as TEXT (weak-tool models) -------------------
# Some small models (e.g. Gemma E4B) write `set_emotion("happy")` as plain content
# instead of using the function-calling interface. Parse those back into real tool
# calls so the robot ACTS instead of speaking the syntax aloud. Only lines that are
# exactly a known tool name + (...) are touched — prose is never misparsed.
def _tool_param_order() -> dict[str, list[str]]:
    order = {}
    for spec in AGENT_TOOL_SPECS:
        fn = spec["function"]
        order[fn["name"]] = list(fn.get("parameters", {}).get("properties", {}).keys())
    return order


_TOOL_PARAM_ORDER = _tool_param_order()
_TOOL_NAMES = tuple(_TOOL_PARAM_ORDER)


_TOOL_CALL_RE = re.compile(
    r"\b(" + "|".join(map(re.escape, _TOOL_NAMES)) + r")\s*\((.*?)\)", re.DOTALL)


def _parse_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """Return [{name, arguments}] for tool calls a model wrote as text, or []. Finds
    `name(...)` anywhere (handles markdown bullets, backticks, several per line); maps
    positional args to each tool's parameter order; arg values via ast.literal_eval."""
    calls: list[dict[str, Any]] = []
    for m in _TOOL_CALL_RE.finditer(content):
        name, argstr = m.group(1), m.group(2)
        try:
            node = ast.parse(f"{name}({argstr})", mode="eval").body
        except SyntaxError:
            continue
        if not isinstance(node, ast.Call):
            continue
        params = _TOOL_PARAM_ORDER.get(name, [])
        args: dict[str, Any] = {}
        try:
            for i, a in enumerate(node.args):
                if i < len(params):
                    args[params[i]] = ast.literal_eval(a)
            for kw in node.keywords:
                if kw.arg:
                    args[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError):
            continue  # non-literal arg (likely real prose) — skip
        calls.append({"name": name, "arguments": args})
    return calls


def _is_pure_tool_calls(content: str) -> bool:
    """True if content is *only* tool calls (plus markdown punctuation) — no real prose."""
    if not _TOOL_CALL_RE.search(content):
        return False
    leftover = re.sub(r"[\s`*\-,.:0-9]+", "", _TOOL_CALL_RE.sub("", content))
    return leftover == ""


def _strip_tool_calls(content: str) -> str:
    """Remove tool-call substrings (and now-empty list punctuation), keeping real prose
    — for cleaning stored thoughts / reflection insights."""
    out = _TOOL_CALL_RE.sub("", content)
    lines = []
    for ln in out.splitlines():
        ln = re.sub(r"^[\s`*\-,]+", "", re.sub(r"[\s`,]+$", "", ln))
        if ln.strip():
            lines.append(ln)
    return "\n".join(lines).strip()

# Tools the agent may use during a PRIVATE internal-monologue tick (cognition.py).
# Deliberately excludes `speak` and `queue_question`: a private thought emotes and
# glances but doesn't talk (speech is added back only on speak-eligible ticks) and
# never defers a question to the human (that belongs to user-driven turns). Plan §12.
SILENT_TOOL_NAMES = ("set_emotion", "look", "play_animation", "drive", "stop", "see")


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
        # Restricted tool sets for the internal-monologue path (think()): silent
        # (no speech) and speak-eligible (silent + speak). Built once from the full
        # schema so the cognition loop can deterministically gate spontaneous speech.
        self._silent_specs = [s for s in AGENT_TOOL_SPECS
                              if s["function"]["name"] in SILENT_TOOL_NAMES]
        self._speak_specs = self._silent_specs + [s for s in AGENT_TOOL_SPECS
                                                  if s["function"]["name"] == "speak"]
        # Optional hook set by the orchestrator when cognition is enabled: given the
        # current utterance, returns a short retrieved-memory + mood block to append
        # to the dynamic context. None → today's behavior (tests / cognition off).
        self.memory_render = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- prompt construction --------------------------------------------------
    def _dynamic_context(self, extra: str | None = None) -> str:
        lines = ["## Current context",
                 "Recent answers the human has given you (use these; don't re-ask):",
                 self.state.render_recent_answers()]
        vision = self.state.fresh_vision()
        if vision:
            lines += ["", f"What you last saw (recent): {vision}"]
        lines += ["", f"Body telemetry: {self.state.render_telemetry_line()}"]
        if extra:
            lines += ["", extra]
        return "\n".join(lines)

    def _memory_block(self, query: str) -> str | None:
        """Top-K retrieved memories + mood, via the orchestrator-set hook (or None).
        Kept short — this block is re-encoded every turn (token budget). Never raises."""
        if self.memory_render is None:
            return None
        try:
            return self.memory_render(query) or None
        except Exception:  # noqa: BLE001 - memory must never break a turn
            log.debug("memory_render failed", exc_info=True)
            return None

    def _build_messages(self, utterance: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.persona_text},
            {"role": "system", "content": self._dynamic_context(self._memory_block(utterance))},
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

    # --- internal monologue (cognition.py) ------------------------------------
    async def think(self, perception: str, memories, mood, allow_speak: bool) -> tuple[str, bool]:
        """Run one PRIVATE cognitive turn. Returns ``(thought_text, spoke)``. Plan §12.

        Unlike a spoken turn, this builds a *minimal* message list (persona + a
        one-off framing message; no conversation history, for low per-tick cost),
        uses a restricted tool set (silent unless ``allow_speak``), and **never
        voices the returned content** — the content IS the private thought. Speech
        happens only if the model explicitly calls ``speak`` (which the silent set
        withholds), so chattiness is gated deterministically in code."""
        messages = self._build_think_messages(perception, memories, mood, allow_speak)
        # Silent ticks offer NO tools so the model returns a plain-words thought instead of
        # emitting tool-call syntax (small models do that); the eyes are driven from mood by
        # the cognition loop regardless. Speak-eligible ticks get the full silent+speak set.
        specs = self._speak_specs if allow_speak else []
        return await self._think_once(messages, specs, allow_speak)

    def _build_think_messages(self, perception, memories, mood, allow_speak: bool):
        mem_lines = "\n".join(f"- {m.content}" for m in memories) if memories \
            else "(nothing in particular)"
        action = (
            "Write your thought in plain words. If — and only if — something is genuinely worth "
            "saying out loud right now, you may also call the speak tool with one short line."
            if allow_speak else
            "Respond with ONLY your thought, in plain words — do not write any actions, function "
            "calls, or quotes."
        )
        framing = (
            "You are alone with your own thoughts for a moment — no one is asking you anything.\n"
            "This is your private inner monologue. Whatever you write is a THOUGHT, not something "
            "you say out loud.\n\n"
            f"Right now:\n{perception}\n\n"
            f"{mood.render()}\n\n"
            f"On your mind (memories, most relevant first):\n{mem_lines}\n\n"
            "Have one short private thought (one or two sentences) about what's going on or what "
            f"you notice. {action}"
        )
        return [
            {"role": "system", "content": self.persona_text},
            {"role": "user", "content": framing},
        ]

    async def _think_once(self, messages: list[dict[str, Any]], specs: list,
                          allow_speak: bool = False) -> tuple[str, bool]:
        """Silent completion: dispatch tool calls (emote/look/…/maybe speak) but NEVER
        voice the trailing content. Returns the latest non-empty content as the thought."""
        thought = ""
        spoke = False
        for _ in range(MAX_TOOL_ITERS):
            msg = await self._chat(messages, tools=specs)
            content = (msg.get("content") or "").strip()
            if content:
                thought = content  # keep the latest private thought; never sent to TTS
            tool_calls = msg.get("tool_calls") or []
            messages.append(msg)
            if not tool_calls:
                break
            for call in tool_calls:
                if call.get("function", {}).get("name") == "speak":
                    spoke = True  # voiced inside _run_tool via tools.speak()
                result = await self._run_tool(call)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )
            self._attach_pending_images(messages)
        # The thought may itself be tool calls written as text — dispatch the actionable
        # ones (emote/look) so the robot still reacts, and keep only prose as the thought.
        if thought:
            calls = _parse_text_tool_calls(thought)
            if calls:
                await self._dispatch_text_calls(calls, allow_speak=allow_speak)
                if allow_speak and any(c["name"] == "speak" for c in calls):
                    spoke = True
                thought = _strip_tool_calls(thought)
        return _strip_reasoning(thought), spoke

    async def _dispatch_text_calls(self, calls: list[dict[str, Any]],
                                   allow_speak: bool = True) -> bool:
        """Execute tool calls a model emitted as text. speak() voices its text (only when
        allowed); everything else goes through _run_tool. Returns True if anything ran."""
        ran = False
        for c in calls:
            name, args = c["name"], c["arguments"]
            if name == "speak":
                text = _strip_reasoning(str(args.get("text", "")).strip())
                if allow_speak and text:
                    await self.tools.speak(text)
                    ran = True
            else:
                await self._run_tool({"id": "", "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}})
                ran = True
        return ran

    async def complete_text(self, prompt: str, system: str | None = None) -> str:
        """A plain, tool-free completion returning assistant text (for reflection /
        meta-reasoning). Never voiced, never added to conversation. Plan §12."""
        messages = [
            {"role": "system", "content": system if system is not None else self.persona_text},
            {"role": "user", "content": prompt},
        ]
        msg = await self._chat(messages, tools=[])  # empty → no tools offered
        return _strip_tool_calls(_strip_reasoning((msg.get("content") or "").strip()))

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

        # The model may end with plain content. If that content is actually tool calls
        # written as text (weak-tool models), execute them; otherwise voice it (minus
        # any leaked reasoning), staying silent if nothing's left.
        if final_text:
            calls = _parse_text_tool_calls(final_text)
            if calls and _is_pure_tool_calls(final_text):
                await self._dispatch_text_calls(calls)
            else:
                spoken = _strip_reasoning(final_text)
                if spoken:
                    await self.tools.speak(spoken)
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

        # Fallback: model answered in plain content and never called speak(). If that
        # content is tool calls written as text, execute them instead of voicing it.
        if final_text and not spoke_any:
            calls = _parse_text_tool_calls(final_text)
            if calls and _is_pure_tool_calls(final_text):
                await self._dispatch_text_calls(calls)
            else:
                spoken = _strip_reasoning(final_text)
                if spoken:
                    await self.tools.speak(spoken)
        return final_text

    @staticmethod
    def _speak_text(raw_args: Any) -> str:
        try:
            return (json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})).get("text", "").strip()
        except (json.JSONDecodeError, AttributeError):
            return ""

    async def _chat_stream(self, messages: list[dict[str, Any]], tools=None) -> dict[str, Any]:
        """Stream a completion; feed any speak() text to TTS as it arrives.
        Returns the fully assembled assistant message (content + tool_calls).
        ``tools`` defaults to the full schema; callers may pass a restricted set."""
        specs = tools if tools is not None else AGENT_TOOL_SPECS
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
        }
        if specs:  # empty list → omit (tool_choice="auto" with no tools confuses some servers)
            payload["tools"] = specs
            payload["tool_choice"] = "auto"
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # Observatory tap (Plan §11): the brain's RECEIVING view. Redacted — only
        # message/tool counts + the last user text (no image bytes). No-op when off.
        if get_observatory().enabled:
            user = _last_user_text(messages)
            emit("lmstudio", "recv", "chat-request",
                 f"{len(messages)} msgs, {len(specs)} tools: {user[:60]}",
                 {"messages": len(messages), "tools": len(specs), "user": user})
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

    async def _chat(self, messages: list[dict[str, Any]], tools=None) -> dict[str, Any]:
        specs = tools if tools is not None else AGENT_TOOL_SPECS
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if specs:  # empty list → omit (tool_choice="auto" with no tools confuses some servers)
            payload["tools"] = specs
            payload["tool_choice"] = "auto"
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        # Observatory tap (Plan §11): brain's RECEIVING view (redacted). No-op when off.
        if get_observatory().enabled:
            user = _last_user_text(messages)
            emit("lmstudio", "recv", "chat-request",
                 f"{len(messages)} msgs, {len(specs)} tools: {user[:60]}",
                 {"messages": len(messages), "tools": len(specs), "user": user})
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
