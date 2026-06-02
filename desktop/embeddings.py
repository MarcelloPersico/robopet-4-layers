"""Local sentence embeddings for the memory stream. Plan §12 (cognition/memory).

A lazy wrapper around a small CPU embedding model (default ``bge-small-en-v1.5``,
384-d). The heavy ``sentence-transformers`` / ``torch`` import is deferred to the
first :meth:`Embedder.encode` call so the lint/test ``.venv`` — which injects a
fake embedder and never runs the orchestrator — never imports it. Mirrors the
lazy-load discipline of ``vlm.py`` / ``asr.py`` / the Kokoro TTS backend.

Embeddings are L2-normalized so cosine similarity is a plain dot product, which
is what ``memory.MemoryStore.retrieve`` relies on for its relevance term.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("embeddings")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384


class Embedder:
    """Lazy local sentence embedder. ``encode`` returns float32, L2-normalized."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None  # lazy — see _ensure()
        self.dim = DEFAULT_DIM

    def _ensure(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy heavy import

            log.info("loading embedding model %s on %s", self.model_name, self.device)
            self._model = SentenceTransformer(self.model_name, device=self.device)
            # sentence-transformers 5.x renamed get_sentence_embedding_dimension ->
            # get_embedding_dimension; support both.
            dim_fn = (getattr(self._model, "get_embedding_dimension", None)
                      or self._model.get_sentence_embedding_dimension)
            self.dim = int(dim_fn())

    def encode(self, text: str | list[str]) -> np.ndarray:
        """Embed text. Returns shape ``(dim,)`` for a str, ``(n, dim)`` for a list.

        Output is float32 and L2-normalized (so ``a @ b`` == cosine similarity).
        """
        self._ensure()
        one = isinstance(text, str)
        vecs = self._model.encode(
            [text] if one else list(text),
            normalize_embeddings=True,
            convert_to_numpy=True,
        ).astype(np.float32)
        return vecs[0] if one else vecs
