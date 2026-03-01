"""Shared cloud LLM callers — Gemini, xAI Grok, Claude Haiku, DeepSeek.

Used by intel_deep_analysis for the AI council (parallel significance scoring).
All callers use only urllib/httpx — no heavy SDK dependencies.

Env vars:
    GEMINI_API_KEY    — Google AI Studio key
    XAI_API_KEY       — xAI API key (Grok)
    ANTHROPIC_API_KEY — Anthropic API key (Claude)
    DEEPSEEK_API_KEY  — DeepSeek API key
"""

import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("nexus")


def call_gemini_flash(system_prompt: str, user_prompt: str, timeout: int = 60,
                      temperature: float = 0.3) -> str:
    """Call Gemini 2.5 Flash via REST API. Returns text content."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    model = "gemini-2.5-flash"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={api_key}"
    )

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingBudget": 4096},
        },
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    candidates = result.get("candidates", [])
    if not candidates:
        return ""

    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [p.get("text", "") for p in parts if not p.get("thought") and p.get("text")]
    return "\n".join(texts).strip()


def call_xai_grok(system_prompt: str, user_prompt: str, timeout: int = 60,
                  temperature: float = 0.3, tools: list | None = None) -> str:
    """Call xAI Grok via Chat Completions API. Returns text content."""
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("XAI_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "PureTensor-Nexus/2.0",
    }
    payload = {
        "model": "grok-3-mini-fast",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4096,
        "temperature": temperature,
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions",
                                data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    choices = result.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "").strip()


def call_claude_haiku(system_prompt: str, user_prompt: str, timeout: int = 60,
                      temperature: float = 0.3) -> str:
    """Call Claude Haiku via Anthropic Messages API. Returns text content."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    content = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            content += block.get("text", "")
    return content.strip()


def call_deepseek(system_prompt: str, user_prompt: str, timeout: int = 60,
                   temperature: float = 0.3) -> str:
    """Call DeepSeek via OpenAI-compatible Chat Completions API. Returns text content."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "max_tokens": 4096,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    choices = result.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "").strip()


def extract_json(text: str) -> dict | list | None:
    """Extract JSON object or array from LLM response text."""
    # Try markdown code block first
    m = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON
    for pattern in [r'\{.*\}', r'\[.*\]']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None
