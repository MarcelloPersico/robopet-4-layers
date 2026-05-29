"""Launch + readiness helper for the llama.cpp server subprocess. Plan §5, §8.1.

Shared by the full orchestrator (orchestrator.py) and the Teensy/Pi-free local
loop (local_loop.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

log = logging.getLogger("llama")


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
    deadline = asyncio.get_running_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            with contextlib.suppress(Exception):
                r = await client.get(f"{base_url}/health")
                if r.status_code == 200:
                    log.info("llama-server ready at %s", base_url)
                    return
            await asyncio.sleep(1.0)
    raise RuntimeError("llama-server did not become ready in time")
