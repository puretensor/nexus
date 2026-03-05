"""Shared cloud LLM callers — Claude Bedrock, xAI Grok, OpenAI (ChatGPT), DeepSeek.

Used by intel_deep_analysis for the AI council (parallel significance scoring).
All callers use only urllib/httpx/boto3 — no heavy SDK dependencies.

Env vars:
    AWS_ACCESS_KEY_ID      — AWS credentials for Bedrock
    AWS_SECRET_ACCESS_KEY  — AWS credentials for Bedrock
    AWS_DEFAULT_REGION     — AWS region (default us-east-1)
    GEMINI_API_KEY         — Google AI Studio key (kept for Deep Research / Imagen)
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

# Lazy-init Bedrock client (boto3 imported on first use)
_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        import boto3
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _bedrock_client


def call_claude_bedrock(system_prompt: str, user_prompt: str, timeout: int = 60,
                        temperature: float = 0.3,
                        model_id: str = "us.anthropic.claude-sonnet-4-6") -> str:
    """Call Claude on AWS Bedrock via converse API. Returns text content."""
    client = _get_bedrock_client()
    response = client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"temperature": temperature, "maxTokens": 4096},
    )
    content = response.get("output", {}).get("message", {}).get("content", [])
    if not content:
        return ""
    return content[0].get("text", "").strip()


def call_claude_bedrock_haiku(system_prompt: str, user_prompt: str, timeout: int = 60,
                              temperature: float = 0.3) -> str:
    """Call Claude Haiku on AWS Bedrock. Cheaper, for bulk/simple tasks."""
    return call_claude_bedrock(system_prompt, user_prompt, timeout, temperature,
                               model_id="us.anthropic.claude-haiku-4-5-20251001")


# Backward-compatible alias — existing callers of call_gemini_flash get Haiku
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


# Backward-compatible alias — callers importing call_claude_haiku get Bedrock Haiku.
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
