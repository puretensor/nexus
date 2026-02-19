"""Shared LLM caller — Ollama-first with Gemini Flash fallback.

Used by observers that need LLM generation (cyber_threat_feed, intel_briefing).
When running on TC with Ollama available, uses the local model. When Ollama is
unreachable (TC powered off, failover runner on fox-n1), falls back to Gemini
Flash via the REST API.

No new dependencies — uses only urllib.request.

Env vars:
    OLLAMA_URL      — e.g. http://localhost:11434 (empty string = skip Ollama)
    OLLAMA_MODEL    — e.g. qwen3-235b-a22b-q4km
    GEMINI_API_KEY  — Google AI Studio key for Gemini Flash
    GEMINI_MODEL    — default: gemini-2.5-flash
"""

import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("nexus")


def call_llm(
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
    num_predict: int = 8192,
    temperature: float = 0.4,
    preferred_backend: str = "auto",
    override_ollama_model: str | None = None,
    override_gemini_model: str | None = None,
) -> tuple[str, str]:
    """Call an LLM with configurable backend priority.

    Args:
        preferred_backend:
            "auto"   — Ollama first, Gemini fallback (default, legacy behaviour)
            "gemini" — Gemini first, Ollama fallback
            "ollama" — Ollama only, no Gemini fallback
        override_ollama_model: Use this Ollama model instead of OLLAMA_MODEL env var.
        override_gemini_model: Use this Gemini model instead of GEMINI_MODEL env var.

    Returns:
        (content, backend_name) — the generated text and which backend was used.
        backend_name is e.g. "Ollama/qwen3-235b-a22b-q4km" or "Gemini/gemini-2.5-flash".

    Raises:
        RuntimeError if all attempted backends fail.
    """
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model = override_ollama_model or os.environ.get("OLLAMA_MODEL", "qwen3-235b-a22b-q4km")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = override_gemini_model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if preferred_backend == "gemini":
        # Gemini first, Ollama fallback
        return _try_gemini_then_ollama(
            gemini_key, gemini_model, ollama_url, ollama_model,
            system_prompt, user_prompt, timeout, num_predict, temperature,
        )
    elif preferred_backend == "ollama":
        # Ollama only
        return _try_ollama_only(
            ollama_url, ollama_model,
            system_prompt, user_prompt, timeout, num_predict, temperature,
        )
    else:
        # "auto" — Ollama first, Gemini fallback (legacy default)
        return _try_ollama_then_gemini(
            ollama_url, ollama_model, gemini_key, gemini_model,
            system_prompt, user_prompt, timeout, num_predict, temperature,
        )


def _try_ollama_then_gemini(
    ollama_url, ollama_model, gemini_key, gemini_model,
    system_prompt, user_prompt, timeout, num_predict, temperature,
) -> tuple[str, str]:
    """Ollama first, Gemini fallback."""
    if ollama_url:
        try:
            content = _call_ollama(
                ollama_url, ollama_model, system_prompt, user_prompt,
                timeout=timeout, num_predict=num_predict, temperature=temperature,
            )
            if content:
                backend = f"Ollama/{ollama_model}"
                log.info("LLM call succeeded via %s (%d chars)", backend, len(content))
                return content, backend
            log.warning("Ollama returned empty response, trying Gemini fallback")
        except Exception as e:
            log.warning("Ollama call failed (%s), trying Gemini fallback", e)

    if not gemini_key:
        raise RuntimeError("Ollama unavailable and GEMINI_API_KEY not set — cannot generate")

    try:
        content = _call_gemini(
            gemini_key, gemini_model, system_prompt, user_prompt,
            timeout=timeout, temperature=temperature,
        )
        if content:
            backend = f"Gemini/{gemini_model}"
            log.info("LLM call succeeded via %s (%d chars)", backend, len(content))
            return content, backend
        raise RuntimeError("Gemini returned empty response")
    except Exception as e:
        raise RuntimeError(f"Both Ollama and Gemini failed. Gemini error: {e}") from e


def _try_gemini_then_ollama(
    gemini_key, gemini_model, ollama_url, ollama_model,
    system_prompt, user_prompt, timeout, num_predict, temperature,
) -> tuple[str, str]:
    """Gemini first, Ollama fallback."""
    if gemini_key:
        try:
            content = _call_gemini(
                gemini_key, gemini_model, system_prompt, user_prompt,
                timeout=timeout, temperature=temperature,
            )
            if content:
                backend = f"Gemini/{gemini_model}"
                log.info("LLM call succeeded via %s (%d chars)", backend, len(content))
                return content, backend
            log.warning("Gemini returned empty response, trying Ollama fallback")
        except Exception as e:
            log.warning("Gemini call failed (%s), trying Ollama fallback", e)

    if not ollama_url:
        raise RuntimeError("Gemini unavailable and OLLAMA_URL not set — cannot generate")

    try:
        content = _call_ollama(
            ollama_url, ollama_model, system_prompt, user_prompt,
            timeout=timeout, num_predict=num_predict, temperature=temperature,
        )
        if content:
            backend = f"Ollama/{ollama_model}"
            log.info("LLM call succeeded via %s (%d chars)", backend, len(content))
            return content, backend
        raise RuntimeError("Ollama returned empty response")
    except Exception as e:
        raise RuntimeError(f"Both Gemini and Ollama failed. Ollama error: {e}") from e


def _try_ollama_only(
    ollama_url, ollama_model,
    system_prompt, user_prompt, timeout, num_predict, temperature,
) -> tuple[str, str]:
    """Ollama only, no fallback."""
    if not ollama_url:
        raise RuntimeError("OLLAMA_URL not set — cannot generate")

    content = _call_ollama(
        ollama_url, ollama_model, system_prompt, user_prompt,
        timeout=timeout, num_predict=num_predict, temperature=temperature,
    )
    if content:
        backend = f"Ollama/{ollama_model}"
        log.info("LLM call succeeded via %s (%d chars)", backend, len(content))
        return content, backend
    raise RuntimeError("Ollama returned empty response")


def _call_ollama(
    url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
    num_predict: int = 8192,
    temperature: float = 0.4,
) -> str:
    """Call Ollama chat API. Returns stripped content."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    content = result.get("message", {}).get("content", "")
    # Strip thinking tokens from Qwen3
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
    return content.strip()


def _call_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 300,
    temperature: float = 0.4,
) -> str:
    """Call Gemini REST API (generativelanguage.googleapis.com). Returns content."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={api_key}"
    )

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            },
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 8192,
        },
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    # Extract text from Gemini response
    candidates = result.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        return ""
    return parts[0].get("text", "").strip()
