from __future__ import annotations

import json
import time
from typing import Callable

import requests

from .config import (
    AI_TIMEOUT_SECONDS,
    GEMINI_API_KEY,
    GEMINI_MODELS,
    GROQ_API_KEY,
    GROQ_MODELS,
    OPENROUTER_API_KEY,
    OPENROUTER_MODELS,
)

TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}


class AIError(RuntimeError):
    pass


def _extract_json(text):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").replace("json\n", "", 1).strip()
    try:
        return json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end+1])
        raise


def _gemini(model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(
        url,
        params={"key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=AI_TIMEOUT_SECONDS,
    )
    if r.status_code >= 400:
        raise AIError(f"Gemini HTTP {r.status_code}")
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _openai_compatible(endpoint, api_key, model, prompt, title="Cybero Plus Jobs"):
    r = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": title,
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.15,
        },
        timeout=AI_TIMEOUT_SECONDS,
    )
    if r.status_code >= 400:
        raise AIError(f"Provider HTTP {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


def _providers():
    rows = []
    if GEMINI_API_KEY:
        rows.extend(("gemini", model) for model in GEMINI_MODELS)
    if GROQ_API_KEY:
        rows.extend(("groq", model) for model in GROQ_MODELS)
    if OPENROUTER_API_KEY:
        rows.extend(("openrouter", model) for model in OPENROUTER_MODELS)
    return rows


def generate_json(prompt, validator: Callable[[dict], None] | None = None):
    errors = []
    for provider, model in _providers():
        try:
            if provider == "gemini":
                text = _gemini(model, prompt)
            elif provider == "groq":
                text = _openai_compatible(
                    "https://api.groq.com/openai/v1/chat/completions",
                    GROQ_API_KEY,
                    model,
                    prompt,
                )
            else:
                text = _openai_compatible(
                    "https://openrouter.ai/api/v1/chat/completions",
                    OPENROUTER_API_KEY,
                    model,
                    prompt,
                )
            data = _extract_json(text)
            if validator:
                validator(data)
            data["_provider"] = provider
            data["_model"] = model
            return data
        except Exception as exc:
            errors.append(f"{provider}:{model}: {exc}")
            time.sleep(1)
    raise AIError("All AI providers failed: " + " | ".join(errors[-6:]))
