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
    return [
        agent_cfg["llama_server_exe"], "-m", agent_cfg["model_path"],
        "--host", agent_cfg["host"], "--port", str(agent_cfg["port"]),
        "-ngl", str(agent_cfg.get("n_gpu_layers", 99)),
        "-c", str(agent_cfg.get("ctx_size", 8192)),
        "--parallel", str(agent_cfg.get("parallel", 1)),
    ]


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
