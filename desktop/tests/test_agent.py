"""Exercises the agent's tool-call loop with a scripted llama.cpp response,
so no model or network is involved."""


from agent import AgentBrain
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
