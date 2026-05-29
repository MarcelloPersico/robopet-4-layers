"""Moondream2 vision wrapper: describe(jpeg_bytes) -> str. Plan §4.

Backs the agent's ``see()`` tool. ~1.5 s/image on the 5070 Ti (Plan §4), so the
orchestrator hides it behind a filler phrase (Plan §5.6 step 6). The heavy
`transformers` / `torch` imports are deferred to :meth:`load`.
"""

from __future__ import annotations

import asyncio
import io
import logging

log = logging.getLogger("vlm")

DEFAULT_PROMPT = "Describe what you see in one or two short sentences."


class VLM:
    def __init__(self, model: str = "vikhyatk/moondream2", device: str = "cuda", dtype: str = "float16"):
        self._model_name = model
        self._device = device
        self._dtype = dtype
        self._model = None
        self._tokenizer = None
        self._lock = asyncio.Lock()  # Moondream is not reentrant; serialize calls

    async def load(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_sync)
        log.info("VLM loaded: %s (%s/%s)", self._model_name, self._device, self._dtype)

    def _load_sync(self) -> None:
        import torch  # lazy
        from transformers import AutoModelForCausalLM, AutoTokenizer

        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}[
            self._dtype
        ]
        self._model = AutoModelForCausalLM.from_pretrained(
            self._model_name, trust_remote_code=True, torch_dtype=dtype
        ).to(self._device)
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name, trust_remote_code=True)

    async def describe(self, jpeg_bytes: bytes, prompt: str = DEFAULT_PROMPT) -> str:
        if self._model is None:
            return "(vision unavailable)"
        async with self._lock:
            loop = asyncio.get_running_loop()
            return (await loop.run_in_executor(None, self._describe_sync, jpeg_bytes, prompt)).strip()

    def _describe_sync(self, jpeg_bytes: bytes, prompt: str) -> str:
        from PIL import Image

        image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        # Moondream2's documented two-step API: encode then answer.
        enc = self._model.encode_image(image)
        return self._model.answer_question(enc, prompt, self._tokenizer)
