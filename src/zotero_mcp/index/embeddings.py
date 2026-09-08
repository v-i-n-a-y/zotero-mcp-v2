# Copyright 2026 Vinay

"""Embedding providers, behind one small interface.

Three providers, all optional, all lazily imported: a local
sentence-transformers model (free, private, no network), OpenAI, and Gemini.
The provider and model are recorded alongside the index, because vectors from
different models are not comparable and silently mixing them produces results
that look plausible and are meaningless.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from zotero_mcp.errors import AuthError, Unsupported

logger = logging.getLogger(__name__)

SEMANTIC_EXTRA_HINT = "Install the semantic extra: pip install 'zotero-mcp-next[semantic]'"

DEFAULT_MODELS = {
    "default": "all-MiniLM-L6-v2",
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
}

#: Input token ceilings, used to truncate before an API rejects the request.
#: Expressed in characters at roughly four per token, deliberately conservative.
MAX_INPUT_CHARS = {
    "default": 1_800,
    "openai": 30_000,
    "gemini": 8_000,
}


class Embedder(ABC):
    """Turns text into vectors."""

    provider: str
    model: str

    @property
    def signature(self) -> str:
        """Identifies the vector space, so an index cannot mix models."""
        return f"{self.provider}:{self.model}"

    @property
    def max_chars(self) -> int:
        return MAX_INPUT_CHARS.get(self.provider, 2_000)

    def prepare(self, texts: list[str]) -> list[str]:
        """Truncate inputs to what the model will accept."""
        return [text[: self.max_chars] for text in texts]

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Order of the result matches the input."""


class LocalEmbedder(Embedder):
    """sentence-transformers, running on this machine."""

    provider = "default"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or DEFAULT_MODELS["default"]
        self._encoder = None

    def _load(self):
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise Unsupported(
                    "Local embeddings need sentence-transformers.", hint=SEMANTIC_EXTRA_HINT
                ) from exc
            logger.info("Loading embedding model %s", self.model)
            self._encoder = SentenceTransformer(self.model)
        return self._encoder

    def embed(self, texts: list[str]) -> list[list[float]]:
        encoder = self._load()
        vectors = encoder.encode(self.prepare(texts), show_progress_bar=False)
        return [list(map(float, vector)) for vector in vectors]


class OpenAIEmbedder(Embedder):
    provider = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or DEFAULT_MODELS["openai"]
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = None

    def _load(self):
        if self._client is None:
            if not self._api_key:
                raise AuthError("OpenAI embeddings need an API key.", hint="Set OPENAI_API_KEY.")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise Unsupported(
                    "OpenAI embeddings need the openai package.", hint=SEMANTIC_EXTRA_HINT
                ) from exc
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._load()
        response = client.embeddings.create(model=self.model, input=self.prepare(texts))
        # The API does not guarantee response order, but does return an index.
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]


class GeminiEmbedder(Embedder):
    provider = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or DEFAULT_MODELS["gemini"]
        self._api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        self._client = None

    def _load(self):
        if self._client is None:
            if not self._api_key:
                raise AuthError(
                    "Gemini embeddings need an API key.",
                    hint="Set GEMINI_API_KEY (or GOOGLE_API_KEY).",
                )
            try:
                from google import genai
            except ImportError as exc:
                raise Unsupported(
                    "Gemini embeddings need google-genai.", hint=SEMANTIC_EXTRA_HINT
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._load()
        response = client.models.embed_content(model=self.model, contents=self.prepare(texts))
        return [list(embedding.values) for embedding in response.embeddings]


def build_embedder(provider: str, model: str | None = None) -> Embedder:
    """Construct the named embedder."""
    normalised = (provider or "default").strip().lower()
    if normalised in {"default", "local", "sentence-transformers"}:
        return LocalEmbedder(model)
    if normalised == "openai":
        return OpenAIEmbedder(model)
    if normalised in {"gemini", "google"}:
        return GeminiEmbedder(model)
    raise Unsupported(
        f"Unknown embedding provider {provider!r}.",
        hint="Choose one of: default, openai, gemini.",
    )


__all__ = [
    "DEFAULT_MODELS",
    "Embedder",
    "GeminiEmbedder",
    "LocalEmbedder",
    "OpenAIEmbedder",
    "build_embedder",
]
