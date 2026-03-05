#!/usr/bin/env python3
"""Bretalon AutoPublish Observer — autonomous article pipeline.

Generates, reviews, and publishes strategic research articles to bretalon.com
on a rotating 2/3-per-week schedule. Uses an AI council (4 models) for quality
gating, Gemini Deep Research for substance, and Claude Sonnet for writing.

Schedule: Alternating weeks — Mon+Thu (2/wk) or Mon+Wed+Fri (3/wk).
Runtime: ~15-25 min per article.

Stages:
  1. Topic selection (manual queue or auto-discovery)
  2. Deep research (Gemini Deep Research API)
  3. Article writing (Claude Sonnet)
  4. AI Council review (4 models, parallel)
  5. Image generation (Imagen 4)
  6. WordPress publishing (future-dated, +3 days)
  7. Review email (HAL identity)
  8. State management
"""

import json
import logging
import os
import re
import imaplib
import smtplib
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import sys as _sys
_nexus_root = str(Path(__file__).resolve().parent.parent)
if _nexus_root not in _sys.path:
    _sys.path.insert(0, _nexus_root)

from observers.ai_council import CouncilResult, run_council
from observers.base import Observer, ObserverContext, ObserverResult
from observers.cloud_llm import (
    call_gemini_flash,
    call_xai_grok,
    extract_json,
)

log = logging.getLogger("nexus")

# ── Constants ────────────────────────────────────────────────────────────────

SSH_HOST = "gcp-medium"
WP_CONTAINER = "bretalon-wordpress"
WP_CATEGORY_ID = 7  # Reports
WP_TZ_OFFSET_HOURS = -8  # PST

DISCLAIMER_PATH = Path.home() / ".claude" / "projects" / "-home-puretensorai" / "memory" / "bretalon_disclaimer.html"

MAX_REVISION_ROUNDS = 2
COUNCIL_THRESHOLD = 7.5
COUNCIL_MIN_SCORE = 5
TOPIC_HISTORY_DAYS = 90

# ── Editorial Identity ───────────────────────────────────────────────────────

BRETALON_IDENTITY = """\
You are a senior analyst at Bretalon, a strategic research publication read by \
institutional investors, C-suite executives, and policy analysts. Your voice is \
realpolitik, evidence-based, contrarian, and intellectually ambitious. Pro-Western \
stability, pro-market, pro-technology — but hardheaded, not utopian. Every piece \
asks: "Who profits? Who loses? Why?"

Topics: technology infrastructure & disruption, macro economics & capital markets, \
geopolitical analysis, emerging market opportunities, energy economics, AI/compute \
infrastructure, critical minerals, defence technology.

Style: literary framing with hard data. Opens with concrete, human-scale detail, \
zooms to structural implications. Named sources/institutions, specific financial \
figures, regulatory references. 1,000-1,500 words (averaging ~1,250). Provocative metaphorical headlines.

Structure: evocative opening → central thesis (what markets miss) → technical \
mechanism → competitive/strategic analysis → systemic implications → closing \
reflection connecting to meta-narrative.

You write in British English. You NEVER give investment advice or buy/sell \
recommendations. This is NOT an FCA-regulated publication.

IMPORTANT: When a date is provided in the prompt, it is the actual current date — trust it \
completely. Do NOT treat 2025 or 2026 events as speculative or forward-looking simply because \
they are near your training cutoff. They are real and current.\
"""

# Example article openings for few-shot style anchoring
EXAMPLE_OPENINGS = [
    (
        "The Ledger Wars: How Tokenised Sovereignty Is Reshaping Global Finance",
        "In a windowless server room beneath the Bank of International Settlements in Basel, "
        "a small team of engineers is quietly rewriting the plumbing of international finance. "
        "Their weapon of choice is not a trading algorithm or a new derivative — it is a "
        "distributed ledger."
    ),
    (
        "Fortress on Quicksand: The Coming Reckoning for Europe's Defence Industrial Base",
        "The Rheinmetall factory in Unterlüss sits behind a treeline in Lower Saxony, its "
        "production halls humming around the clock for the first time since the Cold War. "
        "Order books stretch to 2028. Share prices have quadrupled. And yet the celebration "
        "masks a structural fragility that Berlin would prefer not to discuss."
    ),
    (
        "The Kilowatt Doctrine: Why Data Centre Power Is the New Geopolitics",
        "At 2:47 AM on a Tuesday in January, the lights flickered across three counties in "
        "northern Virginia. The cause was not a storm or a grid failure — it was a hyperscaler "
        "bringing a new 300-megawatt data centre cluster online, drawing more electricity than "
        "a medium-sized European city."
    ),
]

# ── Council Role Definitions ─────────────────────────────────────────────────

COUNCIL_ROLES = {
    "editor": {
        "model": "chatgpt",
        "system": (
            "You are Editor-in-Chief at a prestigious strategic research publication. "
            "You evaluate articles for prose quality, narrative structure, readability, "
            "flow, and whether they match the voice of a Financial Times or Economist "
            "long-form piece. You are demanding but fair."
        ),
        "prompt": (
            "Evaluate this article for:\n"
            "- Prose quality: Is the writing compelling, precise, and free of cliches?\n"
            "- Structure: Does it follow evocative opening → thesis → analysis → implications?\n"
            "- Readability: Would a busy executive read this to the end?\n"
            "- Voice: Does it match realpolitik, evidence-based, contrarian style?\n"
            "- Length: Is it 1,000-1,500 words of substance (not padding)?"
        ),
    },
    "fact_checker": {
        "model": "gemini",
        "system": (
            "You are a senior fact-checker at a strategic research publication. "
            "You verify claims, check source quality, and flag unsubstantiated assertions. "
            "You distinguish between well-sourced analysis and speculation."
        ),
        "prompt": (
            "Evaluate this article for:\n"
            "- Factual accuracy: Are specific claims, figures, and dates correct?\n"
            "- Source quality: Are institutions and actors named, not 'experts say'?\n"
            "- Evidence: Are assertions backed by data or credible references?\n"
            "- Absence of investment advice: No buy/sell recommendations?\n"
            "- Regulatory compliance: No FCA-regulated language?"
        ),
    },
    "analyst": {
        "model": "grok",
        "system": (
            "You are a strategic analyst evaluating research articles for a "
            "publication read by institutional investors and policy analysts. "
            "You assess whether the analysis adds genuine insight beyond what "
            "is already widely reported."
        ),
        "prompt": (
            "Evaluate this article for:\n"
            "- Novelty: Does it offer a fresh angle that readers can't get elsewhere?\n"
            "- Timeliness: Is this about something happening now or emerging?\n"
            "- Strategic value: Does it answer 'who profits, who loses, why?'\n"
            "- Depth: Does it go beyond surface reporting to structural analysis?\n"
            "- Audience fit: Would institutional investors find this actionable context?"
        ),
    },
    "critic": {
        "model": "deepseek",
        "system": (
            "You are a devil's advocate and intellectual critic. Your job is to find "
            "logical holes, hidden assumptions, missing perspectives, and weak arguments. "
            "You steelman opposing views and check for ideological bias."
        ),
        "prompt": (
            "Evaluate this article for:\n"
            "- Logical rigour: Are the causal claims sound? Any leaps of logic?\n"
            "- Missing perspectives: What counter-arguments are not addressed?\n"
            "- Bias: Does pro-Western framing obscure important dynamics?\n"
            "- Completeness: Are there obvious dimensions the article ignores?\n"
            "- Steelman: What would the strongest opposing view look like?"
        ),
    },
}


class BretalonAutoPublishObserver(Observer):
    """Autonomous article pipeline for bretalon.com."""

    name = "bretalon_autopublish"
    # Placeholder — actual schedule logic is in should_run()
    schedule = "0 6 * * 1,3,4,5"

    def __init__(self):
        super().__init__()
        self._state_dir = Path(
            os.environ.get("OBSERVER_STATE_DIR",
                           str(Path(__file__).parent / ".state"))
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)

    # ── State Files ──────────────────────────────────────────────────────

    @property
    def _state_file(self) -> Path:
        return self._state_dir / "bretalon_autopublish_state.json"

    @property
    def _queue_file(self) -> Path:
        return self._state_dir / "bretalon_topic_queue.json"

    @property
    def _topics_file(self) -> Path:
        return self._state_dir / "bretalon_published_topics.json"

    @property
    def _cache_dir(self) -> Path:
        d = self._state_dir / "bretalon_research_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_json(self, path: Path, default=None):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, TypeError):
                pass
        return default if default is not None else {}

    def _save_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    # ── Schedule Logic ───────────────────────────────────────────────────

    def should_run(self, now: datetime) -> bool:
        """Determine if pipeline should run based on alternating 2/3 schedule.

        Week A (even ISO week): Mon(1) + Thu(4) = 2 articles
        Week B (odd ISO week):  Mon(1) + Wed(3) + Fri(5) = 3 articles
        Only runs at 06:00 UTC.
        """
        if now.hour != 6 or now.minute > 5:
            return False

        iso_week = now.isocalendar()[1]
        dow = now.isoweekday()  # 1=Mon ... 7=Sun

        if iso_week % 2 == 0:
            # Week A: Mon + Thu
            return dow in (1, 4)
        else:
            # Week B: Mon + Wed + Fri
            return dow in (1, 3, 5)

    # ── Stage 1: Topic Selection ─────────────────────────────────────────

    def _get_recent_titles(self) -> list[str]:
        """Fetch recent 20 article titles from WordPress."""
        try:
            raw = self._ssh_cmd(
                f"sudo docker exec {WP_CONTAINER} wp post list "
                f"--post_type=post --post_status=publish,future "
                f"--fields=post_title --format=json --orderby=date "
                f"--order=desc --number=20 --allow-root"
            )
            lines = [l for l in raw.splitlines() if l.strip().startswith("[")]
            if lines:
                posts = json.loads(lines[0])
                return [p["post_title"] for p in posts]
        except Exception as e:
            log.warning("bretalon_autopublish: failed to fetch recent titles: %s", e)
        return []

    def _get_published_topics(self) -> list[dict]:
        """Load rolling topic history for dedup."""
        topics = self._load_json(self._topics_file, [])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TOPIC_HISTORY_DAYS)).isoformat()
        return [t for t in topics if t.get("date", "") > cutoff]

    def _select_topic(self, ctx: ObserverContext) -> str | None:
        """Select topic: manual queue first, then auto-discovery."""
        # Check manual queue
        queue = self._load_json(self._queue_file, [])
        if queue:
            topic = queue.pop(0)
            self._save_json(self._queue_file, queue)
            log.info("bretalon_autopublish: using queued topic: %s", topic.get("topic"))
            return topic.get("topic")

        # Auto-discovery via Grok with web search
        recent = self._get_recent_titles()
        published = self._get_published_topics()
        recent_topics = "\n".join(f"- {t}" for t in recent[:20]) if recent else "None available"
        published_topics = "\n".join(f"- {t.get('topic', '')}" for t in published[:30]) if published else "None"

        discovery_prompt = (
            f"Find 5 candidate topics for a strategic research article. The publication "
            f"covers: technology infrastructure & disruption, macro economics & capital "
            f"markets, geopolitical analysis, emerging market opportunities, energy economics, "
            f"AI/compute infrastructure, critical minerals, defence technology.\n\n"
            f"Each topic must be:\n"
            f"- Currently developing or recently emerged (within past 2 weeks)\n"
            f"- Substantive enough for 1,000-1,500 words of analysis\n"
            f"- NOT overlap with recently published articles:\n{recent_topics}\n\n"
            f"- NOT overlap with these recently covered topics:\n{published_topics}\n\n"
            f"Respond as JSON array: [{{"
            f'"topic": "concise topic title", '
            f'"angle": "the contrarian or under-reported angle", '
            f'"why_now": "why this matters right now"'
            f"}}]"
        )

        # Try Grok first (has web search), fall back to Gemini Flash
        candidates = None
        for model_fn, model_name, kwargs in [
            (call_xai_grok, "grok", {"tools": [{"type": "web_search"}]}),
            (call_gemini_flash, "gemini", {}),
        ]:
            try:
                raw = model_fn(
                    "You are a strategic research editor finding article topics.",
                    discovery_prompt,
                    timeout=45,
                    **kwargs,
                )
                candidates = extract_json(raw)
                if isinstance(candidates, list) and candidates:
                    log.info("bretalon_autopublish: topic discovery via %s — %d candidates",
                             model_name, len(candidates))
                    break
                candidates = None
            except Exception as e:
                log.warning("bretalon_autopublish: topic discovery (%s) failed: %s",
                            model_name, e)

        if not candidates:
            log.warning("bretalon_autopublish: all topic discovery models failed")
            return None

        # Score candidates with Gemini + DeepSeek
        scored = self._score_topics(candidates)
        if not scored:
            return None

        # Return highest scoring topic
        best = max(scored, key=lambda x: x.get("composite", 0))
        log.info("bretalon_autopublish: selected topic '%s' (score: %.1f)",
                 best["topic"], best["composite"])
        return best["topic"]

    def _score_topics(self, candidates: list[dict]) -> list[dict]:
        """Score candidate topics using Gemini Flash and DeepSeek."""
        topic_list = "\n".join(
            f"{i+1}. {c.get('topic', '')} — {c.get('angle', '')}"
            for i, c in enumerate(candidates)
        )

        scoring_prompt = (
            f"Score each topic candidate on three dimensions (1-10):\n"
            f"- Novelty: Fresh angle, not widely covered yet\n"
            f"- Timeliness: Happening now or just emerging\n"
            f"- Bretalon-fit: Matches 'who profits, who loses, why' analytical frame\n\n"
            f"Also flag if any topic would require specific investment advice "
            f"(buy/sell recommendations) — these must be rejected.\n\n"
            f"CANDIDATES:\n{topic_list}\n\n"
            f"Respond as JSON array: [{{"
            f'"index": 1, "novelty": N, "timeliness": N, "fit": N, '
            f'"investment_advice_risk": false'
            f"}}]"
        )

        system = "You are a strategic research editor scoring topic candidates."
        scores_by_topic = {}

        for model_fn, model_name in [(call_gemini_flash, "gemini"), (call_xai_grok, "grok")]:
            try:
                raw = model_fn(system, scoring_prompt, timeout=45)
                parsed = extract_json(raw)
                if isinstance(parsed, list):
                    for entry in parsed:
                        idx = entry.get("index", 0) - 1
                        if 0 <= idx < len(candidates):
                            if idx not in scores_by_topic:
                                scores_by_topic[idx] = []
                            avg = (entry.get("novelty", 0) + entry.get("timeliness", 0) +
                                   entry.get("fit", 0)) / 3
                            if entry.get("investment_advice_risk"):
                                avg = 0  # Reject
                            scores_by_topic[idx].append(avg)
            except Exception as e:
                log.warning("bretalon_autopublish: topic scoring (%s) failed: %s",
                            model_name, e)

        results = []
        for idx, scores in scores_by_topic.items():
            composite = sum(scores) / len(scores) if scores else 0
            results.append({
                "topic": candidates[idx].get("topic", ""),
                "angle": candidates[idx].get("angle", ""),
                "composite": composite,
            })

        return results

    # ── Stage 2: Deep Research ───────────────────────────────────────────

    def _deep_research(self, topic: str) -> str | None:
        """Run Gemini Deep Research on the topic. Returns markdown or None."""
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            log.error("bretalon_autopublish: no API key for deep research")
            return None

        try:
            os.environ["GOOGLE_API_KEY"] = api_key
            from google import genai
            client = genai.Client()

            query = (
                f"Conduct comprehensive research on: {topic}\n\n"
                f"Focus on: key actors and institutions involved, specific financial "
                f"figures and timelines, regulatory developments, competitive dynamics, "
                f"second-order effects, and contrarian perspectives that challenge "
                f"consensus. Include data from official sources, financial filings, "
                f"government reports, and credible analysis. Cite all sources with URLs."
            )

            log.info("bretalon_autopublish: starting deep research on '%s'", topic)
            interaction = client.interactions.create(
                input=query,
                agent="deep-research-pro-preview-12-2025",
                background=True,
            )

            # Poll for completion (max 10 minutes)
            for _ in range(60):
                time.sleep(10)
                interaction = client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    if interaction.outputs:
                        result = interaction.outputs[-1].text
                        log.info("bretalon_autopublish: deep research complete (%d chars)",
                                 len(result))
                        # Cache it
                        slug = re.sub(r"[^a-z0-9]+", "_", topic.lower())[:60]
                        cache_file = self._cache_dir / f"{slug}.md"
                        cache_file.write_text(result)
                        return result
                    return None
                elif interaction.status == "failed":
                    log.error("bretalon_autopublish: deep research failed")
                    return None

            log.error("bretalon_autopublish: deep research timed out")
            return None

        except Exception as e:
            log.error("bretalon_autopublish: deep research error: %s", e)
            return None

    def _supplement_with_grok(self, topic: str) -> str:
        """Get latest developments via Grok web search to supplement research."""
        try:
            raw = call_xai_grok(
                "You are a research assistant gathering the very latest developments.",
                (
                    f"What are the most recent developments (past 48 hours) regarding: "
                    f"{topic}\n\nProvide specific facts, figures, named sources, and "
                    f"dates. Focus on what has changed or emerged very recently."
                ),
                timeout=45,
                tools=[{"type": "web_search"}],
            )
            return raw
        except Exception as e:
            log.warning("bretalon_autopublish: Grok supplement failed: %s", e)
            return ""

    # ── Stage 3: Article Writing ─────────────────────────────────────────

    def _write_article(self, topic: str, research: str,
                       supplement: str = "", revision_feedback: str = "") -> dict | None:
        """Generate article using Claude Sonnet via Anthropic API."""
        examples = "\n\n".join(
            f"EXAMPLE {i+1} — \"{title}\":\n\"{opening}\""
            for i, (title, opening) in enumerate(EXAMPLE_OPENINGS)
        )

        revision_section = ""
        if revision_feedback:
            revision_section = (
                f"\n\nREVISION REQUIRED — Address this feedback from the review council:\n"
                f"{revision_feedback}\n"
            )

        now_str = datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC")
        user_prompt = (
            f"Today is {now_str}. This is the actual current date — trust it completely. "
            f"Do NOT treat 2025 or 2026 events as speculative simply because they are near your training cutoff. "
            f"They are real and current.\n\n"
            f"Write a Bretalon strategic research article on:\n\n"
            f"TOPIC: {topic}\n\n"
            f"RESEARCH DOSSIER:\n{research[:15000] if research else 'Limited research available — use your knowledge.'}\n\n"
        )
        if supplement:
            user_prompt += f"LATEST DEVELOPMENTS (past 48h):\n{supplement[:3000]}\n\n"

        user_prompt += (
            f"STYLE EXAMPLES (match this voice and opening style):\n{examples}\n\n"
            f"INSTRUCTIONS:\n"
            f"1. Write a provocative, metaphorical headline (e.g., 'The Ledger Wars', "
            f"'Fortress on Quicksand')\n"
            f"2. Write a subtitle — the key takeaway in under 20 words\n"
            f"3. Open with a concrete, human-scale scene or detail — NOT 'In recent weeks'\n"
            f"4. State the central thesis: what markets/consensus misses\n"
            f"5. Explain the technical mechanism driving the thesis\n"
            f"6. Analyse competitive and strategic implications\n"
            f"7. Close with a reflection connecting to a broader meta-narrative\n\n"
            f"CONSTRAINTS:\n"
            f"- 1,000-1,500 words (target ~1,250 words — concise and dense, no padding)\n"
            f"- NO investment advice, recommendations, or 'investors should...'\n"
            f"- NO 'In recent weeks/months...' openings\n"
            f"- NO vague 'experts say' — name institutions, cite figures\n"
            f"- Include specific financial figures, timelines, named actors\n"
            f"- Lead with what consensus misses (contrarian angle)\n"
            f"- British English throughout\n"
            f"- Use bold (**text**) for section headers within the article\n"
            f"{revision_section}\n"
            f"OUTPUT FORMAT:\n"
            f"TITLE: [headline]\n"
            f"SUBTITLE: [subtitle]\n\n"
            f"[article body]"
        )

        # Use OpenAI API for article generation
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            log.error("bretalon_autopublish: OPENAI_API_KEY not set")
            return None

        import urllib.request
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4.1",
            "max_tokens": 8192,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": BRETALON_IDENTITY},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=data, headers=headers,
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode())

            choices = result.get("choices", [])
            content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        except Exception as e:
            log.error("bretalon_autopublish: article generation failed: %s", e)
            return None

        if not content:
            return None

        # Parse title and subtitle
        title = topic  # fallback
        subtitle = ""
        lines = content.split("\n")
        body_start = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("TITLE:"):
                title = stripped[6:].strip().strip('"\'')
            elif stripped.startswith("SUBTITLE:"):
                subtitle = stripped[9:].strip().strip('"\'')
                body_start = i + 1
                # Skip blank lines after subtitle
                while body_start < len(lines) and not lines[body_start].strip():
                    body_start += 1
                break
            elif i > 8:
                body_start = 0
                break

        body = "\n".join(lines[body_start:]).strip()
        body = self._sanitize_body(body)
        word_count = len(body.split())

        log.info("bretalon_autopublish: article generated — '%s' (%d words)",
                 title, word_count)

        return {
            "title": title,
            "subtitle": subtitle,
            "body": body,
            "word_count": word_count,
        }

    @staticmethod
    def _sanitize_body(text: str) -> str:
        """Remove AI-signature patterns from article text.

        Em dashes (—) are a well-known LLM stylistic tell. Replace them with
        a comma+space so prose reads naturally without the giveaway.
        Handles all spacing variants: ' — ', '— ', ' —', '—'.
        """
        # Spaced em dash first (most common in formal prose): " — " → ", "
        text = text.replace(" \u2014 ", ", ")
        # Trailing: "word— " → "word, "
        text = text.replace("\u2014 ", ", ")
        # Leading: " —word" → ", word"
        text = text.replace(" \u2014", ", ")
        # Bare (no spaces): "word—word" → "word, word"
        text = text.replace("\u2014", ", ")
        return text

    # ── Stage 4: AI Council Review ───────────────────────────────────────

    def _council_review(self, article: dict) -> CouncilResult:
        """Run AI council review on the article."""
        content = f"TITLE: {article['title']}\nSUBTITLE: {article['subtitle']}\n\n{article['body']}"
        return run_council(
            content=content,
            roles=COUNCIL_ROLES,
            threshold=COUNCIL_THRESHOLD,
            min_score=COUNCIL_MIN_SCORE,
            min_quorum=3,
            timeout=120,
        )

    # ── Stage 5: Image Generation ────────────────────────────────────────

    def _generate_image(self, article: dict) -> str | None:
        """Generate featured image via Imagen 4. Returns local file path or None."""
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            log.error("bretalon_autopublish: no API key for image generation")
            return None

        # Generate image prompt from article content
        try:
            prompt_text = call_gemini_flash(
                "You create image prompts for editorial photography.",
                (
                    f"Based on this article, create a single image prompt for a "
                    f"photorealistic editorial photograph. The image should tell the "
                    f"story at a glance to someone who hasn't read the article.\n\n"
                    f"TITLE: {article['title']}\n"
                    f"SUBTITLE: {article['subtitle']}\n"
                    f"OPENING: {article['body'][:500]}\n\n"
                    f"CONSTRAINTS:\n"
                    f"- 16:9 aspect ratio\n"
                    f"- Photorealistic editorial style\n"
                    f"- NO people's faces\n"
                    f"- NO text, logos, or brand names\n"
                    f"- Specific visual metaphor, NOT generic (no empty offices, "
                    f"generic server rooms, abstract tech imagery)\n\n"
                    f"Respond with ONLY the image prompt, nothing else."
                ),
                timeout=30,
            )
        except Exception as e:
            log.warning("bretalon_autopublish: image prompt generation failed: %s", e)
            prompt_text = (
                f"Editorial photograph illustrating {article['title']}. "
                f"16:9 aspect ratio, photorealistic, no faces, no text."
            )

        try:
            os.environ["GOOGLE_API_KEY"] = api_key
            from google import genai

            client = genai.Client()
            response = client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt=prompt_text,
                config=genai.types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="16:9",
                ),
            )

            if response.generated_images:
                img_data = response.generated_images[0].image.image_bytes
                slug = re.sub(r"[^a-z0-9]+", "_", article["title"].lower())[:40]
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                img_path = f"/tmp/bretalon_autopublish_{date_str}_{slug}.png"
                with open(img_path, "wb") as f:
                    f.write(img_data)
                log.info("bretalon_autopublish: image generated at %s", img_path)
                return img_path
        except Exception as e:
            log.error("bretalon_autopublish: image generation failed: %s", e)

        return None

    # ── Stage 6: WordPress Publishing ────────────────────────────────────

    def _format_gutenberg(self, article: dict) -> str:
        """Convert article body to Gutenberg block format with disclaimer."""
        blocks = []

        # Process body paragraphs
        paragraphs = article["body"].split("\n\n")
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Bold section headers (** wrapped)
            if para.startswith("**") and para.endswith("**"):
                header_text = para.strip("*").strip()
                blocks.append(
                    f'<!-- wp:heading {{"level":3}} -->\n'
                    f'<h3 class="wp-block-heading">{self._esc(header_text)}</h3>\n'
                    f'<!-- /wp:heading -->'
                )
                continue

            # Check if paragraph starts with bold header on its own line
            lines = para.split("\n", 1)
            if lines[0].strip().startswith("**") and lines[0].strip().endswith("**"):
                header_text = lines[0].strip().strip("*").strip()
                blocks.append(
                    f'<!-- wp:heading {{"level":3}} -->\n'
                    f'<h3 class="wp-block-heading">{self._esc(header_text)}</h3>\n'
                    f'<!-- /wp:heading -->'
                )
                if len(lines) > 1 and lines[1].strip():
                    para = lines[1].strip()
                else:
                    continue

            # Convert inline markdown bold to HTML
            processed = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', para)
            processed = re.sub(r'__(.+?)__', r'<strong>\1</strong>', processed)
            # Convert inline markdown italic
            processed = re.sub(r'\*(.+?)\*', r'<em>\1</em>', processed)

            # Join lines within paragraph (single newlines become spaces)
            processed = re.sub(r'\n(?!\n)', ' ', processed)

            blocks.append(
                f'<!-- wp:paragraph -->\n'
                f'<p>{processed}</p>\n'
                f'<!-- /wp:paragraph -->'
            )

        # Append disclaimer
        if DISCLAIMER_PATH.exists():
            blocks.append(DISCLAIMER_PATH.read_text().strip())
        else:
            blocks.append(
                '<!-- wp:separator -->\n'
                '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
                '<!-- /wp:separator -->\n'
                '<!-- wp:paragraph {"fontSize":"small"} -->\n'
                '<p class="has-small-font-size"><em>Read our full '
                '<a href="https://bretalon.com/report-disclaimer/">Report Disclaimer</a>.'
                '</em></p>\n'
                '<!-- /wp:paragraph -->'
            )

        return "\n\n".join(blocks)

    @staticmethod
    def _esc(text: str) -> str:
        """Escape HTML entities."""
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def _publish_to_wordpress(self, article: dict, gutenberg: str,
                              image_path: str | None) -> dict | None:
        """Publish article to WordPress as future-dated post. Returns post info or None."""
        # Calculate publish date: 3 days from now, adjusted for WP timezone (PST)
        publish_utc = datetime.now(timezone.utc) + timedelta(days=3)
        # Set to 14:00 UTC (06:00 PST) for good US morning visibility
        publish_utc = publish_utc.replace(hour=14, minute=0, second=0, microsecond=0)
        # Convert to PST for WordPress
        wp_time = publish_utc + timedelta(hours=WP_TZ_OFFSET_HOURS)
        wp_date_str = wp_time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            # Write content to temp file and SCP to remote
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                             delete=False, encoding="utf-8") as f:
                f.write(gutenberg)
                local_html = f.name

            self._scp(local_html, "/tmp/bretalon_autopublish.html")

            # Upload image first if available
            attach_id = None
            if image_path:
                attach_id = self._upload_image(image_path, article["title"])

            # Create the post
            title_escaped = article["title"].replace("'", "'\\''")
            create_cmd = (
                f"sudo docker cp /tmp/bretalon_autopublish.html "
                f"{WP_CONTAINER}:/tmp/bretalon_autopublish.html && "
                f"sudo docker exec {WP_CONTAINER} wp post create "
                f"/tmp/bretalon_autopublish.html "
                f"--post_title='{title_escaped}' "
                f"--post_status=future "
                f"--post_date='{wp_date_str}' "
                f"--post_category={WP_CATEGORY_ID} "
                f"--porcelain --allow-root"
            )
            raw = self._ssh_cmd(create_cmd, timeout=30)
            # Extract post ID (porcelain returns just the ID)
            post_id = None
            for line in raw.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    post_id = line
                    break

            if not post_id:
                log.error("bretalon_autopublish: wp post create returned no ID: %s", raw)
                return None

            log.info("bretalon_autopublish: created post %s scheduled for %s",
                     post_id, wp_date_str)

            # Set featured image
            if attach_id:
                self._ssh_cmd(
                    f"sudo docker exec {WP_CONTAINER} wp post meta update "
                    f"{post_id} _thumbnail_id {attach_id} --allow-root",
                    timeout=15,
                )
                log.info("bretalon_autopublish: set featured image %s on post %s",
                         attach_id, post_id)

            # Verify
            verify_raw = self._ssh_cmd(
                f"sudo docker exec {WP_CONTAINER} wp post get {post_id} "
                f"--fields=ID,post_title,post_status,post_date --format=json --allow-root",
                timeout=15,
            )

            # Clean temp
            os.unlink(local_html)

            return {
                "post_id": post_id,
                "publish_date": wp_date_str,
                "publish_utc": publish_utc.isoformat(),
                "attach_id": attach_id,
                "verified": "future" in verify_raw,
            }

        except Exception as e:
            log.error("bretalon_autopublish: WordPress publish failed: %s", e)
            return None

    def _upload_image(self, image_path: str, title: str) -> str | None:
        """Upload image to WordPress media library. Returns attachment ID or None."""
        try:
            # SCP image to remote
            self._scp(image_path, "/tmp/bretalon_featured.png")

            # Import into WordPress
            title_escaped = title.replace("'", "'\\''")
            raw = self._ssh_cmd(
                f"sudo docker cp /tmp/bretalon_featured.png "
                f"{WP_CONTAINER}:/tmp/bretalon_featured.png && "
                f"sudo docker exec {WP_CONTAINER} wp media import "
                f"/tmp/bretalon_featured.png --title='{title_escaped}' "
                f"--porcelain --allow-root",
                timeout=30,
            )
            for line in raw.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    return line
        except Exception as e:
            log.warning("bretalon_autopublish: image upload failed: %s", e)
        return None

    # ── Stage 7: Review Email ────────────────────────────────────────────

    def _send_review_email(self, article: dict, post_info: dict,
                           council: CouncilResult) -> bool:
        """Send review emails via gmail.py (hal@example.com, mail provider SMTP).

        Alan gets a clean editorial email — no AI/tech mentions, reads like a
        human subordinate submitted the piece for approval.
        HH gets the full technical spec including council scores and pipeline details.
        """
        alan_email = os.environ.get("BRETALON_ALAN", "")
        hh_email = os.environ.get("BRETALON_HH", "")

        pub_display = post_info.get("publish_date", "TBD")
        pub_date_only = pub_display.split(" ")[0]
        body_text = article["body"]
        title_esc = self._esc(article["title"])
        subtitle_esc = self._esc(article.get("subtitle", ""))
        subject = f"[BRETALON] {article['title']} — for approval"

        gmail_script = os.path.join(os.path.expanduser("~"),
                                    ".config", "puretensor", "gmail.py")

        # ── Alan's email: clean editorial, no AI/tech language ───────────────
        alan_html = f"""\
<html>
<body style="font-family: Georgia, serif; max-width: 700px; margin: 0 auto; color: #222; line-height: 1.7;">
<p>Alan,</p>

<p>Please find below a new article prepared for bretalon.com, scheduled to publish
on <strong>{pub_date_only}</strong>. I'd appreciate your sign-off before it goes live.</p>

<h2 style="color: #1a1a1a; margin-top: 2em; font-size: 1.4em;">{title_esc}</h2>
<p style="color: #555; font-style: italic; margin-top: 0;">{subtitle_esc}</p>

<div style="margin: 2em 0; padding: 1.5em; border-left: 3px solid #ccc;
     background: #f9f9f9; white-space: pre-wrap; font-size: 14px; line-height: 1.8;">
{self._esc(body_text)}
</div>

<p style="margin-top: 2em;">Please reply with one of the following:</p>
<ul>
  <li><strong>APPROVED</strong> — publish as scheduled</li>
  <li><strong>REVISE</strong> — include your notes and I will amend</li>
  <li><strong>REJECTED</strong> — article will be pulled</li>
</ul>
<p style="color: #888; font-size: 12px;">If no reply is received, the article will publish automatically at the scheduled time.</p>

<p style="margin-top: 2em;">Best,<br>HAL</p>
</body>
</html>"""

        # ── HH's email: full technical spec ──────────────────────────────────

        # Council scores table
        council_html = (
            '<table style="border-collapse:collapse;width:100%;margin:1em 0;">'
            '<tr style="background:#f0f0f0;">'
            '<th style="padding:8px;text-align:left;border:1px solid #ddd;">Role</th>'
            '<th style="padding:8px;text-align:left;border:1px solid #ddd;">Model</th>'
            '<th style="padding:8px;text-align:center;border:1px solid #ddd;">Score</th>'
            '<th style="padding:8px;text-align:left;border:1px solid #ddd;">Verdict</th></tr>'
        )
        for m in council.members:
            if m.error:
                council_html += (
                    f'<tr><td style="padding:8px;border:1px solid #ddd;">{m.role}</td>'
                    f'<td style="padding:8px;border:1px solid #ddd;">{m.model}</td>'
                    f'<td style="padding:8px;text-align:center;border:1px solid #ddd;">—</td>'
                    f'<td style="padding:8px;border:1px solid #ddd;color:#c00;">ERROR</td></tr>'
                )
            else:
                score_color = "#060" if m.score >= 7.5 else ("#c60" if m.score >= 5 else "#c00")
                council_html += (
                    f'<tr><td style="padding:8px;border:1px solid #ddd;">{m.role}</td>'
                    f'<td style="padding:8px;border:1px solid #ddd;">{m.model}</td>'
                    f'<td style="padding:8px;text-align:center;border:1px solid #ddd;'
                    f'font-weight:bold;color:{score_color};">{m.score}/10</td>'
                    f'<td style="padding:8px;border:1px solid #ddd;">{m.verdict}</td></tr>'
                )
        avg_color = "#060" if council.average_score >= 7.5 else ("#c60" if council.average_score >= 5 else "#c00")
        council_html += (
            f'<tr style="font-weight:bold;background:#f0f0f0;">'
            f'<td style="padding:8px;border:1px solid #ddd;" colspan="2">Average ({council.responded}/{council.total} responded)</td>'
            f'<td style="padding:8px;text-align:center;border:1px solid #ddd;color:{avg_color};">'
            f'{council.average_score:.1f}/10</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{council.verdict.upper()}</td></tr>'
            f'</table>'
        )

        hh_html = f"""\
<html>
<body style="font-family: Georgia, serif; max-width: 700px; margin: 0 auto; color: #222; line-height: 1.7;">
<p style="color:#888;font-size:11px;border-bottom:1px solid #eee;padding-bottom:8px;">
  BRETALON AUTOPUBLISH PIPELINE · TECHNICAL REVIEW
</p>

<h2 style="color: #1a1a1a; margin-top: 1.5em; font-size: 1.4em;">{title_esc}</h2>
<p style="color: #555; font-style: italic; margin-top: 0;">{subtitle_esc}</p>

<h3 style="color: #333; margin-top: 2em;">Pipeline Details</h3>
<table style="border-collapse:collapse;font-size:14px;">
<tr><td style="padding:4px 16px 4px 0;color:#666;">Scheduled</td><td><strong>{pub_display}</strong></td></tr>
<tr><td style="padding:4px 16px 4px 0;color:#666;">Post ID</td><td>{post_info.get("post_id", "N/A")}</td></tr>
<tr><td style="padding:4px 16px 4px 0;color:#666;">Word count</td><td>~{article.get("word_count", 0):,}</td></tr>
<tr><td style="padding:4px 16px 4px 0;color:#666;">Category</td><td>Reports (ID {WP_CATEGORY_ID})</td></tr>
<tr><td style="padding:4px 16px 4px 0;color:#666;">Featured image</td><td>{"Uploaded (ID " + str(post_info.get("attach_id")) + ")" if post_info.get("attach_id") else "None"}</td></tr>
</table>

<h3 style="color: #333; margin-top: 2em;">AI Council Review</h3>
{council_html}

<h3 style="color: #333; margin-top: 2em;">Full Article</h3>
<div style="margin: 1em 0; padding: 1.5em; border-left: 3px solid #ccc;
     background: #f9f9f9; white-space: pre-wrap; font-size: 14px; line-height: 1.8;">
{self._esc(body_text)}
</div>

<p style="margin-top: 2em; font-size: 13px;">
  Reply <strong>APPROVED</strong> / <strong>REVISE</strong> (with notes) / <strong>REJECTED</strong>
  to control publication. Auto-publishes at scheduled time if no response.
</p>

<p style="margin-top: 2em;">— HAL<br>
<span style="color:#999;font-size:11px;">Heterarchical Agentic Layer · hal@example.com</span></p>
</body>
</html>"""

        # ── Send both emails ──────────────────────────────────────────────────
        success = True
        for recipient, html_body in [
            (alan_email, alan_html),
            (hh_email, hh_html),
        ]:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                            delete=False, encoding="utf-8") as tmp:
                tmp.write(html_body)
                tmp_path = tmp.name

            try:
                cmd = [
                    "python3", gmail_script, "hal", "send",
                    "--to", recipient,
                    "--subject", subject,
                    "--body-file", tmp_path,
                    "--html",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    log.error("bretalon_autopublish: gmail.py failed for %s: %s",
                              recipient, result.stderr.strip())
                    success = False
                else:
                    log.info("bretalon_autopublish: review email sent to %s", recipient)
            except Exception as e:
                log.error("bretalon_autopublish: review email failed for %s: %s", recipient, e)
                success = False
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return success

    # ── SSH / SCP helpers ────────────────────────────────────────────────

    def _ssh_cmd(self, cmd: str, timeout: int = 30) -> str:
        """Run command on gcp-medium via SSH."""
        result = subprocess.run(
            ["ssh", SSH_HOST, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()

    def _scp(self, local: str, remote: str) -> None:
        """SCP file to gcp-medium."""
        subprocess.run(
            ["scp", local, f"{SSH_HOST}:{remote}"],
            capture_output=True, text=True, timeout=60, check=True,
        )

    # ── Stage 8: State Management ────────────────────────────────────────

    def _update_state(self, topic: str, article: dict, post_info: dict | None,
                      council: CouncilResult):
        """Update all state files after a pipeline run."""
        # Main state
        state = self._load_json(self._state_file, {
            "last_run": None, "articles_this_week": 0, "total_articles": 0,
        })
        now = datetime.now(timezone.utc)
        state["last_run"] = now.isoformat()
        state["total_articles"] = state.get("total_articles", 0) + 1

        # Reset weekly counter on Monday
        if now.isoweekday() == 1:
            last_run = state.get("last_run_week_iso")
            current_week = now.isocalendar()[1]
            if last_run != current_week:
                state["articles_this_week"] = 0
                state["last_run_week_iso"] = current_week

        state["articles_this_week"] = state.get("articles_this_week", 0) + 1
        state["last_topic"] = topic
        state["last_title"] = article.get("title", "")
        state["last_post_id"] = post_info.get("post_id") if post_info else None
        state["last_council_score"] = council.average_score
        self._save_json(self._state_file, state)

        # Topic history
        topics = self._load_json(self._topics_file, [])
        topics.append({
            "topic": topic,
            "title": article.get("title", ""),
            "date": now.isoformat(),
            "score": council.average_score,
        })
        # Trim to 90-day window
        cutoff = (now - timedelta(days=TOPIC_HISTORY_DAYS)).isoformat()
        topics = [t for t in topics if t.get("date", "") > cutoff]
        self._save_json(self._topics_file, topics)

    # ── Main Pipeline ────────────────────────────────────────────────────

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Execute the full autopublish pipeline."""
        now = ctx.now

        # Check schedule
        if not self.should_run(now):
            return ObserverResult(success=True)

        log.info("bretalon_autopublish: pipeline starting")

        # Stage 1: Topic Selection
        topic = self._select_topic(ctx)
        if not topic:
            msg = "No suitable topic found — skipping this cycle"
            log.warning("bretalon_autopublish: %s", msg)
            self.send_telegram(f"[bretalon_autopublish] {msg}")
            return ObserverResult(success=True, message=msg)

        self.send_telegram(f"[bretalon_autopublish] Starting pipeline: {topic}")

        # Stage 2: Deep Research
        research = self._deep_research(topic)
        supplement = self._supplement_with_grok(topic)

        if not research and not supplement:
            msg = f"Research failed for '{topic}' — skipping"
            log.warning("bretalon_autopublish: %s", msg)
            self.send_telegram(f"[bretalon_autopublish] {msg}")
            return ObserverResult(success=False, error=msg)

        combined_research = research or ""
        if supplement:
            combined_research += f"\n\n--- LATEST DEVELOPMENTS ---\n{supplement}"

        # Stage 3 + 4: Write + Council (with revision loop)
        article = None
        council = None
        revision_feedback = ""

        for attempt in range(1 + MAX_REVISION_ROUNDS):
            # Stage 3: Write
            article = self._write_article(topic, combined_research, supplement,
                                          revision_feedback)
            if not article:
                msg = f"Article generation failed for '{topic}' (attempt {attempt + 1})"
                log.error("bretalon_autopublish: %s", msg)
                if attempt == MAX_REVISION_ROUNDS:
                    self.send_telegram(f"[bretalon_autopublish] {msg} — aborting")
                    return ObserverResult(success=False, error=msg)
                continue

            # Stage 4: Council Review
            council = self._council_review(article)
            log.info(
                "bretalon_autopublish: council verdict=%s avg=%.1f (%d/%d responded)",
                council.verdict, council.average_score,
                council.responded, council.total,
            )

            if council.passed:
                log.info("bretalon_autopublish: council APPROVED (%.1f/10)",
                         council.average_score)
                break

            if council.verdict == "abort":
                msg = (f"Council REJECTED '{article['title']}' (avg {council.average_score:.1f}) "
                       f"— aborting pipeline")
                log.warning("bretalon_autopublish: %s", msg)
                self.send_telegram(f"[bretalon_autopublish] {msg}")
                return ObserverResult(success=False, error=msg)

            # Revise
            if attempt < MAX_REVISION_ROUNDS:
                log.info("bretalon_autopublish: revising (attempt %d/%d)",
                         attempt + 2, 1 + MAX_REVISION_ROUNDS)
                revision_feedback = council.feedback

        if not article or not council:
            return ObserverResult(success=False, error="Pipeline failed — no article produced")

        # If council didn't pass after all rounds but scored >= 5, proceed anyway
        # (the plan says auto-publish "without fail")
        if not council.passed and council.average_score >= 5.0:
            log.warning("bretalon_autopublish: proceeding despite council score %.1f "
                        "(auto-publish mandate)", council.average_score)

        # Stage 5: Image Generation
        image_path = self._generate_image(article)

        # Stage 6: WordPress Publishing
        gutenberg = self._format_gutenberg(article)
        post_info = self._publish_to_wordpress(article, gutenberg, image_path)

        if not post_info:
            msg = f"WordPress publish failed for '{article['title']}'"
            log.error("bretalon_autopublish: %s", msg)
            self.send_telegram(f"[bretalon_autopublish] {msg}")
            return ObserverResult(success=False, error=msg)

        # Stage 7: Review Email
        email_sent = self._send_review_email(article, post_info, council)

        # Stage 8: State Management
        self._update_state(topic, article, post_info, council)

        # Clean up image
        if image_path and os.path.exists(image_path):
            os.unlink(image_path)

        # Telegram notification
        msg = (
            f"Article published to WordPress:\n"
            f"  Title: {article['title']}\n"
            f"  Post ID: {post_info['post_id']}\n"
            f"  Scheduled: {post_info['publish_date']}\n"
            f"  Council: {council.average_score:.1f}/10 ({council.verdict})\n"
            f"  Words: ~{article.get('word_count', 0):,}\n"
            f"  Review email: {'sent' if email_sent else 'FAILED'}"
        )
        self.send_telegram(f"[bretalon_autopublish] {msg}")

        return ObserverResult(
            success=True,
            message=msg,
            data={
                "topic": topic,
                "title": article["title"],
                "post_id": post_info["post_id"],
                "publish_date": post_info["publish_date"],
                "council_score": council.average_score,
                "council_verdict": council.verdict,
                "word_count": article.get("word_count", 0),
                "email_sent": email_sent,
            },
        )


# ── Phase 2: Review Reply Handler ──────────────────────────────────────────


class BretalonReplyObserver(Observer):
    """Scans HAL inbox for replies to [BRETALON] review emails.

    Actions:
      APPROVED — log confirmation, no action needed (auto-publishes)
      REJECTED — move WordPress post to draft, notify via Telegram
      REVISE   — extract notes, re-write article, re-run council, update post
    """

    name = "bretalon_reply"
    schedule = "*/15 * * * *"  # every 15 minutes

    def __init__(self):
        super().__init__()
        self._state_dir = Path(
            os.environ.get("OBSERVER_STATE_DIR",
                           str(Path(__file__).parent / ".state"))
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _reply_state_file(self) -> Path:
        return self._state_dir / "bretalon_reply_processed.json"

    @property
    def _autopub_state_file(self) -> Path:
        return self._state_dir / "bretalon_autopublish_state.json"

    def _load_json(self, path: Path, default=None):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, TypeError):
                pass
        return default if default is not None else {}

    def _save_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))

    # mail provider IMAP credentials for hal@example.com
    IMAP_HOST = "mail.example.com"
    IMAP_PORT = 993
    IMAP_USER = "hal@example.com"
    IMAP_PASS = os.environ.get("HAL_IMAP_PASSWORD", "")

    def _search_replies(self) -> list[dict]:
        """Search HAL's mail provider inbox for replies to BRETALON review emails."""
        import email as email_mod
        import email.header
        import email.utils

        try:
            conn = imaplib.IMAP4_SSL(self.IMAP_HOST, self.IMAP_PORT)
            conn.login(self.IMAP_USER, self.IMAP_PASS)
        except Exception as e:
            log.warning("bretalon_reply: IMAP connect failed: %s", e)
            return []

        try:
            conn.select("INBOX")
            # Search for unseen messages with [BRETALON] in subject
            status, data = conn.search(None, '(UNSEEN SUBJECT "[BRETALON]")')
            if status != "OK" or not data[0]:
                return []

            uids = data[0].split()[-10:]  # max 10 per cycle
            replies = []

            for uid in uids:
                status, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                msg = email_mod.message_from_bytes(raw)

                from_raw = self._decode_imap_header(msg.get("From", ""))
                subject = self._decode_imap_header(msg.get("Subject", ""))
                date_str = msg.get("Date", "")
                msg_id = msg.get("Message-ID", f"{uid.decode()}@mail_provider")

                # Skip our own sent messages
                from_addr = email.utils.parseaddr(from_raw)[1]
                if from_addr == self.IMAP_USER:
                    continue

                body = self._extract_imap_body(msg)

                replies.append({
                    "imap_uid": uid.decode(),
                    "message_id": msg_id.strip(),
                    "from": from_raw,
                    "subject": subject,
                    "date": date_str,
                    "body": body,
                })

            return replies

        except Exception as e:
            log.warning("bretalon_reply: IMAP search failed: %s", e)
            return []
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _decode_imap_header(raw: str) -> str:
        """Decode IMAP email header (handles encoded words)."""
        import email.header
        if not raw:
            return ""
        parts = email.header.decode_header(raw)
        decoded = []
        for data, charset in parts:
            if isinstance(data, bytes):
                decoded.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(data)
        return " ".join(decoded)

    @staticmethod
    def _extract_imap_body(msg) -> str:
        """Extract plain text body from an IMAP email message."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            # Fallback to HTML stripped
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        text = payload.decode(charset, errors="replace")
                        return re.sub(r"<[^>]+>", "", text)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""

    @staticmethod
    def _classify_reply(body: str) -> tuple[str, str]:
        """Parse reply body for APPROVED/REVISE/REJECTED.

        Returns (action, notes) where action is one of:
        'approved', 'rejected', 'revise', 'unknown'
        """
        # Strip quoted reply chains — only look at the actual reply text
        lines = body.split("\n")
        reply_lines = []
        for line in lines:
            # Stop at quoted text markers
            if line.strip().startswith(">") or line.strip().startswith("On ") and "wrote:" in line:
                break
            reply_lines.append(line)

        reply_text = "\n".join(reply_lines).strip()
        reply_upper = reply_text.upper()

        if "APPROVED" in reply_upper or "APPROVE" in reply_upper:
            return "approved", ""
        elif "REJECTED" in reply_upper or "REJECT" in reply_upper or "CANCEL" in reply_upper:
            return "rejected", ""
        elif "REVISE" in reply_upper or "REVISION" in reply_upper or "AMEND" in reply_upper:
            # Everything after the keyword is revision notes
            notes = reply_text
            for keyword in ["REVISE", "REVISION", "AMEND", "revise", "revision", "amend"]:
                if keyword in notes:
                    idx = notes.index(keyword) + len(keyword)
                    notes = notes[idx:].strip().lstrip(":").lstrip("-").strip()
                    break
            return "revise", notes if notes else reply_text

        return "unknown", reply_text

    def _get_pending_post(self) -> dict | None:
        """Get the most recent pending post info from state."""
        state = self._load_json(self._autopub_state_file)
        post_id = state.get("last_post_id")
        if not post_id:
            return None
        return {
            "post_id": post_id,
            "title": state.get("last_title", ""),
            "topic": state.get("last_topic", ""),
        }

    def _cancel_post(self, post_id: str) -> bool:
        """Move WordPress post to draft status."""
        try:
            result = subprocess.run(
                ["ssh", SSH_HOST,
                 f"sudo docker exec {WP_CONTAINER} wp post update {post_id} "
                 f"--post_status=draft --allow-root"],
                capture_output=True, text=True, timeout=30,
            )
            return "Success" in result.stdout
        except Exception as e:
            log.error("bretalon_reply: failed to cancel post %s: %s", post_id, e)
            return False

    def _archive_reply(self, imap_uid: str):
        """Mark processed reply as read on mail provider IMAP."""
        try:
            conn = imaplib.IMAP4_SSL(self.IMAP_HOST, self.IMAP_PORT)
            conn.login(self.IMAP_USER, self.IMAP_PASS)
            conn.select("INBOX")
            conn.store(imap_uid.encode(), "+FLAGS", "\\Seen")
            conn.logout()
        except Exception as e:
            log.warning("bretalon_reply: failed to mark reply %s as read: %s", imap_uid, e)

    def run(self, ctx=None) -> ObserverResult:
        """Check for and process review email replies."""
        processed = set(self._load_json(self._reply_state_file, []))
        replies = self._search_replies()

        if not replies:
            return ObserverResult(success=True)

        pending = self._get_pending_post()
        actions_taken = []

        for reply in replies:
            if reply["message_id"] in processed:
                continue

            action, notes = self._classify_reply(reply["body"])
            sender = reply["from"]
            log.info("bretalon_reply: %s from %s — action: %s",
                     reply["subject"][:60], sender, action)

            if action == "approved":
                msg = (f"Article APPROVED by {sender}.\n"
                       f"Will auto-publish at scheduled time.")
                self.send_telegram(f"[bretalon_reply] {msg}")
                actions_taken.append(f"approved by {sender}")

            elif action == "rejected":
                if pending:
                    success = self._cancel_post(pending["post_id"])
                    status = "moved to draft" if success else "FAILED to cancel"
                    msg = (f"Article REJECTED by {sender}.\n"
                           f"Post {pending['post_id']} ({pending['title']}) {status}.")
                    self.send_telegram(f"[bretalon_reply] {msg}")
                    actions_taken.append(f"rejected by {sender}, post {status}")
                else:
                    self.send_telegram(
                        f"[bretalon_reply] REJECTED by {sender} but no pending post found")

            elif action == "revise":
                if pending:
                    msg = (f"REVISION requested by {sender}.\n"
                           f"Post: {pending['post_id']} ({pending['title']})\n"
                           f"Notes: {notes[:300]}")
                    self.send_telegram(f"[bretalon_reply] {msg}")

                    # Trigger revision pipeline
                    revised = self._handle_revision(pending, notes)
                    if revised:
                        actions_taken.append(f"revised per {sender}")
                    else:
                        actions_taken.append(f"revision failed for {sender}")
                else:
                    self.send_telegram(
                        f"[bretalon_reply] REVISE from {sender} but no pending post found")

            elif action == "unknown":
                msg = (f"Unrecognised reply from {sender}:\n"
                       f"{notes[:200]}\n\n"
                       f"Expected: APPROVED, REVISE, or REJECTED")
                self.send_telegram(f"[bretalon_reply] {msg}")
                actions_taken.append(f"unknown reply from {sender}")

            # Mark as processed and archive (mark read on IMAP)
            processed.add(reply["message_id"])
            self._archive_reply(reply["imap_uid"])

        # Save processed IDs (keep last 200)
        self._save_json(self._reply_state_file, sorted(processed)[-200:])

        if actions_taken:
            return ObserverResult(
                success=True,
                message=f"Processed {len(actions_taken)} replies: {'; '.join(actions_taken)}",
            )
        return ObserverResult(success=True)

    def _handle_revision(self, pending: dict, notes: str) -> bool:
        """Re-write article with revision notes, re-run council, update post."""
        post_id = pending["post_id"]
        topic = pending["topic"]
        title = pending["title"]

        log.info("bretalon_reply: starting revision for post %s (%s)", post_id, title)

        # Fetch current article content from WordPress
        try:
            raw = subprocess.run(
                ["ssh", SSH_HOST,
                 f"sudo docker exec {WP_CONTAINER} wp post get {post_id} "
                 f"--field=post_content --allow-root"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()

            # Strip HTML tags to get plain text for revision prompt
            import html as html_mod
            current_text = re.sub(r"<[^>]+>", "", raw)
            current_text = html_mod.unescape(current_text)
            current_text = re.sub(r"\n{3,}", "\n\n", current_text).strip()
        except Exception as e:
            log.error("bretalon_reply: failed to fetch post %s: %s", post_id, e)
            return False

        # Re-write with revision feedback
        autopub = BretalonAutoPublishObserver()
        article = autopub._write_article(
            topic=topic,
            research=current_text,  # use existing article as "research"
            revision_feedback=(
                f"EDITOR REVISION REQUEST:\n{notes}\n\n"
                f"The original article title was: {title}\n"
                f"Maintain the same topic and thesis. Address the editor's specific notes. "
                f"Keep the same headline unless the notes specifically request a title change."
            ),
        )

        if not article:
            log.error("bretalon_reply: revision writing failed for post %s", post_id)
            self.send_telegram(f"[bretalon_reply] Revision writing FAILED for post {post_id}")
            return False

        # Run council review on revised article
        council = autopub._council_review(article)
        log.info("bretalon_reply: revision council — %.1f/10 (%s)",
                 council.average_score, council.verdict)

        # Format and update WordPress post
        gutenberg = autopub._format_gutenberg(article)

        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                             delete=False, encoding="utf-8") as f:
                f.write(gutenberg)
                local_html = f.name

            subprocess.run(
                ["scp", local_html, f"{SSH_HOST}:/tmp/bretalon_revision.html"],
                capture_output=True, text=True, timeout=60, check=True,
            )

            result = subprocess.run(
                ["ssh", SSH_HOST,
                 f"sudo docker cp /tmp/bretalon_revision.html "
                 f"{WP_CONTAINER}:/tmp/bretalon_revision.html && "
                 f"sudo docker exec {WP_CONTAINER} wp post update {post_id} "
                 f"/tmp/bretalon_revision.html --allow-root"],
                capture_output=True, text=True, timeout=30,
            )

            os.unlink(local_html)

            if "Success" not in result.stdout:
                log.error("bretalon_reply: wp update failed: %s", result.stdout)
                return False

        except Exception as e:
            log.error("bretalon_reply: revision update failed: %s", e)
            return False

        # Send new review email
        post_info = {"post_id": post_id, "publish_date": "unchanged"}
        autopub._send_review_email(article, post_info, council)

        # Telegram summary
        msg = (f"Article REVISED and updated:\n"
               f"  Post ID: {post_id}\n"
               f"  Title: {article['title']}\n"
               f"  Words: ~{article.get('word_count', 0):,}\n"
               f"  Council: {council.average_score:.1f}/10 ({council.verdict})\n"
               f"  New review email sent")
        self.send_telegram(f"[bretalon_reply] {msg}")

        log.info("bretalon_reply: revision complete for post %s", post_id)
        return True


# ── Standalone testing ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Bretalon AutoPublish (standalone)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Full pipeline but skip WordPress publish and email")
    parser.add_argument("--once", action="store_true",
                        help="Run once regardless of schedule")
    parser.add_argument("--topic", type=str, default="",
                        help="Override topic selection with this topic")
    parser.add_argument("--collect-only", action="store_true",
                        help="Only run topic selection + research, then stop")
    args = parser.parse_args()

    obs = BretalonAutoPublishObserver()
    ctx = ObserverContext()

    if args.topic:
        # Inject topic into queue
        queue = obs._load_json(obs._queue_file, [])
        queue.insert(0, {
            "topic": args.topic,
            "injected_at": datetime.now(timezone.utc).isoformat(),
            "source": "cli",
        })
        obs._save_json(obs._queue_file, queue)
        print(f"Injected topic: {args.topic}")

    if args.collect_only:
        print("=== Stage 1: Topic Selection ===")
        topic = obs._select_topic(ctx)
        if not topic:
            print("No topic selected.")
            sys.exit(1)
        print(f"Selected: {topic}\n")

        print("=== Stage 2: Deep Research ===")
        research = obs._deep_research(topic)
        if research:
            print(f"Research: {len(research)} chars")
            print(research[:2000])
        else:
            print("Deep research failed or unavailable.")

        supplement = obs._supplement_with_grok(topic)
        if supplement:
            print(f"\nGrok supplement: {len(supplement)} chars")
            print(supplement[:1000])
        sys.exit(0)

    if args.once or args.dry_run:
        # Override schedule check
        original_should_run = obs.should_run
        obs.should_run = lambda now: True

    if args.dry_run:
        # Run pipeline stages 1-4 only
        print("=== DRY RUN MODE ===\n")

        topic = obs._select_topic(ctx)
        if not topic:
            print("No topic selected.")
            sys.exit(1)
        print(f"Topic: {topic}\n")

        print("Stage 2: Deep Research...")
        research = obs._deep_research(topic)
        supplement = obs._supplement_with_grok(topic)
        combined = (research or "") + (f"\n\n{supplement}" if supplement else "")
        print(f"  Research: {len(research or '')} chars, Supplement: {len(supplement)} chars\n")

        print("Stage 3: Writing...")
        article = obs._write_article(topic, combined, supplement)
        if not article:
            print("Article generation failed.")
            sys.exit(1)
        print(f"  Title: {article['title']}")
        print(f"  Subtitle: {article.get('subtitle', '')}")
        print(f"  Words: {article.get('word_count', 0)}\n")
        print(f"  Opening: {article['body'][:300]}...\n")

        print("Stage 4: Council Review...")
        council = obs._council_review(article)
        print(f"  Average: {council.average_score}/10")
        print(f"  Verdict: {council.verdict}")
        print(f"  Passed: {council.passed}")
        for m in council.members:
            status = f"{m.score}/10 ({m.verdict})" if not m.error else f"ERROR: {m.error[:60]}"
            print(f"  [{m.role}] {m.model}: {status}")

        print("\n=== DRY RUN COMPLETE (no publish, no email) ===")
        sys.exit(0)

    # Normal run (--once or scheduled)
    result = obs.run(ctx)
    if result.success:
        print(f"SUCCESS: {result.message}")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
