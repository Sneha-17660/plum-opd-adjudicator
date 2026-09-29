"""Thin async client for Groq's OpenAI-compatible chat API, returning JSON.

Handles: 429/5xx retries (honouring retry-after), models that reject
``response_format`` (retries without it), reasoning models that emit
``<think>`` blocks, and replies wrapped in code fences.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("groq")


class LLMError(RuntimeError):
    """Raised when the model cannot produce a usable answer. Message is user-safe."""


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first complete JSON object from a model reply."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    cleaned = re.sub(r"^\s*```(?:json)?", "", cleaned.strip(), flags=re.I)
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise LLMError("The AI model did not return valid JSON.")


class GroqClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    async def chat_json(self, *, model: str, system: str, content: str | list[dict[str, Any]],
                        max_tokens: int = 2500, agent: str = "agent") -> dict[str, Any]:
        if not self.settings.groq_api_key:
            raise LLMError("GROQ_API_KEY is not configured on the server.")
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        url = f"{self.settings.groq_base_url}/chat/completions"
        retries = self.settings.groq_max_retries
        last_error = "no response"
        async with httpx.AsyncClient(timeout=self.settings.groq_timeout_s, transport=self._transport) as client:
            attempt = 0
            while attempt <= retries:
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"network error: {exc.__class__.__name__}"
                    attempt += 1
                    await asyncio.sleep(min(8, 2 ** attempt))
                    continue
                if resp.status_code == 400 and "response_format" in payload and _mentions_format(resp.text):
                    payload.pop("response_format")          # model doesn't support JSON mode; retry plainly
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    attempt += 1
                    await asyncio.sleep(_retry_after(resp, attempt))
                    continue
                if resp.status_code >= 400:
                    log.warning("%s: Groq HTTP %s: %s", agent, resp.status_code, resp.text[:500])
                    raise LLMError(_friendly_error(resp))
                try:
                    text = resp.json()["choices"][0]["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMError("The AI model returned an unexpected response.") from exc
                try:
                    return parse_json_object(text)
                except LLMError:
                    last_error = "invalid JSON"
                    attempt += 1
                    continue
        if last_error == "HTTP 429":
            raise LLMError("The AI provider's rate limit was reached. Please retry in a minute.")
        raise LLMError(f"The AI provider could not be reached ({last_error}). Please retry.")


def _mentions_format(text: str) -> bool:
    t = text.lower()
    return "response_format" in t or "json_object" in t or "json mode" in t


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return max(1.0, min(20.0, float(resp.headers.get("retry-after", ""))))
    except ValueError:
        return float(min(20, 2 ** attempt))


def _friendly_error(resp: httpx.Response) -> str:
    if resp.status_code in (401, 403):
        return "The server's Groq API key was rejected. Check GROQ_API_KEY."
    if resp.status_code == 404:
        return "The configured Groq model was not found. Check GROQ_MODEL."
    if resp.status_code == 413:
        return "The documents are too large for the AI model. Upload fewer or smaller pages."
    return f"The AI provider rejected the request (HTTP {resp.status_code})."
