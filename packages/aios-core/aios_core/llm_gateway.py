"""
LLM Gateway — provider abstraction.

Every service calls this. Nobody imports openai, anthropic, ollama, or ibm directly.
Swapping providers is one environment variable change.

Phase 1 (local, needs Docker):  LLM_PROVIDER=ollama
Phase 1 (IBM Cloud, no Docker): LLM_PROVIDER=watsonx  <-- use this
Phase 2 alternatives:           LLM_PROVIDER=openai or LLM_PROVIDER=anthropic

IBM watsonx.ai Authentication:
  1. Exchange IBM Cloud IAM API key for a Bearer token
     POST https://iam.cloud.ibm.com/identity/token
  2. Use Bearer token on all watsonx.ai REST calls (auto-refreshed every 1h)
  3. Project ID scopes the model to your watsonx.ai project

Usage:
    from aios_core.llm_gateway import get_llm_gateway

    llm = get_llm_gateway()
    response = await llm.complete([LLMMessage(role="user", content="Hello")])
    async for token in llm.stream([...]):
        print(token, end="")
    vectors = await llm.embed(["text1", "text2"])
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx
from pydantic import BaseModel

from aios_core.config import settings

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Shared models
# ─────────────────────────────────────────────────────────────────

class LLMMessage(BaseModel):
    role: str      # "system" | "user" | "assistant"
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    usage: dict[str, int] = {}
    raw: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────

class LLMGateway(ABC):
    """
    Abstract provider interface. Add a new provider by subclassing this
    and registering it in get_llm_gateway().
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Single-shot completion. Returns the full response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Streaming completion. Yields text chunks as they arrive."""
        ...

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """
        Embed a list of texts. Returns one vector per text.
        Batch size limit is provider-dependent; callers should chunk large lists.
        """
        ...


# ─────────────────────────────────────────────────────────────────
# Ollama  (Phase 1 — local)
# ─────────────────────────────────────────────────────────────────

class OllamaGateway(LLMGateway):
    def __init__(
        self,
        base_url: str | None = None,
        default_model: str | None = None,
        embed_model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.default_model = default_model or settings.ollama_default_model
        self.embed_model = embed_model or settings.ollama_embed_model
        self.timeout = timeout

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        use_model = model or self.default_model
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if response_format and response_format.get("type") == "json_object":
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        content = data.get("message", {}).get("content", "")
        return LLMResponse(
            content=content,
            model=use_model,
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
            raw=data,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        use_model = model or self.default_model
        payload = {
            "model": use_model,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.strip():
                        try:
                            chunk = json.loads(line)
                            delta = chunk.get("message", {}).get("content", "")
                            if delta:
                                yield delta
                        except json.JSONDecodeError:
                            continue

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        use_model = model or self.embed_model
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for text in texts:
                resp = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": use_model, "prompt": text},
                )
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"])
        return vectors


# ─────────────────────────────────────────────────────────────────
# OpenAI  (Phase 2)
# ─────────────────────────────────────────────────────────────────

class OpenAIGateway(LLMGateway):
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.openai_api_key
        self.default_model = settings.openai_default_model
        self.embed_model = settings.openai_embed_model
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        import openai  # lazy import — not installed in Phase 1
        client = openai.AsyncOpenAI(api_key=self._api_key)
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        return LLMResponse(
            content=msg.content or "",
            model=resp.model,
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            },
            raw=resp.model_dump(),
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        import openai
        client = openai.AsyncOpenAI(api_key=self._api_key)
        async with client.chat.completions.stream(
            model=model or self.default_model,
            messages=[m.model_dump() for m in messages],
            temperature=temperature,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        import openai
        client = openai.AsyncOpenAI(api_key=self._api_key)
        use_model = model or self.embed_model
        resp = await client.embeddings.create(input=texts, model=use_model)
        return [item.embedding for item in resp.data]


# ─────────────────────────────────────────────────────────────────
# Anthropic  (Phase 2 alternative)
# ─────────────────────────────────────────────────────────────────

class AnthropicGateway(LLMGateway):
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.default_model = settings.anthropic_default_model
        if not self._api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        import anthropic  # lazy import
        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        system_messages = [m for m in messages if m.role == "system"]
        user_messages = [m for m in messages if m.role != "system"]
        system_content = system_messages[0].content if system_messages else ""
        resp = await client.messages.create(
            model=model or self.default_model,
            max_tokens=max_tokens,
            system=system_content,
            messages=[{"role": m.role, "content": m.content} for m in user_messages],
        )
        return LLMResponse(
            content=resp.content[0].text if resp.content else "",
            model=resp.model,
            usage={
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            },
            raw=resp.model_dump(),
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        system_messages = [m for m in messages if m.role == "system"]
        user_messages = [m for m in messages if m.role != "system"]
        async with client.messages.stream(
            model=model or self.default_model,
            max_tokens=4096,
            system=system_messages[0].content if system_messages else "",
            messages=[{"role": m.role, "content": m.content} for m in user_messages],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        raise NotImplementedError(
            "Anthropic does not provide an embedding API. "
            "Use OpenAIGateway or OllamaGateway for embeddings."
        )


# ─────────────────────────────────────────────────────────────────
# IBM watsonx.ai  (IBM Cloud, no Docker/GPU needed)
# ─────────────────────────────────────────────────────────────────

class WatsonxGateway(LLMGateway):
    """
    IBM watsonx.ai gateway — uses IBM Cloud REST APIs directly.

    Required env vars (set in .env):
      IBM_API_KEY         — IBM Cloud IAM API key
      WATSONX_URL         — e.g. https://us-south.ml.cloud.ibm.com
      WATSONX_PROJECT_ID  — from your watsonx.ai project settings
    """

    IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
    API_VERSION = "2024-05-01"

    def __init__(self) -> None:
        self._api_key = settings.ibm_api_key
        self._base_url = settings.watsonx_url.rstrip("/")
        self._project_id = settings.watsonx_project_id
        self._default_model = settings.watsonx_default_model
        self._embed_model = settings.watsonx_embed_model
        self._token: str | None = None
        self._token_expires_at: float = 0.0

        if not self._api_key:
            raise ValueError(
                "IBM_API_KEY is required when LLM_PROVIDER=watsonx. "
                "Get it from: https://cloud.ibm.com → Manage → Access (IAM) → API keys"
            )
        if not self._project_id:
            raise ValueError(
                "WATSONX_PROJECT_ID is required when LLM_PROVIDER=watsonx. "
                "Get it from your watsonx.ai project → Manage → General → Project ID"
            )

    async def _get_token(self) -> str:
        """Exchange IBM IAM API key for a Bearer token. Auto-refreshes 5min before expiry."""
        import time
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.IAM_TOKEN_URL,
                data={
                    "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                    "apikey": self._api_key,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)
        log.debug("IBM IAM token refreshed (expires in %ds)", data.get("expires_in", 3600))
        return self._token

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        token = await self._get_token()
        use_model = model or self._default_model
        prompt = self._messages_to_prompt(messages)

        if response_format and response_format.get("type") == "json_object":
            prompt += "\n\nRespond ONLY with valid JSON. No explanation outside the JSON."

        payload: dict[str, Any] = {
            "model_id": use_model,
            "input": prompt,
            "project_id": self._project_id,
            "parameters": {
                "decoding_method": "greedy" if temperature == 0.0 else "sample",
                "temperature": temperature,
                "max_new_tokens": max_tokens,
                "repetition_penalty": 1.1,
            },
        }

        url = f"{self._base_url}/ml/v1/text/generation?version={self.API_VERSION}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [{}])
        content = results[0].get("generated_text", "").strip()
        return LLMResponse(
            content=content,
            model=use_model,
            usage={
                "prompt_tokens": results[0].get("input_token_count", 0),
                "completion_tokens": results[0].get("generated_token_count", 0),
            },
            raw=data,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        token = await self._get_token()
        use_model = model or self._default_model
        prompt = self._messages_to_prompt(messages)

        payload: dict[str, Any] = {
            "model_id": use_model,
            "input": prompt,
            "project_id": self._project_id,
            "parameters": {
                "decoding_method": "sample",
                "temperature": temperature,
                "max_new_tokens": 2048,
            },
        }

        url = f"{self._base_url}/ml/v1/text/generation_stream?version={self.API_VERSION}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", url, json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                            delta = chunk.get("results", [{}])[0].get("generated_text", "")
                            if delta:
                                yield delta
                        except json.JSONDecodeError:
                            continue

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """
        Generate 768-dim embeddings using ibm/slate-125m-english-rtrvr-v2.
        Vector dimension matches Qdrant collection (QDRANT_VECTOR_SIZE=768).
        """
        token = await self._get_token()
        use_model = model or self._embed_model

        payload: dict[str, Any] = {
            "model_id": use_model,
            "inputs": texts,
            "project_id": self._project_id,
        }

        url = f"{self._base_url}/ml/v1/text/embeddings?version={self.API_VERSION}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url, json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # watsonx.ai returns: {"results": [{"embedding": [0.1, 0.2, ...]}, ...]}
        return [r["embedding"] for r in data.get("results", [])]

    def _messages_to_prompt(self, messages: list[LLMMessage]) -> str:
        """
        Convert chat messages to Granite instruction format.
        Granite 3.x models use <|system|>, <|user|>, <|assistant|> tokens.
        """
        parts: list[str] = []
        for msg in messages:
            if msg.role == "system":
                parts.append(f"<|system|>\n{msg.content}\n<|end_of_text|>")
            elif msg.role == "user":
                parts.append(f"<|user|>\n{msg.content}\n<|end_of_text|>")
            elif msg.role == "assistant":
                parts.append(f"<|assistant|>\n{msg.content}\n<|end_of_text|>")
        parts.append("<|assistant|>")
        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────

def get_llm_gateway() -> LLMGateway:
    """
    Returns the configured LLM gateway singleton.
    Controlled by LLM_PROVIDER env var.

    IBM Cloud (no Docker):   LLM_PROVIDER=watsonx   (recommended without Docker)
    Local (needs Docker):    LLM_PROVIDER=ollama    (default)
    Cloud alternatives:      LLM_PROVIDER=openai  or  LLM_PROVIDER=anthropic
    """
    provider = settings.llm_provider
    match provider:
        case "ollama":
            return OllamaGateway()
        case "openai":
            return OpenAIGateway()
        case "anthropic":
            return AnthropicGateway()
        case "watsonx":
            return WatsonxGateway()
        case _:
            raise ValueError(
                f"Unknown LLM_PROVIDER: {provider!r}. "
                "Must be 'ollama', 'openai', 'anthropic', or 'watsonx'."
            )
