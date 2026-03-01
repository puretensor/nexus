#!/usr/bin/env python3
"""Intel Deep Analysis Observer — automated investigative analysis pipeline.

Collects news from RSS+GDELT+X/Twitter+Financial feeds, uses an AI council
(3 cloud LLMs) to score significance, runs Gemini Deep Research on the top
event, generates long-form analysis articles, and publishes to both
intel.puretensor.ai and intel.varangian.ai.

Schedule: every 8 hours (0 */8 * * *)
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

import sys as _sys
_nexus_root = str(Path(__file__).resolve().parent.parent)
if _nexus_root not in _sys.path:
    _sys.path.insert(0, _nexus_root)

from observers.base import ALERT_BOT_TOKEN, Observer, ObserverContext, ObserverResult
from observers.cloud_llm import (
    call_gemini_flash, call_xai_grok, call_claude_haiku, call_deepseek, extract_json,
)

log = logging.getLogger("nexus")

SCRIPT_DIR = Path(__file__).parent
STATE_DIR = SCRIPT_DIR / ".state"
RSS_CONF = Path(os.environ.get(
    "RSS_FEEDS_CONF",
    str(Path.home() / ".config" / "puretensor" / "rss_feeds.conf"),
))

GCP_SSH_HOST = os.environ.get("GCP_SSH_HOST", "")
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")

COUNCIL_THRESHOLD = 7.0

BRANDS = {
    "puretensor": {
        "webroot": "/var/www/intel.puretensor.ai",
        "ga_id": "G-Q5RPQQ747Z",
        "site_url": "https://intel.puretensor.ai",
        "nav_brand": "PureTensor // Intel",
        "nav_brand_href": "/",
        "accent_css": "--cyan: #00E5FF; --cyan-bright: #7DE3FF; --cyan-dim: #0088A3; --cyan-border: rgba(0, 229, 255, 0.2); --cyan-glow: rgba(0, 229, 255, 0.06);",
        "footer_parent_text": "A PureTensor Inc company",
        "footer_parent_href": "https://www.puretensor.ai",
        "footer_entity": "PureTensor Inc &middot; United States",
        "disclaimer_href": "/disclaimer.html",
        "privacy_href": "https://www.puretensor.ai/privacy.html",
        "terms_href": "https://www.puretensor.ai/terms.html",
    },
    "varangian": {
        "webroot": "/var/www/intel.varangian.ai",
        "ga_id": "G-6VCCF8WSXL",
        "site_url": "https://intel.varangian.ai",
        "nav_brand": "Varangian // Intel",
        "nav_brand_href": "/",
        "accent_css": "--cyan: #c9b97a; --cyan-bright: #ddd0a0; --cyan-dim: #8b7f52; --cyan-border: rgba(201, 185, 122, 0.2); --cyan-glow: rgba(201, 185, 122, 0.06);",
        "footer_parent_text": "A Varangian Group company",
        "footer_parent_href": "https://varangian.ai",
        "footer_entity": "Varangian Group Ltd &middot; United Kingdom",
        "disclaimer_href": "/disclaimer.html",
        "privacy_href": "/privacy-policy.html",
        "terms_href": "/terms-of-service.html",
    },
}

WRITER_PROMPTS = {
    "puretensor": (
        "You are a senior strategic analyst at PureTensor Intel. You produce in-depth, "
        "evidence-based investigative analysis articles. Your tone is analytical, dispassionate, "
        "and precise. You write in British English. You cite sources with numbered references. "
        "Do NOT use markdown formatting. Output clean plain text with section headers in UPPERCASE. "
        "Do NOT include any <think> tags or thinking process."
    ),
    "varangian": (
        "You are a senior intelligence analyst at Varangian Intel, a British strategic "
        "intelligence firm in London, writing for UK defence officials, City of London risk desks, "
        "and Whitehall policy staff. Your analytical frame is British: always ask 'what does this "
        "mean for Britain?' Cover UK defence posture, Five Eyes equities, City exposure, sterling "
        "implications, AUKUS, CPTPP, and post-Brexit positioning. NATO-aligned and pro-Western, "
        "but distinctly British. Measured, precise, in the Whitehall tradition. "
        "British English throughout. Do NOT use markdown. Section headers in UPPERCASE. "
        "Do NOT include any <think> tags."
    ),
}


class IntelDeepAnalysisObserver(Observer):
    """Generates investigative deep-analysis articles via AI council + Gemini Deep Research."""

    name = "intel_deep_analysis"
    schedule = "0 */12 * * *"  # every 12 hours

    STATE_FILE = Path(
        os.environ.get("OBSERVER_STATE_DIR", str(STATE_DIR))
    ) / "intel_deep_analysis_state.json"

    MAX_PER_FEED = 8
    MAX_ARTICLES_FOR_COUNCIL = 80

    # ── Data Collection ──────────────────────────────────────────────

    def _load_rss_feeds(self) -> dict[str, str]:
        feeds = {}
        if not RSS_CONF.exists():
            return feeds
        in_feeds = False
        for line in RSS_CONF.read_text().splitlines():
            line = line.strip()
            if line == "[feeds]":
                in_feeds = True
                continue
            if line.startswith("[") and line != "[feeds]":
                in_feeds = False
                continue
            if not in_feeds or not line or line.startswith("#"):
                continue
            if "=" in line:
                name, url = line.split("=", 1)
                feeds[name.strip()] = url.strip()
        return feeds

    def _fetch_rss(self, feed_name: str, feed_url: str) -> list[dict]:
        articles = []
        try:
            req = urllib.request.Request(feed_url,
                                        headers={"User-Agent": "PureTensor-Intel/2.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
        except Exception:
            return []
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return []
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item")
        if items:
            for item in items[:self.MAX_PER_FEED]:
                title = self._el_text(item, "title")
                if title:
                    articles.append({
                        "source": feed_name,
                        "title": title,
                        "summary": self._clean_html(self._el_text(item, "description"))[:500],
                        "url": self._el_text(item, "link"),
                    })
            return articles
        for entry in (root.findall("atom:entry", ns) or root.findall("entry"))[:self.MAX_PER_FEED]:
            title = self._el_text(entry, "atom:title", ns) or self._el_text(entry, "title")
            link_el = entry.find("atom:link", ns)
            if link_el is None:
                link_el = entry.find("link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary_el = entry.find("atom:summary", ns)
            if summary_el is None:
                summary_el = entry.find("summary")
            summary = self._clean_html(summary_el.text)[:500] if summary_el is not None and summary_el.text else ""
            if title:
                articles.append({"source": feed_name, "title": title, "summary": summary, "url": link})
        return articles

    @staticmethod
    def _el_text(el, tag, ns=None):
        child = el.find(tag, ns) if ns else el.find(tag)
        return child.text.strip() if child is not None and child.text else ""

    @staticmethod
    def _clean_html(text: str) -> str:
        if not text:
            return ""
        return re.sub(r"<[^>]+>", "", text).strip()

    def _fetch_all_rss(self) -> list[dict]:
        feeds = self._load_rss_feeds()
        all_articles = []
        for name, url in feeds.items():
            all_articles.extend(self._fetch_rss(name, url))
        log.info("intel_deep_analysis: RSS collected %d articles", len(all_articles))
        return all_articles

    def _fetch_gdelt(self) -> list[dict]:
        queries = ["geopolitics conflict", "cybersecurity threat", "defence military",
                    "sanctions trade policy", "AI technology semiconductor",
                    "financial markets crisis", "energy supply disruption"]
        articles = []
        for query in queries:
            try:
                params = urllib.parse.urlencode({
                    "query": query, "mode": "artlist", "maxrecords": 8,
                    "format": "json", "sort": "DateDesc", "timespan": "24h",
                })
                req = urllib.request.Request(f"{GDELT_DOC_API}?{params}",
                                            headers={"User-Agent": "PureTensor-Intel/2.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read())
                for art in data.get("articles", [])[:5]:
                    articles.append({
                        "source": f"GDELT ({query.split()[0]})",
                        "title": art.get("title", ""),
                        "summary": "", "url": art.get("url", ""),
                    })
            except Exception:
                continue
        log.info("intel_deep_analysis: GDELT collected %d articles", len(articles))
        return articles[:30]

    def _fetch_financial(self) -> list[dict]:
        financial_feeds = {
            "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
            "CNBC World": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
        }
        articles = []
        for name, url in financial_feeds.items():
            articles.extend(self._fetch_rss(name, url))
        log.info("intel_deep_analysis: Financial collected %d articles", len(articles))
        return articles

    def _fetch_x_trends(self) -> list[dict]:
        """Use xAI Grok with web_search to get trending geopolitical topics."""
        try:
            result = call_xai_grok(
                "You are a news trend analyst.",
                "What are the top 5 most significant geopolitical, defence, technology, or "
                "financial events happening right now globally? For each, provide a short title "
                "and a 1-sentence summary. Respond as a JSON array of objects with 'title' and "
                "'summary' keys.",
                timeout=30, tools=[{"type": "web_search"}],
            )
            parsed = extract_json(result)
            if isinstance(parsed, list):
                return [{"source": "X/Twitter Trends", "title": t.get("title", ""),
                         "summary": t.get("summary", ""), "url": ""} for t in parsed[:5]]
        except Exception as e:
            log.debug("intel_deep_analysis: X trends fetch failed: %s", e)
        return []

    def _fetch_searxng(self, query: str) -> list[dict]:
        """Enrich a topic with SearXNG results."""
        try:
            params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "news"})
            req = urllib.request.Request(f"{SEARXNG_URL}?{params}",
                                        headers={"User-Agent": "PureTensor-Intel/2.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            results = []
            for r in data.get("results", [])[:5]:
                results.append({
                    "source": f"SearXNG ({query[:20]})",
                    "title": r.get("title", ""),
                    "summary": r.get("content", "")[:300],
                    "url": r.get("url", ""),
                })
            return results
        except Exception:
            return []

    def _collect_all(self) -> list[dict]:
        all_articles = []
        all_articles.extend(self._fetch_all_rss())
        all_articles.extend(self._fetch_gdelt())
        all_articles.extend(self._fetch_financial())
        all_articles.extend(self._fetch_x_trends())
        # Deduplicate
        seen = set()
        unique = []
        for art in all_articles:
            key = re.sub(r"[^a-z0-9 ]", "", art.get("title", "").lower())[:60]
            if key and key not in seen:
                seen.add(key)
                unique.append(art)
        log.info("intel_deep_analysis: %d unique articles after dedup", len(unique))
        return unique[:self.MAX_ARTICLES_FOR_COUNCIL]

    # ── Delta Detection ──────────────────────────────────────────────

    def _load_state(self) -> dict:
        if self.STATE_FILE.exists():
            try:
                return json.loads(self.STATE_FILE.read_text())
            except (json.JSONDecodeError, TypeError):
                pass
        return {"published_hashes": [], "last_run": None}

    def _save_state(self, state: dict):
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE_FILE.write_text(json.dumps(state, indent=2))

    def _event_hash(self, title: str) -> str:
        norm = re.sub(r"[^a-z0-9]", "", title.lower())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    # ── Domain Classification ────────────────────────────────────────

    DOMAINS = ["Geopolitical", "Defence", "Financial", "Technology"]

    def _classify_by_domain(self, articles: list[dict]) -> dict[str, list[dict]]:
        """Classify articles into 4 strategic domains using Gemini Flash."""
        cap = min(len(articles), 50)
        article_list = "\n".join(
            f"[{i+1}] {a['title']}" for i, a in enumerate(articles[:cap])
        )
        prompt = (
            f"Classify these {cap} headlines into exactly 4 domains: "
            f"Geopolitical, Defence, Financial, Technology.\n"
            f"Each headline should go into the single most relevant domain.\n"
            f"For each domain, also write a 1-sentence summary of the dominant theme.\n\n"
            f"Respond as JSON: {{\"Geopolitical\": {{\"articles\": [1,2,...], \"theme\": \"...\"}}, "
            f"\"Defence\": {{...}}, \"Financial\": {{...}}, \"Technology\": {{...}}}}\n\n"
            f"HEADLINES:\n{article_list}"
        )
        try:
            result = call_gemini_flash("You are a news classifier.", prompt, timeout=45)
            parsed = extract_json(result)
            if isinstance(parsed, dict):
                domains = {}
                for domain in self.DOMAINS:
                    entry = parsed.get(domain, {})
                    domain_articles = []
                    for idx in entry.get("articles", []):
                        if isinstance(idx, int) and 1 <= idx <= len(articles):
                            domain_articles.append(articles[idx - 1])
                    domains[domain] = {
                        "articles": domain_articles,
                        "theme": entry.get("theme", ""),
                    }
                return domains
        except Exception as e:
            log.warning("intel_deep_analysis: domain classification failed: %s", e)
        # Fallback: all articles into each domain
        return {d: {"articles": articles[:15], "theme": ""} for d in self.DOMAINS}

    # ── AI Council ───────────────────────────────────────────────────

    @staticmethod
    def _call_with_retry(fn, name: str, max_retries: int = 2, backoff: float = 5.0):
        """Call a council function with retries and exponential backoff."""
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    wait = backoff * (2 ** attempt)
                    log.info("intel_deep_analysis: council %s attempt %d failed (%s), retrying in %.0fs",
                             name, attempt + 1, e, wait)
                    time.sleep(wait)
        raise last_err

    def _council_score_domains(self, domains: dict) -> list[dict]:
        """Run AI council to score each domain for significance. Returns domains above threshold."""
        domain_text = ""
        for domain, data in domains.items():
            titles = [a["title"] for a in data["articles"][:5]]
            theme = data.get("theme", "")
            domain_text += f"\n{domain}:\n  Theme: {theme}\n  Headlines: {'; '.join(titles[:4])}\n"

        system = "You are a strategic intelligence analyst evaluating world events for significance."
        prompt = (
            f"Below are articles grouped into 4 strategic domains from the past 24 hours.\n"
            f"Score EACH domain on three dimensions (1-10):\n"
            f"- Novelty: How new/unexpected are these developments?\n"
            f"- Global Impact: How widely will this affect the world?\n"
            f"- Analysis Depth: How much would a 4,000-word investigative piece add?\n\n"
            f"For each domain, suggest 3 analytical angles.\n\n"
            f"Respond as JSON: {{\"domains\": [{{\"domain\": \"Geopolitical\", \"novelty\": N, "
            f"\"impact\": N, \"depth\": N, \"angles\": [\"...\"]}}]}}\n\n"
            f"DOMAINS:\n{domain_text}"
        )

        callers = {
            "gemini": lambda: call_gemini_flash(system, prompt, timeout=45),
            "grok": lambda: call_xai_grok(system, prompt, timeout=45),
            "claude": lambda: call_claude_haiku(system, prompt, timeout=45),
            "deepseek": lambda: call_deepseek(system, prompt, timeout=45),
        }

        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._call_with_retry, fn, name): name
                for name, fn in callers.items()
            }
            for future in as_completed(futures, timeout=120):
                name = futures[future]
                try:
                    raw = future.result()
                    parsed = extract_json(raw)
                    if isinstance(parsed, dict) and "domains" in parsed:
                        results[name] = parsed
                        log.info("intel_deep_analysis: council %s responded", name)
                    else:
                        log.warning("intel_deep_analysis: council %s returned invalid JSON", name)
                except Exception as e:
                    log.warning("intel_deep_analysis: council %s failed after retries: %s", name, e)

        if len(results) < 1:
            log.error("intel_deep_analysis: council quorum failed — no models responded")
            return []
        log.info("intel_deep_analysis: council quorum %d/4", len(results))

        # Aggregate scores per domain across models
        domain_scores = {}  # domain -> list of (avg_score, angles)
        for model_name, data in results.items():
            for entry in data.get("domains", []):
                domain = entry.get("domain", "")
                if domain not in self.DOMAINS:
                    continue
                if domain not in domain_scores:
                    domain_scores[domain] = {"scores": [], "angles": []}
                avg = (entry.get("novelty", 0) + entry.get("impact", 0) + entry.get("depth", 0)) / 3
                domain_scores[domain]["scores"].append(avg)
                domain_scores[domain]["angles"].extend(entry.get("angles", []))

        # Filter domains above threshold
        qualified = []
        for domain in self.DOMAINS:
            if domain not in domain_scores:
                continue
            scores = domain_scores[domain]["scores"]
            composite = sum(scores) / len(scores)
            if composite >= COUNCIL_THRESHOLD:
                qualified.append({
                    "domain": domain,
                    "topic": domains[domain].get("theme", domain),
                    "score": round(composite, 2),
                    "angles": domain_scores[domain]["angles"][:5],
                    "articles": domains[domain]["articles"],
                    "council_models": list(results.keys()),
                })
                log.info("intel_deep_analysis: %s qualifies (%.1f)", domain, composite)
            else:
                log.info("intel_deep_analysis: %s below threshold (%.1f)", domain, composite)

        return qualified

    # ── Gemini Deep Research ─────────────────────────────────────────

    def _deep_research(self, topic: str, angles: list[str], context: str) -> str | None:
        """Run Gemini Deep Research on a topic. Returns markdown or None."""
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            log.error("intel_deep_analysis: GEMINI_API_KEY not set for deep research")
            return None

        try:
            os.environ["GOOGLE_API_KEY"] = api_key
            from google import genai
            client = genai.Client()

            angles_text = "\n".join(f"- {a}" for a in angles) if angles else "- General strategic analysis"
            research_query = (
                f"Conduct a comprehensive investigative analysis of: {topic}\n\n"
                f"Context from recent news:\n{context[:2000]}\n\n"
                f"Analytical angles to explore:\n{angles_text}\n\n"
                f"Cover: background and timeline, key actors and their motivations, "
                f"strategic implications, second-order effects, expert assessments, "
                f"and forward-looking scenarios. Cite all sources with URLs."
            )

            log.info("intel_deep_analysis: starting deep research on '%s'", topic)
            interaction = client.interactions.create(
                input=research_query,
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
                        log.info("intel_deep_analysis: deep research complete (%d chars)", len(result))
                        return result
                    return None
                elif interaction.status == "failed":
                    log.error("intel_deep_analysis: deep research failed")
                    return None

            log.error("intel_deep_analysis: deep research timed out")
            return None

        except Exception as e:
            log.error("intel_deep_analysis: deep research error: %s", e)
            return None

    # ── Article Generation ───────────────────────────────────────────

    def _generate_article(self, topic: str, research: str, angles: list[str],
                          raw_articles: list[dict], brand: str) -> dict | None:
        """Generate a long-form analysis article using Gemini Flash."""
        from observers.llm import call_llm

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%d %b %Y")
        time_str = now.strftime("%H:%M UTC")

        # Build source context
        source_text = ""
        for i, a in enumerate(raw_articles[:10], 1):
            source_text += f"[{i}] {a['title']} — {a.get('source', 'Unknown')}"
            if a.get("url"):
                source_text += f" ({a['url']})"
            source_text += "\n"

        research_excerpt = research[:12000] if research else "No deep research available — write from source material only."
        is_fallback = not research

        angles_text = "\n".join(f"- {a}" for a in angles) if angles else ""

        prompt = f"""Write a comprehensive investigative analysis article on the following topic.

TOPIC: {topic}
DATE: {date_str}, {time_str}

DEEP RESEARCH FINDINGS:
{research_excerpt}

ANALYTICAL ANGLES:
{angles_text}

RAW SOURCE MATERIAL:
{source_text}

INSTRUCTIONS:
1. Write a compelling article title (max 12 words).
2. Write a subtitle (max 25 words) — the key takeaway.
3. Write an EXECUTIVE SUMMARY section (150-200 words).
4. Write 4-6 analytical sections, each with an UPPERCASE header and 2-4 substantive paragraphs.
   Each section should provide analysis, not just description — explain WHY things matter and WHAT comes next.
5. Include a KEY ASSESSMENTS section with 4-6 bullet points, each with a confidence level (HIGH/MEDIUM/LOW).
6. End with numbered SOURCES section listing all referenced sources with URLs.
7. Target length: 2,000-3,000 words.

OUTPUT FORMAT:
TITLE: [title]
SUBTITLE: [subtitle]
CATEGORY: [one of: Geopolitical, Technology, Defence, Financial, Cybersecurity, Multi-Domain]

[body text with UPPERCASE section headers]"""

        system_prompt = WRITER_PROMPTS.get(brand, WRITER_PROMPTS["puretensor"])
        try:
            raw, backend = call_llm(
                system_prompt=system_prompt,
                user_prompt=prompt,
                timeout=420,
                num_predict=8192,
                temperature=0.4,
                preferred_backend="gemini",
                override_gemini_model="gemini-2.5-flash",
            )
        except Exception as e:
            log.error("intel_deep_analysis: article generation failed (%s): %s", brand, e)
            return None

        # Parse response
        title, subtitle, category = "Deep Analysis", "", "Multi-Domain"
        lines = raw.split("\n")
        body_start = 0
        for i, line in enumerate(lines):
            if line.startswith("TITLE:"):
                title = line[6:].strip().strip('"\'')
            elif line.startswith("SUBTITLE:"):
                subtitle = line[9:].strip().strip('"\'')
            elif line.startswith("CATEGORY:"):
                category = line[9:].strip()
                body_start = i + 1
                break
            elif i > 5:
                body_start = 0
                break

        body = "\n".join(lines[body_start:]).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
        date_prefix = now.strftime("%Y-%m-%d")
        slug = f"{date_prefix}-{slug}"

        # Count citations in research
        citation_count = 0
        if research:
            citation_count = len(re.findall(r'https?://[^\s\)]+', research))

        return {
            "title": title, "subtitle": subtitle, "category": category,
            "body": body, "slug": slug, "date": date_str, "time": time_str,
            "datetime": now, "backend": backend, "is_fallback": is_fallback,
            "citation_count": citation_count,
        }

    # ── HTML Generation ──────────────────────────────────────────────

    def _body_to_html(self, body: str) -> str:
        html_parts = []
        paragraphs = body.split("\n\n")
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            first_line = para.split("\n")[0].strip()
            # Section header
            if re.match(r"^[A-Z][A-Z &:\-,/]{3,}$", first_line):
                html_parts.append(f"            <h2>{html_escape(first_line)}</h2>")
                rest = "\n".join(para.split("\n")[1:]).strip()
                if rest:
                    for sp in rest.split("\n"):
                        sp = sp.strip()
                        if sp:
                            html_parts.append(f"            <p>{self._fmt(sp)}</p>")
                continue
            # Key assessments
            if first_line.upper().startswith("KEY ASSESSMENTS"):
                html_parts.append('            <div class="key-assessment">')
                html_parts.append('                <div class="key-assessment-label">Key Assessments</div>')
                for line in para.split("\n")[1:]:
                    line = line.strip().lstrip("-*").strip()
                    if line:
                        line = re.sub(r"\b(HIGH|MEDIUM|LOW)\b",
                                      r'<span style="color: var(--cyan); font-family: var(--font-mono); font-size: 0.8em;">\1</span>', line)
                        html_parts.append(f"                <p>{self._fmt(line)}</p>")
                html_parts.append("            </div>")
                continue
            # Sources section
            if first_line.upper().startswith("SOURCES"):
                html_parts.append('            <div class="sources-section" style="margin-top: 2.5rem; padding-top: 1.5rem; border-top: 1px solid var(--border);">')
                html_parts.append('                <h3 style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 1rem;">Sources</h3>')
                html_parts.append('                <ol style="padding-left: 1.5rem; font-size: 0.85rem; color: var(--text-secondary); line-height: 1.8;">')
                for line in para.split("\n")[1:]:
                    line = line.strip().lstrip("0123456789.)-").strip()
                    if line:
                        # Auto-link URLs
                        line = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" style="color: var(--cyan-dim); word-break: break-all;">\1</a>', html_escape(line))
                        html_parts.append(f"                    <li>{line}</li>")
                html_parts.append("                </ol>")
                html_parts.append("            </div>")
                continue
            # Bullet points
            if para.startswith("- ") or para.startswith("* "):
                html_parts.append("            <ul>")
                for line in para.split("\n"):
                    line = line.strip().lstrip("-*").strip()
                    if line:
                        line = re.sub(r"\b(HIGH|MEDIUM|LOW)\b",
                                      r'<span style="color: var(--cyan); font-family: var(--font-mono); font-size: 0.8em;">\1</span>', line)
                        html_parts.append(f"                <li>{self._fmt(line)}</li>")
                html_parts.append("            </ul>")
                continue
            # Regular paragraphs
            for line in para.split("\n"):
                line = line.strip()
                if line:
                    html_parts.append(f"            <p>{self._fmt(line)}</p>")
        return "\n".join(html_parts)

    @staticmethod
    def _fmt(text: str) -> str:
        text = html_escape(text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
        return text

    def _generate_html(self, article: dict, brand: str, score: float, council_models: list) -> str:
        bc = BRANDS[brand]
        t = html_escape(article["title"])
        st = html_escape(article["subtitle"])
        cat = html_escape(article["category"])
        d = html_escape(article["date"])
        tm = html_escape(article["time"])
        body_html = self._body_to_html(article["body"])
        favicon_hex = "%23c9b97a" if brand == "varangian" else "%2300E5FF"
        pipeline = "Varangian Intel" if brand == "varangian" else "PureTensor Intel"
        fb_notice = ' <span style="color: #FF6B35;">(Source-based fallback — deep research unavailable)</span>' if article.get("is_fallback") else ""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<script async src="https://www.googletagmanager.com/gtag/js?id={bc['ga_id']}"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}};gtag("js",new Date());gtag("config","{bc['ga_id']}");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{t} | {html_escape(bc['nav_brand'])}</title>
    <meta name="description" content="{st}">
    <meta name="theme-color" content="#0A0C10">
    <meta property="og:title" content="{t}">
    <meta property="og:description" content="{st}">
    <meta property="og:type" content="article">
    <meta property="og:url" content="{bc['site_url']}/analysis/{article['slug']}.html">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><polygon points='16,2 28,28 16,22 4,28' fill='{favicon_hex}'/></svg>">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
    <style>
        :root {{ --bg-primary: #09090b; --bg-surface: #0f0f12; --bg-elevated: #16161a; {bc['accent_css']} --gold: #c9b97a; --gold-bright: #ddd0a0; --gold-dim: #8b7f52; --gold-border: rgba(201, 185, 122, 0.2); --text-primary: #e8e4dc; --text-secondary: #9a958b; --text-muted: #5a564e; --border: rgba(255,255,255,0.06); --font-serif: 'Cormorant Garamond', Georgia, 'Times New Roman', serif; --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; --font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace; }}
        *,*::before,*::after {{ margin:0; padding:0; box-sizing:border-box; }}
        html {{ scroll-behavior:smooth; font-size:16px; }}
        body {{ font-family:var(--font-sans); background:var(--bg-primary); color:var(--text-primary); line-height:1.65; font-weight:300; -webkit-font-smoothing:antialiased; overflow-x:hidden; }}
        body::before {{ content:''; position:fixed; inset:0; opacity:0.025; pointer-events:none; z-index:9999; background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); background-size:256px 256px; }}
        nav {{ position:fixed; top:0; left:0; right:0; z-index:1000; padding:1.25rem calc(max(2rem, (100% - 1140px) / 2)); display:flex; align-items:center; justify-content:space-between; background:rgba(9,9,11,0.85); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border-bottom:1px solid var(--border); }}
        .nav-brand {{ font-family:var(--font-serif); font-weight:600; font-size:1.4rem; color:var(--gold); letter-spacing:0.15em; text-transform:uppercase; text-decoration:none; }}
        .nav-back {{ font-family:var(--font-mono); font-size:0.72rem; color:var(--text-secondary); text-decoration:none; letter-spacing:0.1em; text-transform:uppercase; transition:color 0.2s; }}
        .nav-back:hover {{ color:var(--cyan); }}
        .container {{ max-width:760px; margin:0 auto; padding:0 2rem; }}
        .article-header {{ padding-top:8rem; padding-bottom:3rem; border-bottom:1px solid var(--border); margin-bottom:3rem; }}
        .article-meta {{ display:flex; align-items:center; gap:1rem; margin-bottom:2rem; flex-wrap:wrap; }}
        .article-badge {{ font-family:var(--font-mono); font-size:0.6rem; font-weight:500; color:var(--cyan); letter-spacing:0.1em; text-transform:uppercase; padding:0.2rem 0.6rem; border:1px solid var(--cyan-border); background:rgba(0,229,255,0.05); }}
        .article-badge--analysis {{ color:#9C27B0; border-color:rgba(156,39,176,0.3); background:rgba(156,39,176,0.05); }}
        .article-date {{ font-family:var(--font-mono); font-size:0.7rem; color:var(--text-muted); letter-spacing:0.05em; }}
        .article-title {{ font-family:var(--font-serif); font-weight:700; font-size:clamp(2rem,4vw,3rem); line-height:1.15; color:var(--text-primary); margin-bottom:1.5rem; }}
        .article-subtitle {{ font-size:1.15rem; color:var(--text-secondary); line-height:1.7; max-width:640px; }}
        .briefing-meta {{ display:flex; gap:2rem; margin-top:1.5rem; flex-wrap:wrap; }}
        .briefing-meta-item {{ display:flex; flex-direction:column; gap:0.2rem; }}
        .briefing-meta-value {{ font-family:var(--font-mono); font-size:0.75rem; font-weight:500; color:var(--cyan); }}
        .briefing-meta-label {{ font-family:var(--font-mono); font-size:0.6rem; color:var(--text-muted); letter-spacing:0.1em; text-transform:uppercase; }}
        .article-body {{ padding-bottom:5rem; }}
        .article-body h2 {{ font-family:var(--font-serif); font-size:1.65rem; font-weight:600; color:var(--text-primary); margin-top:3rem; margin-bottom:1.25rem; }}
        .article-body h3 {{ font-family:var(--font-serif); font-size:1.3rem; font-weight:600; color:var(--text-primary); margin-top:2.5rem; margin-bottom:1rem; }}
        .article-body p {{ font-size:1.05rem; color:var(--text-secondary); line-height:1.85; margin-bottom:1.5rem; }}
        .article-body strong {{ color:var(--text-primary); font-weight:500; }}
        .key-assessment {{ background:var(--bg-surface); border:1px solid var(--border); border-left:2px solid var(--cyan-dim); padding:1.5rem 2rem; margin:2.5rem 0; }}
        .key-assessment-label {{ font-family:var(--font-mono); font-size:0.65rem; font-weight:500; color:var(--cyan); letter-spacing:0.15em; text-transform:uppercase; margin-bottom:0.75rem; }}
        .key-assessment p {{ font-size:0.95rem; color:var(--text-secondary); line-height:1.7; margin-bottom:0.75rem; }}
        .key-assessment p:last-child {{ margin-bottom:0; }}
        .article-body ul {{ list-style:none; margin:1.5rem 0; padding:0; }}
        .article-body ul li {{ font-size:1rem; color:var(--text-secondary); line-height:1.7; padding-left:1.5rem; margin-bottom:0.75rem; position:relative; }}
        .article-body ul li::before {{ content:'\\25C6'; position:absolute; left:0; color:var(--cyan-dim); font-size:0.5rem; top:0.4rem; }}
        .cyan-divider {{ width:100%; height:1px; background:linear-gradient(90deg,transparent,var(--cyan-border),transparent); }}
        .automated-notice {{ background:var(--bg-surface); border:1px solid var(--border); padding:1.25rem 1.5rem; margin:2rem 0 0 0; font-family:var(--font-mono); font-size:0.72rem; color:var(--text-muted); line-height:1.6; }}
        .automated-notice strong {{ color:var(--text-secondary); }}
        footer {{ border-top:1px solid var(--border); padding:2rem 0; }}
        .footer-inner {{ display:flex; justify-content:space-between; align-items:center; }}
        .footer-entity {{ font-family:var(--font-mono); font-size:0.68rem; color:var(--text-muted); }}
        .footer-mark {{ font-family:var(--font-serif); font-size:0.8rem; color:var(--text-muted); }}
        .footer-parent {{ text-align:center; margin-bottom:1.25rem; }}
        .footer-parent a {{ font-family:var(--font-mono); font-size:0.65rem; color:var(--text-muted); text-decoration:none; letter-spacing:0.1em; text-transform:uppercase; transition:color 0.2s; }}
        .footer-parent a:hover {{ color:var(--cyan); }}
        .footer-links {{ display:flex; justify-content:center; gap:2rem; margin:0.75rem 0; font-family:var(--font-mono); font-size:0.7rem; }}
        .footer-links a {{ color:var(--text-secondary); text-decoration:none; transition:color 0.2s; }}
        .footer-links a:hover {{ color:var(--cyan); }}
        @media (max-width:768px) {{ nav {{ padding:1rem 1.5rem; }} .container {{ padding:0 1.5rem; }} .article-header {{ padding-top:6rem; }} .briefing-meta {{ gap:1.5rem; }} }}
    </style>
</head>
<body>
<nav>
    <a href="{bc['nav_brand_href']}" class="nav-brand">{html_escape(bc['nav_brand'])}</a>
    <a href="{bc['nav_brand_href']}" class="nav-back">&larr; All Analysis</a>
</nav>
<article>
    <header class="article-header">
        <div class="container">
            <div class="article-meta">
                <span class="article-badge article-badge--analysis">Deep Analysis</span>
                <span class="article-badge">{cat}</span>
                <span class="article-date">{d} &middot; {tm}</span>
            </div>
            <h1 class="article-title">{t}</h1>
            <p class="article-subtitle">{st}</p>
            <div class="briefing-meta">
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">{score:.1f}/10</span>
                    <span class="briefing-meta-label">Significance Score</span>
                </div>
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">{article.get('citation_count', 0)}</span>
                    <span class="briefing-meta-label">Citations</span>
                </div>
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">{len(council_models)}/3</span>
                    <span class="briefing-meta-label">Council Quorum</span>
                </div>
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">Automated</span>
                    <span class="briefing-meta-label">Pipeline</span>
                </div>
            </div>
        </div>
    </header>
    <div class="article-body">
        <div class="container">
            <div class="article-disclaimer" style="background:rgba(255,180,0,0.06); border-left:2px solid rgba(255,180,0,0.5); padding:1rem 1.5rem; margin:0 0 2rem 0; border-radius:0 6px 6px 0; font-family:var(--font-sans); font-size:0.82rem; line-height:1.6; color:var(--text-secondary);">
                <span style="font-family:var(--font-mono); font-size:0.65rem; text-transform:uppercase; letter-spacing:0.1em; color:rgba(255,180,0,0.7); display:block; margin-bottom:0.5rem;">Disclaimer</span>
                This analysis is provided for informational and educational purposes only and does not constitute investment, financial, legal, or professional advice. Content is AI-assisted and human-reviewed. See our full <a href="{bc['disclaimer_href']}" style="color:var(--cyan-dim); text-decoration:underline;">Disclaimer</a> for important limitations.
            </div>
{body_html}
            <div class="automated-notice">
                <strong>Automated Deep Analysis</strong> &mdash; This article was generated by the {html_escape(pipeline)} deep analysis pipeline: multi-source data fusion, AI council significance scoring ({', '.join(council_models)}), Gemini Deep Research, and structured analytical writing ({html_escape(article.get('backend', 'Gemini'))}).{fb_notice} Published {tm} on {d}. All automated analyses are subject to editorial review.
            </div>
        </div>
    </div>
</article>
<div class="cyan-divider"></div>
<footer>
    <div class="container">
        <div class="footer-parent"><a href="{bc['footer_parent_href']}">{bc['footer_parent_text']}</a></div>
        <div class="footer-links">
            <a href="{bc['disclaimer_href']}">Disclaimer</a>
            <a href="{bc['privacy_href']}">Privacy Policy</a>
            <a href="{bc['terms_href']}">Terms of Service</a>
        </div>
        <div class="footer-inner">
            <span class="footer-entity">{bc['footer_entity']}</span>
            <span class="footer-mark">&copy; 2026</span>
        </div>
    </div>
</footer>
</body>
</html>"""

    # ── Index Update ─────────────────────────────────────────────────

    def _generate_card_html(self, article: dict, score: float) -> str:
        t = html_escape(article["title"])
        st = html_escape(article["subtitle"])
        cat = html_escape(article["category"])
        d = html_escape(article["date"])
        return f"""            <a href="/analysis/{article['slug']}.html" class="analysis-card">
                <div class="analysis-card-meta">
                    <span class="analysis-card-badge" style="color: #9C27B0; border-color: rgba(156,39,176,0.3);">Deep Analysis</span>
                    <span class="analysis-card-badge">{cat}</span>
                    <span class="analysis-card-date">{d} &middot; Score: {score:.1f}</span>
                </div>
                <h3 class="analysis-card-title">{t}</h3>
                <p class="analysis-card-summary">{st}</p>
                <span class="analysis-card-link">Read analysis &rarr;</span>
            </a>"""

    def _update_index(self, article: dict, score: float, brand: str):
        bc = BRANDS[brand]
        webroot = bc["webroot"]
        result = subprocess.run(
            ["ssh", GCP_SSH_HOST, f"cat {webroot}/index.html"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.error("intel_deep_analysis: failed to download %s index.html", brand)
            return
        index_html = result.stdout
        card_html = self._generate_card_html(article, score)
        article_url = f"/analysis/{article['slug']}.html"
        if article_url in index_html:
            return
        marker = '<div class="analysis-grid reveal">'
        if marker not in index_html:
            log.warning("intel_deep_analysis: analysis-grid marker not found in %s index", brand)
            return
        pos = index_html.index(marker) + len(marker)
        index_html = index_html[:pos] + "\n" + card_html + "\n" + index_html[pos:]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(index_html)
            tmp = f.name
        try:
            subprocess.run(["scp", "-q", tmp, f"{GCP_SSH_HOST}:/tmp/_da_index.html"],
                           check=True, timeout=15)
            subprocess.run(["ssh", GCP_SSH_HOST,
                            f"sudo cp /tmp/_da_index.html {webroot}/index.html && "
                            f"sudo chown www-data:www-data {webroot}/index.html && "
                            f"rm /tmp/_da_index.html"],
                           check=True, timeout=15)
            log.info("intel_deep_analysis: updated %s index with new analysis card", brand)
        except subprocess.CalledProcessError as e:
            log.error("intel_deep_analysis: %s index update failed: %s", brand, e)
        finally:
            os.unlink(tmp)

    # ── Briefings Index (summary card linking to full analysis) ─────

    def _generate_briefing_summary(self, article: dict) -> str:
        """Generate a 2-3 sentence teaser summary for the briefings card."""
        from observers.llm import call_llm
        body = article.get("body", "")
        excerpt = body[:2000]
        prompt = (
            f"Write a 2-3 sentence teaser (40-60 words) for this analysis. "
            f"State the core development and one key implication. "
            f"Plain text, no markdown, no line breaks, British English.\n\n"
            f"TITLE: {article['title']}\n"
            f"ARTICLE EXCERPT:\n{excerpt}"
        )
        try:
            summary, _ = call_llm(
                system_prompt="Write 2-3 sentence intelligence teasers. Never exceed 60 words.",
                user_prompt=prompt, timeout=60, num_predict=150,
                temperature=0.3, preferred_backend="gemini",
                override_gemini_model="gemini-2.5-flash",
            )
            # Hard truncate: max 3 sentences, max 300 chars
            text = summary.strip().replace("\n", " ").replace("  ", " ")
            sentences = re.split(r'(?<=[.!?])\s+', text)
            if len(sentences) > 3:
                text = " ".join(sentences[:3])
            if len(text) > 300:
                text = text[:297].rsplit(" ", 1)[0] + "..."
            return text
        except Exception as e:
            log.warning("intel_deep_analysis: briefing summary generation failed: %s", e)
            return article.get("subtitle", "")

    def _generate_briefing_card_html(self, article: dict, score: float, summary: str) -> str:
        """Generate a summary briefing card for the /briefings/ index."""
        t = html_escape(article["title"])
        cat = html_escape(article["category"])
        d = html_escape(article["date"])
        summary_escaped = html_escape(summary)
        # Priority based on score
        if score >= 8.5:
            priority_class, priority_label = "priority-high", "High"
        elif score >= 7.5:
            priority_class, priority_label = "priority-elevated", "Elevated"
        else:
            priority_class, priority_label = "priority-standard", "Standard"
        return f"""        <a href="/analysis/{article['slug']}.html" class="briefing-card" data-category="{html_escape(article['category'].lower())}">
            <div class="briefing-card-date">{d}</div>
            <div class="briefing-card-body">
                <div class="briefing-card-meta">
                    <span class="briefing-card-badge">{cat}</span>
                    <span class="briefing-card-badge {priority_class}">{priority_label}</span>
                </div>
                <div class="briefing-card-title">{t}</div>
                <div class="briefing-card-summary">{summary_escaped}</div>
            </div>
            <div class="briefing-card-arrow">&rarr;</div>
        </a>"""

    def _update_briefings_index(self, article: dict, score: float, summary: str, brand: str):
        """Insert a summary card at the top of /briefings/index.html."""
        bc = BRANDS[brand]
        webroot = bc["webroot"]
        result = subprocess.run(
            ["ssh", GCP_SSH_HOST, f"cat {webroot}/briefings/index.html"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.warning("intel_deep_analysis: no briefings/index.html for %s, skipping", brand)
            return
        index_html = result.stdout
        card_html = self._generate_briefing_card_html(article, score, summary)
        article_url = f"/analysis/{article['slug']}.html"
        if article_url in index_html:
            return
        # Find the briefing list container — cards start after <div class="briefing-list">
        marker = '<div class="briefing-list">'
        if marker not in index_html:
            log.warning("intel_deep_analysis: briefing-list marker not found in %s briefings index", brand)
            return
        pos = index_html.index(marker) + len(marker)
        index_html = index_html[:pos] + "\n" + card_html + "\n" + index_html[pos:]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(index_html)
            tmp = f.name
        try:
            subprocess.run(["scp", "-q", tmp, f"{GCP_SSH_HOST}:/tmp/_da_briefings_index.html"],
                           check=True, timeout=15)
            subprocess.run(["ssh", GCP_SSH_HOST,
                            f"sudo cp /tmp/_da_briefings_index.html {webroot}/briefings/index.html && "
                            f"sudo chown www-data:www-data {webroot}/briefings/index.html && "
                            f"rm /tmp/_da_briefings_index.html"],
                           check=True, timeout=15)
            log.info("intel_deep_analysis: updated %s briefings index with summary card", brand)
        except subprocess.CalledProcessError as e:
            log.error("intel_deep_analysis: %s briefings index update failed: %s", brand, e)
        finally:
            os.unlink(tmp)

    # ── Deployment ───────────────────────────────────────────────────

    def _deploy(self, article: dict, html: str, brand: str) -> str:
        bc = BRANDS[brand]
        webroot = bc["webroot"]
        analysis_dir = f"{webroot}/analysis"
        filename = f"{article['slug']}.html"
        subprocess.run(
            ["ssh", GCP_SSH_HOST,
             f"sudo mkdir -p {analysis_dir} && sudo chown www-data:www-data {analysis_dir}"],
            capture_output=True, timeout=15,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html)
            tmp = f.name
        try:
            subprocess.run(["scp", "-q", tmp, f"{GCP_SSH_HOST}:/tmp/_da_article.html"],
                           check=True, timeout=15)
            subprocess.run(["ssh", GCP_SSH_HOST,
                            f"sudo cp /tmp/_da_article.html {analysis_dir}/{filename} && "
                            f"sudo chown www-data:www-data {analysis_dir}/{filename} && "
                            f"sudo chmod 644 {analysis_dir}/{filename} && "
                            f"rm /tmp/_da_article.html"],
                           check=True, timeout=15)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Deployment failed ({brand}): {e}")
        finally:
            os.unlink(tmp)
        url = f"{bc['site_url']}/analysis/{filename}"
        log.info("intel_deep_analysis: deployed %s", url)
        return url

    # ── Landing Page Feed ───────────────────────────────────────────

    def _update_landing_feed(self, all_published: list):
        """Update varangian.ai landing-page activity ticker feed (intel section)."""
        feed_path = "/var/www/varangian.ai/html/api/feed.json"
        try:
            r = subprocess.run(
                ["ssh", GCP_SSH_HOST, f"cat {feed_path}"],
                capture_output=True, text=True, timeout=10,
            )
            feed = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {
                "updated": "", "intel": [], "cyber": [],
            }
        except Exception:
            feed = {"updated": "", "intel": [], "cyber": []}

        now = datetime.now(timezone.utc).isoformat()
        for p in all_published:
            varangian_url = next(
                (u for u in p["urls"] if "varangian" in u), p["urls"][0] if p["urls"] else "",
            )
            if varangian_url:
                feed["intel"].insert(0, {
                    "title": p["topic"],
                    "domain": p["domain"],
                    "url": varangian_url,
                    "time": now,
                })
        feed["intel"] = feed["intel"][:10]
        feed["updated"] = now

        feed_json = json.dumps(feed, indent=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(feed_json)
            tmp = f.name
        try:
            subprocess.run(["scp", "-q", tmp, f"{GCP_SSH_HOST}:/tmp/_landing_feed.json"],
                           check=True, timeout=10)
            subprocess.run(["ssh", GCP_SSH_HOST,
                            f"sudo cp /tmp/_landing_feed.json {feed_path} && "
                            f"sudo chown www-data:www-data {feed_path} && "
                            f"rm /tmp/_landing_feed.json"],
                           check=True, timeout=10)
            log.info("intel_deep_analysis: landing feed updated (%d intel items)", len(feed["intel"]))
        except Exception as e:
            log.warning("intel_deep_analysis: landing feed update failed: %s", e)
        finally:
            os.unlink(tmp)

    # ── Observer Entry Point ─────────────────────────────────────────

    def run(self, ctx: ObserverContext) -> ObserverResult:
        start_time = time.time()

        # 1. Collect
        articles = self._collect_all()
        if len(articles) < 5:
            return ObserverResult(success=True, message="", data={"skipped": "insufficient articles"})

        # 2. Classify into 4 domains
        domains = self._classify_by_domain(articles)

        # 3. Council scores each domain
        qualified = self._council_score_domains(domains)
        if not qualified:
            return ObserverResult(success=True, message="",
                                 data={"skipped": "no domains above significance threshold"})

        # 4. Process each qualifying domain
        state = self._load_state()
        all_published = []

        for domain_result in qualified:
            domain = domain_result["domain"]
            topic = domain_result["topic"]
            score = domain_result["score"]
            topic_hash = self._event_hash(f"{domain}:{topic}")

            if topic_hash in state.get("published_hashes", []):
                log.info("intel_deep_analysis: %s '%s' already analysed, skipping", domain, topic)
                continue

            # 5. SearXNG enrichment
            enriched = self._fetch_searxng(f"{domain} {topic}")
            domain_articles = domain_result["articles"] + enriched

            # 6. Deep research
            context = "\n".join(f"- {a['title']}" for a in domain_articles[:15])
            research = self._deep_research(
                f"{domain}: {topic}", domain_result["angles"], context,
            )

            # 7. Generate, deploy for each brand
            published_urls = []
            for brand in ("puretensor", "varangian"):
                article = self._generate_article(
                    f"{domain}: {topic}", research, domain_result["angles"],
                    domain_articles, brand,
                )
                if not article or len(article.get("body", "")) < 3000:
                    log.warning("intel_deep_analysis: %s/%s article too short or failed", domain, brand)
                    continue

                # Force category to the domain
                article["category"] = domain

                html = self._generate_html(article, brand, score, domain_result["council_models"])
                try:
                    url = self._deploy(article, html, brand)
                    published_urls.append(url)
                except Exception as e:
                    log.error("intel_deep_analysis: deploy failed (%s/%s): %s", domain, brand, e)
                    continue

                try:
                    self._update_index(article, score, brand)
                except Exception as e:
                    log.error("intel_deep_analysis: index update failed (%s/%s): %s", domain, brand, e)

                # Generate briefing summary and update briefings index
                try:
                    summary = self._generate_briefing_summary(article)
                    self._update_briefings_index(article, score, summary, brand)
                except Exception as e:
                    log.error("intel_deep_analysis: briefings update failed (%s/%s): %s", domain, brand, e)

            if published_urls:
                state["published_hashes"].append(topic_hash)
                all_published.append({
                    "domain": domain, "topic": topic, "score": score,
                    "urls": published_urls,
                })

        if not all_published:
            return ObserverResult(success=True, message="",
                                 data={"skipped": "all domains either duplicate or failed"})

        # 8. Save state
        state["published_hashes"] = state["published_hashes"][-100:]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        # 8b. Update varangian.ai landing page feed
        try:
            self._update_landing_feed(all_published)
        except Exception as e:
            log.warning("intel_deep_analysis: landing feed update failed: %s", e)

        # 9. Notify
        elapsed = time.time() - start_time
        domain_list = "\n".join(
            f"  [{p['domain']}] {p['topic']} ({p['score']:.1f}/10)\n    " +
            "\n    ".join(p["urls"])
            for p in all_published
        )
        council_models = qualified[0]["council_models"] if qualified else []
        message = (
            f"Deep analysis published ({len(all_published)} domains):\n"
            f"{domain_list}\n"
            f"  Council: {', '.join(council_models)} | {elapsed:.0f}s pipeline"
        )
        self.send_telegram(message, token=ALERT_BOT_TOKEN)
        return ObserverResult(success=True, message=message, data={
            "domains_published": len(all_published),
            "details": all_published,
            "elapsed": round(elapsed, 1),
        })


# ── Standalone CLI ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="Intel Deep Analysis pipeline (standalone)")
    parser.add_argument("--collect-only", action="store_true", help="Test data collection only")
    parser.add_argument("--council-only", action="store_true", help="Collect + council scoring only")
    parser.add_argument("--dry-run", action="store_true", help="Full pipeline, no deploy")
    parser.add_argument("--topic", type=str, help="Force a specific topic for research")
    args = parser.parse_args()

    obs = IntelDeepAnalysisObserver()

    print("[collect] Fetching all data sources...")
    articles = obs._collect_all()
    print(f"  {len(articles)} unique articles collected")
    for a in articles[:8]:
        print(f"  - [{a['source']}] {a['title'][:70]}")

    if args.collect_only:
        _sys.exit(0)

    if args.topic:
        # Force a single domain/topic
        qualified = [{
            "domain": "Multi-Domain", "topic": args.topic, "score": 9.0,
            "angles": ["Strategic implications", "Key actors", "Future scenarios"],
            "articles": articles[:15], "council_models": ["forced"],
        }]
    else:
        print("\n[classify] Sorting articles into 4 domains...")
        domains = obs._classify_by_domain(articles)
        for d, data in domains.items():
            print(f"  - {d}: {len(data['articles'])} articles — {data.get('theme', '')[:60]}")

        print("\n[council] Running AI council on all domains (3 models in parallel)...")
        qualified = obs._council_score_domains(domains)
        if not qualified:
            print("  No domains above significance threshold. Exiting.")
            _sys.exit(0)

    for q in qualified:
        print(f"\n  QUALIFIED: {q['domain']} — {q['topic'][:60]} (score: {q['score']:.1f})")
    print(f"  Council: {', '.join(qualified[0]['council_models'])}")

    if args.council_only:
        _sys.exit(0)

    for domain_result in qualified:
        domain = domain_result["domain"]
        topic = domain_result["topic"]
        score = domain_result["score"]

        print(f"\n{'='*60}")
        print(f"[{domain}] {topic} (score: {score:.1f})")
        print(f"{'='*60}")

        print(f"[research] Running Gemini Deep Research (may take 2-6 min)...")
        context = "\n".join(f"- {a['title']}" for a in domain_result["articles"][:15])
        research = obs._deep_research(f"{domain}: {topic}", domain_result["angles"], context)
        if research:
            print(f"  Research complete: {len(research)} chars")
        else:
            print("  Deep research failed — will use fallback")

        for brand in ("puretensor", "varangian"):
            print(f"\n[write:{brand}] Generating article...")
            article = obs._generate_article(
                f"{domain}: {topic}", research, domain_result["angles"],
                domain_result["articles"], brand,
            )
            if not article:
                print(f"  Article generation failed for {brand}")
                continue
            article["category"] = domain
            print(f"  Title: {article['title']}")
            print(f"  Body: {len(article['body'])} chars")

            html = obs._generate_html(article, brand, score, domain_result["council_models"])
            print(f"  HTML: {len(html):,} bytes")

            if args.dry_run:
                out = Path.home() / f"intel_deep_analysis_preview_{domain.lower()}_{brand}.html"
                out.write_text(html)
                print(f"  [DRY RUN] Saved to {out}")
            else:
                print(f"  Deploying to {BRANDS[brand]['site_url']}...")
                url = obs._deploy(article, html, brand)
                print(f"  Deployed: {url}")
                obs._update_index(article, score, brand)
                summary = obs._generate_briefing_summary(article)
                obs._update_briefings_index(article, score, summary, brand)
                print(f"  Index + briefings updated")

    print("\nDone.")
