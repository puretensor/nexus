"""Reusable AI Council — parallel multi-model review with scoring and quorum.

Extracted from intel_deep_analysis.py, generalised for any content pipeline.
Uses ThreadPoolExecutor to run 4 cloud LLMs in parallel, each with a distinct
editorial role. Returns structured scores, feedback, and a pass/fail verdict.

Usage:
    from observers.ai_council import run_council, CouncilResult

    result = run_council(
        content="Article text here...",
        roles={
            "editor": {
                "model": "haiku",
                "system": "You are an editor-in-chief...",
                "prompt": "Evaluate this article for prose quality...",
            },
            ...
        },
        threshold=7.5,
    )
    if result.passed:
        publish(content)
    else:
        revise(content, result.feedback)
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
_nexus_root = str(Path(__file__).resolve().parent.parent)
if _nexus_root not in _sys.path:
    _sys.path.insert(0, _nexus_root)

from observers.cloud_llm import (
    call_claude_haiku,
    call_deepseek,
    call_gemini_flash,
    call_xai_grok,
    extract_json,
)

log = logging.getLogger("nexus")

# Model name → caller function
MODEL_CALLERS = {
    "haiku": call_claude_haiku,
    "gemini": call_gemini_flash,
    "grok": call_xai_grok,
    "deepseek": call_deepseek,
}

# JSON schema instruction appended to every council prompt
JSON_INSTRUCTION = """

Respond with ONLY a JSON object in this exact format:
```json
{
  "score": <integer 1-10>,
  "verdict": "<approve|revise|reject>",
  "strengths": ["strength 1", "strength 2"],
  "concerns": ["concern 1", "concern 2"],
  "suggestions": ["suggestion 1", "suggestion 2"]
}
```
No other text before or after the JSON."""


@dataclass
class CouncilMemberResult:
    """Result from a single council member."""
    role: str
    model: str
    score: int = 0
    verdict: str = ""  # approve, revise, reject
    strengths: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class CouncilResult:
    """Aggregate result from the full council."""
    passed: bool = False
    average_score: float = 0.0
    verdict: str = ""  # proceed, revise, abort
    members: list[CouncilMemberResult] = field(default_factory=list)
    quorum_met: bool = False
    responded: int = 0
    total: int = 0

    @property
    def feedback(self) -> str:
        """Concatenate all concerns and suggestions for revision prompts."""
        parts = []
        for m in self.members:
            if m.error:
                continue
            if m.concerns:
                parts.append(f"[{m.role}] Concerns: {'; '.join(m.concerns)}")
            if m.suggestions:
                parts.append(f"[{m.role}] Suggestions: {'; '.join(m.suggestions)}")
        return "\n".join(parts)

    @property
    def scores_table(self) -> str:
        """Format scores as a simple text table for emails/logs."""
        lines = ["Role | Model | Score | Verdict", "--- | --- | --- | ---"]
        for m in self.members:
            if m.error:
                lines.append(f"{m.role} | {m.model} | ERROR | {m.error[:50]}")
            else:
                lines.append(f"{m.role} | {m.model} | {m.score}/10 | {m.verdict}")
        if self.responded > 0:
            lines.append(f"**Average** | | **{self.average_score:.1f}/10** | **{self.verdict.upper()}**")
        return "\n".join(lines)


def _call_with_retry(fn, name: str, max_retries: int = 2, backoff: float = 5.0):
    """Call a function with retries and exponential backoff."""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                log.info("ai_council: %s attempt %d failed (%s), retrying in %.0fs",
                         name, attempt + 1, e, wait)
                time.sleep(wait)
    raise last_err


def _call_member(role: str, config: dict, content: str,
                 timeout: int = 120) -> CouncilMemberResult:
    """Call a single council member and parse its JSON response."""
    model = config["model"]
    system = config["system"]
    prompt = config["prompt"] + f"\n\nCONTENT TO EVALUATE:\n{content[:12000]}" + JSON_INSTRUCTION

    caller = MODEL_CALLERS.get(model)
    if not caller:
        return CouncilMemberResult(role=role, model=model,
                                   error=f"Unknown model: {model}")

    try:
        raw = _call_with_retry(
            lambda: caller(system, prompt, timeout=timeout, temperature=0.3),
            name=f"{role}/{model}",
        )
        parsed = extract_json(raw)
        if not isinstance(parsed, dict):
            return CouncilMemberResult(role=role, model=model,
                                       error="Invalid JSON response")

        return CouncilMemberResult(
            role=role,
            model=model,
            score=max(1, min(10, int(parsed.get("score", 0)))),
            verdict=parsed.get("verdict", "revise"),
            strengths=parsed.get("strengths", [])[:5],
            concerns=parsed.get("concerns", [])[:5],
            suggestions=parsed.get("suggestions", [])[:5],
        )
    except Exception as e:
        return CouncilMemberResult(role=role, model=model,
                                   error=str(e)[:200])


def run_council(
    content: str,
    roles: dict[str, dict],
    threshold: float = 7.5,
    min_score: int = 5,
    min_quorum: int = 3,
    timeout: int = 120,
) -> CouncilResult:
    """Run parallel AI council review.

    Args:
        content: The text to evaluate.
        roles: Dict of {role_name: {"model": "haiku|gemini|grok|deepseek",
                                     "system": "system prompt",
                                     "prompt": "evaluation prompt"}}.
        threshold: Average score required to pass (default 7.5).
        min_score: Minimum individual score — any below triggers revise (default 5).
        min_quorum: Minimum number of models that must respond (default 3).
        timeout: Per-model timeout in seconds (default 120).

    Returns:
        CouncilResult with aggregate scores and pass/fail verdict.
    """
    total = len(roles)
    members = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_call_member, role, config, content, timeout): role
            for role, config in roles.items()
        }
        for future in as_completed(futures, timeout=timeout + 30):
            role = futures[future]
            try:
                result = future.result()
                members.append(result)
                if result.error:
                    log.warning("ai_council: %s failed: %s", role, result.error)
                else:
                    log.info("ai_council: %s scored %d/10 (%s)",
                             role, result.score, result.verdict)
            except Exception as e:
                members.append(CouncilMemberResult(
                    role=role, model=roles[role].get("model", "?"),
                    error=str(e)[:200],
                ))
                log.warning("ai_council: %s exception: %s", role, e)

    # Calculate aggregate
    valid = [m for m in members if not m.error]
    responded = len(valid)
    quorum_met = responded >= min_quorum

    if not valid:
        return CouncilResult(
            passed=False, verdict="abort", members=members,
            quorum_met=False, responded=0, total=total,
        )

    avg = sum(m.score for m in valid) / responded
    any_below_min = any(m.score < min_score for m in valid)

    if avg >= threshold and not any_below_min and quorum_met:
        verdict = "proceed"
        passed = True
    elif avg < 5.0:
        verdict = "abort"
        passed = False
    else:
        verdict = "revise"
        passed = False

    return CouncilResult(
        passed=passed,
        average_score=round(avg, 2),
        verdict=verdict,
        members=members,
        quorum_met=quorum_met,
        responded=responded,
        total=total,
    )


# ── Standalone testing ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # Minimal test — ensure all 4 models respond with valid JSON
    test_content = (
        "The European Central Bank's decision to cut interest rates by 50 basis "
        "points caught markets off guard, sending the euro to a six-month low "
        "against the dollar. Analysts at Goldman Sachs warned that the move "
        "signals deeper economic fragility than previously acknowledged."
    )

    test_roles = {
        "editor": {
            "model": "haiku",
            "system": "You are an editor evaluating prose quality.",
            "prompt": "Score this text for writing quality, clarity, and structure.",
        },
        "fact_checker": {
            "model": "gemini",
            "system": "You are a fact-checker evaluating accuracy.",
            "prompt": "Score this text for factual accuracy and source quality.",
        },
        "analyst": {
            "model": "grok",
            "system": "You are a strategic analyst evaluating relevance.",
            "prompt": "Score this text for strategic relevance and timeliness.",
        },
        "critic": {
            "model": "deepseek",
            "system": "You are a devil's advocate looking for logical flaws.",
            "prompt": "Score this text for logical rigour and missing perspectives.",
        },
    }

    print("Running AI Council test with 4 models...")
    print(f"Content: {test_content[:100]}...\n")

    result = run_council(test_content, test_roles, threshold=5.0, timeout=60)

    print(f"\nResults ({result.responded}/{result.total} responded):")
    print(f"  Average: {result.average_score}/10")
    print(f"  Verdict: {result.verdict}")
    print(f"  Quorum met: {result.quorum_met}")
    print(f"  Passed: {result.passed}\n")

    for m in result.members:
        if m.error:
            print(f"  [{m.role}] {m.model}: ERROR — {m.error}")
        else:
            print(f"  [{m.role}] {m.model}: {m.score}/10 ({m.verdict})")
            if m.strengths:
                print(f"    Strengths: {', '.join(m.strengths[:2])}")
            if m.concerns:
                print(f"    Concerns: {', '.join(m.concerns[:2])}")

    sys.exit(0 if result.responded >= 3 else 1)
