"""build_args tests: the managed launch is unchanged by default, and the optional
prompt/KV-cache flags appear only when their [agent] keys are set. Plan §12."""

from llama_server import build_args, manages

BASE = {
    "llama_server_exe": "llama-server.exe",
    "model_path": "m.gguf",
    "host": "127.0.0.1",
    "port": 8080,
}


def test_build_args_minimal_is_unchanged():
    assert build_args(dict(BASE)) == [
        "llama-server.exe", "-m", "m.gguf", "--host", "127.0.0.1", "--port", "8080",
        "-ngl", "99", "-c", "8192", "--parallel", "1",
    ]


def test_build_args_appends_cache_flags_when_set():
    args = build_args(dict(
        BASE, cache_reuse=256, cache_type_k="q8_0", cache_type_v="q8_0",
        flash_attn=True, slot_save_path="data/llm_cache",
    ))
    assert args[args.index("--cache-reuse") + 1] == "256"
    assert args[args.index("--cache-type-k") + 1] == "q8_0"
    assert args[args.index("--cache-type-v") + 1] == "q8_0"
    assert "-fa" in args
    assert args[args.index("--slot-save-path") + 1] == "data/llm_cache"


def test_build_args_no_cache_flags_when_falsy():
    args = build_args(dict(BASE, cache_reuse=0, cache_type_k="", flash_attn=False))
    for flag in ("--cache-reuse", "--cache-type-k", "--cache-type-v", "-fa", "--slot-save-path"):
        assert flag not in args


def test_build_args_extra_args_appended():
    args = build_args(dict(BASE, extra_args=["--foo", "bar"]))
    assert args[-2:] == ["--foo", "bar"]


def test_manages_default_true():
    assert manages({}) is True
    assert manages({"manage_server": False}) is False
