"""Shared cloud LLM callers — Gemini (Google), xAI Grok, OpenAI (ChatGPT), DeepSeek.

Used by intel_deep_analysis for the AI council (parallel significance scoring).
All callers use only urllib/httpx/google-genai — no heavy SDK dependencies.

Env vars:
    GOOGLE_API_KEY         — Google AI / Gemini API key (primary)
    GEMINI_API_KEY         — Google AI / Gemini API key (fallback)
    XAI_API_KEY            — xAI API key (Grok)
    OPENAI_API_KEY         — OpenAI API key (ChatGPT)
    DEEPSEEK_API_KEY       — DeepSeek API key
"""

import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("nexus")

# Lazy-init Gemini client (google-genai imported on first use)
_gemini_client = None

# Map legacy Bedrock model IDs to Gemini models
_GEMINI_MODEL_MAP = {
    "us.anthropic.claude-sonnet-4-6": "gemini-2.5-flash",
    "us.anthropic.claude-haiku-4-5-20251001": "gemini-2.0-flash",
    "us.anthropic.claude-opus-4-6": "gemini-2.5-pro",
    "us.anthropic.claude-opus-4-6-v1": "gemini-2.5-pro",
}


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY / GEMINI_API_KEY not set")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _resolve_model(model_id: str) -> str:
    """Resolve legacy Bedrock model IDs to Gemini model names."""
    return _GEMINI_MODEL_MAP.get(model_id, model_id)


def call_claude_bedrock(system_prompt: str, user_prompt: str, timeout: int = 60,
                        temperature: float = 0.3,
                        model_id: str = "gemini-2.5-flash") -> str:
    """Call Gemini via google-genai SDK. Returns text content.

    Kept as call_claude_bedrock for backward compatibility with existing callers.
    """
    from google.genai import types
    client = _get_gemini_client()
    model = _resolve_model(model_id)
    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=4096,
        system_instruction=system_prompt,
    )
    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=config,
    )
    return (response.text or "").strip()


def call_claude_bedrock_haiku(system_prompt: str, user_prompt: str, timeout: int = 60,
                              temperature: float = 0.3) -> str:
    """Call Gemini 2.0 Flash (fast/cheap). Backward-compatible name."""
    return call_claude_bedrock(system_prompt, user_prompt, timeout, temperature,
                               model_id="gemini-2.0-flash")


# Backward-compatible aliases
call_gemini_flash = call_claude_bedrock_haiku


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


def call_openai(system_prompt: str, user_prompt: str, timeout: int = 60,
                temperature: float = 0.3) -> str:
    """Call OpenAI ChatGPT via Chat Completions API. Returns text content."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4.1-mini",
        "max_tokens": 4096,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
                                data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())

    choices = result.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "").strip()


# Backward-compatible alias
call_claude_haiku = call_claude_bedrock_haiku


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
