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
) -> tuple[str, str]:
    """Call an LLM with Ollama-first, Gemini-fallback logic.

    Returns:
        (content, backend_name) — the generated text and which backend was used.
        backend_name is e.g. "Ollama/qwen3-235b-a22b-q4km" or "Gemini/gemini-2.5-flash".

    Raises:
        RuntimeError if both backends fail.
    """
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL", "qwen3-235b-a22b-q4km")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # --- Try Ollama first (skip if URL is empty) ---
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

    # --- Gemini fallback ---
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
