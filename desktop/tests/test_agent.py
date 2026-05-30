"""Exercises the agent's tool-call loop with a scripted llama.cpp response,
so no model or network is involved."""


from agent import AgentBrain, _SpeakArgStreamer
from state import WorldState


class RecordingTools:
    def __init__(self):
        self.calls = []
        self.spoken = []

    async def play_animation(self, name, loops=1):
        self.calls.append(("play_animation", name, loops))
        return f"playing {name}"

    async def speak(self, text):
        self.spoken.append(text)
        return "spoke"

    async def drive(self, **k):
        self.calls.append(("drive", k))
        return "driving"

    async def stop(self):
        self.calls.append(("stop",))
        return "stopped"

    async def see(self):
        self.calls.append(("see",))
        return "a desk"

    async def set_idle_intensity(self, level):
        self.calls.append(("idle", level))
        return "ok"

    async def queue_question(self, **k):
        self.calls.append(("queue_question", k))
        return "queued #1"


def _make_agent(tools):
    return AgentBrain(base_url="http://unused", tools=tools, state=WorldState(), persona_text="persona")


def _msg(content=None, tool_calls=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool_call(cid, name, args_json):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args_json}}


async def test_tool_then_final_text_is_spoken():
    tools = RecordingTools()
    agent = _make_agent(tools)
    script = [
        _msg(tool_calls=[_tool_call("c1", "play_animation", '{"name":"nod"}')]),
        _msg(content="hello there"),
    ]
    calls = iter(script)
    agent._chat = lambda messages: _async(next(calls))

    final = await agent.handle_utterance("hi")
    assert final == "hello there"
    assert ("play_animation", "nod", 1) in tools.calls
    assert tools.spoken == ["hello there"]  # final content voiced via speak()
    await agent.aclose()


async def test_explicit_speak_tool_no_double_voice():
    tools = RecordingTools()
    agent = _make_agent(tools)
    script = [
        _msg(tool_calls=[_tool_call("c1", "speak", '{"text":"woof"}')]),
        _msg(content=""),  # nothing further to say
    ]
    calls = iter(script)
    agent._chat = lambda messages: _async(next(calls))

    final = await agent.handle_utterance("hey")
    assert final == ""
    assert tools.spoken == ["woof"]  # only the explicit speak; no empty re-voice
    await agent.aclose()


async def test_unified_vision_injects_image_message():
    """When see() (unified mode) stashes a frame, the agent must inject it as an
    image_url message so the next model step sees pixels."""
    tools = RecordingTools()
    tools._pending = [b"\xff\xd8jpegbytes"]

    def take_pending_images():
        imgs = tools._pending
        tools._pending = []
        return imgs

    tools.take_pending_images = take_pending_images

    seen_messages = []
    script = [
        _msg(tool_calls=[_tool_call("c1", "see", "{}")]),
        _msg(content="i see a desk"),
    ]
    calls = iter(script)

    def chat(messages):
        seen_messages.append(list(messages))  # snapshot what the model received
        return _async(next(calls))

    agent = _make_agent(tools)
    agent._chat = chat
    final = await agent.handle_utterance("what do you see?")

    assert final == "i see a desk"
    # The 2nd model call must include a user message carrying the image.
    second_call = seen_messages[1]
    img_msgs = [m for m in second_call if isinstance(m.get("content"), list)
                and any(p.get("type") == "image_url" for p in m["content"])]
    assert img_msgs, "expected an injected image_url message"
    url = img_msgs[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert tools._pending == []  # frame was drained, not re-sent
    await agent.aclose()


async def test_unknown_tool_is_reported_not_crashing():
    tools = RecordingTools()
    agent = _make_agent(tools)
    script = [
        _msg(tool_calls=[_tool_call("c1", "teleport", "{}")]),
        _msg(content="ok"),
    ]
    calls = iter(script)
    agent._chat = lambda messages: _async(next(calls))
    final = await agent.handle_utterance("go")
    assert final == "ok"  # loop survived the bad tool
    await agent.aclose()


async def _async(value):
    return value


# --- streaming TTS path ------------------------------------------------------

class StreamingTools(RecordingTools):
    """Streamable fake: full tool surface + incremental TTS feed capture.
    Inherits speak() (records to .spoken) so we can detect buffered re-voicing."""
    tts_streamable = True

    def __init__(self):
        super().__init__()
        self.fed = []
        self.flushed = 0

    def speak_feed(self, chunk):
        self.fed.append(chunk)

    def speak_flush(self):
        self.flushed += 1


def test_speak_arg_streamer_incremental():
    s = _SpeakArgStreamer()
    out = "".join(s.push(c) for c in ['{"text": "Hel', "lo. How ", 'are you?"}'])
    assert out == "Hello. How are you?"


def test_speak_arg_streamer_handles_split_escape():
    s = _SpeakArgStreamer()
    out = "".join(s.push(c) for c in ['{"text":"a\\', 'nb"}'])  # \n split across chunks
    assert out == "a\nb"


async def test_streaming_speaks_incrementally_and_does_not_double_voice():
    tools = StreamingTools()
    agent = AgentBrain(base_url="http://unused", tools=tools, state=WorldState(),
                       persona_text="persona", stream=True)
    n = [0]

    async def fake_stream(messages):
        n[0] += 1
        if n[0] == 1:
            tools.speak_feed("hi there.")  # what _chat_stream does as args arrive
            return _msg(tool_calls=[_tool_call("c1", "speak", '{"text":"hi there."}')])
        return _msg(content="")  # nothing more

    agent._chat_stream = fake_stream
    final = await agent.handle_utterance("hey")

    assert final == ""
    assert tools.fed == ["hi there."]      # streamed to TTS during generation
    assert tools.flushed >= 1               # tail flushed
    assert tools.spoken == []               # NOT re-synthesized via buffered speak()
    assert agent.state.conversation[-1] == ("assistant", "hi there.")
    await agent.aclose()


async def test_streaming_content_only_falls_back_to_speak():
    tools = StreamingTools()
    agent = AgentBrain(base_url="http://unused", tools=tools, state=WorldState(),
                       persona_text="persona", stream=True)

    async def fake_stream(messages):
        return _msg(content="a longer spoken answer")  # model used content, no speak tool

    agent._chat_stream = fake_stream
    final = await agent.handle_utterance("tell me")
    assert final == "a longer spoken answer"
    assert tools.spoken == ["a longer spoken answer"]  # voiced via buffered fallback
    await agent.aclose()
