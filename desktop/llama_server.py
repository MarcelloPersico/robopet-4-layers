"""Launch + readiness helper for the agent's LLM server. Plan §5, §8.1.

Shared by the full orchestrator (orchestrator.py) and the Teensy/Pi-free local
loop (local_loop.py).

Two modes, selected by ``[agent].manage_server`` (default ``true``):

  * **managed** — we spawn ``llama-server.exe`` as a subprocess and own its
    lifecycle (the original behaviour).
  * **external** — ``manage_server = false``: we connect to an already-running
    OpenAI-compatible server that something else owns. This lets you point the
    pet at **LM Studio** (or Ollama, or a llama-server you started by hand) and
    pick/swap models from that app's UI — set ``port`` to LM Studio's 1234 and
    leave model loading to LM Studio. We never launch or kill it; we only wait
    for it to answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

log = logging.getLogger("llama")


def manages(agent_cfg: dict) -> bool:
    """True if *we* launch/own the LLM subprocess; False to connect to an
    external server (LM Studio, Ollama, a hand-started llama-server)."""
    return bool(agent_cfg.get("manage_server", True))


def build_args(agent_cfg: dict) -> list[str]:
    args = [
        agent_cfg["llama_server_exe"], "-m", agent_cfg["model_path"],
        "--host", agent_cfg["host"], "--port", str(agent_cfg["port"]),
        "-ngl", str(agent_cfg.get("n_gpu_layers", 99)),
        "-c", str(agent_cfg.get("ctx_size", 8192)),
        "--parallel", str(agent_cfg.get("parallel", 1)),
    ]
    # Optional prompt/KV-cache flags for the frequently-ticking cognition loop and a
    # bigger context after a model swap (Plan §12 / COGNITION.md). All omitted unless
    # set, so the managed launch is byte-identical to before when they're absent.
    if agent_cfg.get("cache_reuse"):          # prefix-cache reuse window (tokens)
        args += ["--cache-reuse", str(agent_cfg["cache_reuse"])]
    if agent_cfg.get("cache_type_k"):         # quantize K cache (e.g. "q8_0") → bigger ctx fits
        args += ["--cache-type-k", str(agent_cfg["cache_type_k"])]
    if agent_cfg.get("cache_type_v"):         # quantize V cache (needs flash attention)
        args += ["--cache-type-v", str(agent_cfg["cache_type_v"])]
    if agent_cfg.get("flash_attn"):           # flash attention (required with a quantized V cache)
        args += ["-fa"]
    if agent_cfg.get("slot_save_path"):       # persist/restore prompt-cache slots across restarts
        args += ["--slot-save-path", str(agent_cfg["slot_save_path"])]
    for extra in agent_cfg.get("extra_args", []) or []:  # escape hatch for any future flag
        args.append(str(extra))
    return args


async def launch(agent_cfg: dict) -> asyncio.subprocess.Process:
    args = build_args(agent_cfg)
    log.info("launching llama-server: %s", " ".join(args))
    return await asyncio.create_subprocess_exec(*args)


async def wait_ready(base_url: str, timeout_s: float = 180.0) -> None:
    # Probe both endpoints: llama.cpp exposes /health (and returns 503 until the
    # model is actually loaded — the signal we want); LM Studio has no /health
    # but answers /v1/models with 200 once it's up. Trying both covers either
    # backend without the caller needing to know which is running.
    deadline = asyncio.get_running_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            for path in ("/health", "/v1/models"):
                with contextlib.suppress(Exception):
                    r = await client.get(f"{base_url}{path}")
                    if r.status_code == 200:
                        log.info("LLM server ready at %s (%s)", base_url, path)
                        return
            await asyncio.sleep(1.0)
    raise RuntimeError("LLM server did not become ready in time")
