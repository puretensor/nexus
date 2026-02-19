#!/usr/bin/env python3
"""Intel Briefing Observer — auto-publishes intelligence briefings to intel.puretensor.ai.

Pulls from GDELT trending topics + curated RSS feeds, synthesises via local Ollama
(qwen3-235b-a22b-q4km), generates styled static HTML, and deploys to GCP e2-micro.

Schedule: every 4 hours (0 */4 * * *)
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
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

import sys as _sys
_nexus_root = str(Path(__file__).resolve().parent.parent)
if _nexus_root not in _sys.path:
    _sys.path.insert(0, _nexus_root)

from observers.base import ALERT_BOT_TOKEN, Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")

SCRIPT_DIR = Path(__file__).parent
STATE_DIR = SCRIPT_DIR / ".state"
RSS_CONF = Path(os.environ.get(
    "RSS_FEEDS_CONF",
    str(Path.home() / ".config" / "puretensor" / "rss_feeds.conf"),
))

# GCP deployment
GCP_SSH_HOST = "puretensorai@GCP_TAILSCALE_IP"
WEBROOT = "/var/www/intel.puretensor.ai"
BRIEFINGS_DIR = f"{WEBROOT}/briefings"

# Ollama config
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("INTEL_OLLAMA_MODEL", "qwen3-235b-a22b-q4km")

# GDELT API
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Google Analytics
GA_ID = "G-Q5RPQQ747Z"


class IntelBriefingObserver(Observer):
    """Generates and publishes intelligence briefings to intel.puretensor.ai."""

    name = "intel_briefing"
    schedule = "0 */6 * * *"  # every 6 hours

    # Maximum articles to fetch per RSS feed
    MAX_PER_FEED = 8
    # Maximum total articles to send to LLM
    MAX_ARTICLES_FOR_LLM = 60
    # Maximum GDELT articles
    MAX_GDELT = 20

    # State file tracks published briefing slugs to avoid duplicates
    STATE_FILE = Path(
        os.environ.get("OBSERVER_STATE_DIR", str(STATE_DIR))
    ) / "intel_briefing_published.json"

    # ── RSS Feed Parsing ─────────────────────────────────────────────────

    def _load_rss_feeds(self) -> dict[str, str]:
        """Load RSS feed URLs from rss_feeds.conf."""
        feeds = {}
        if not RSS_CONF.exists():
            log.warning("intel_briefing: RSS config not found at %s", RSS_CONF)
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
        """Fetch and parse a single RSS/Atom feed. Returns list of article dicts."""
        articles = []
        try:
            req = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "PureTensor-Intel/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
        except Exception as e:
            log.debug("intel_briefing: RSS fetch failed for %s: %s", feed_name, e)
            return []

        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            log.debug("intel_briefing: RSS parse failed for %s: %s", feed_name, e)
            return []

        # Handle both RSS 2.0 and Atom formats
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        # RSS 2.0: channel/item
        items = root.findall(".//item")
        if items:
            for item in items[: self.MAX_PER_FEED]:
                title = self._get_text(item, "title")
                desc = self._get_text(item, "description")
                link = self._get_text(item, "link")
                pub_date = self._get_text(item, "pubDate")
                if title:
                    articles.append({
                        "source": feed_name,
                        "title": title,
                        "summary": self._clean_html(desc)[:500] if desc else "",
                        "url": link,
                        "date": pub_date,
                    })
            return articles

        # Atom: feed/entry
        entries = root.findall("atom:entry", ns) or root.findall("entry")
        for entry in entries[: self.MAX_PER_FEED]:
            title = self._get_text(entry, "atom:title", ns) or self._get_text(entry, "title")
            summary_el = entry.find("atom:summary", ns)
            if summary_el is None:
                summary_el = entry.find("summary")
            content_el = entry.find("atom:content", ns)
            if content_el is None:
                content_el = entry.find("content")
            summary = ""
            if summary_el is not None and summary_el.text:
                summary = self._clean_html(summary_el.text)[:500]
            elif content_el is not None and content_el.text:
                summary = self._clean_html(content_el.text)[:500]

            link_el = entry.find("atom:link", ns)
            if link_el is None:
                link_el = entry.find("link")
            link = ""
            if link_el is not None:
                link = link_el.get("href", "")

            updated = self._get_text(entry, "atom:updated", ns) or self._get_text(entry, "updated") or ""

            if title:
                articles.append({
                    "source": feed_name,
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "date": updated,
                })
        return articles

    @staticmethod
    def _get_text(el, tag, ns=None):
        """Get text content of a child element."""
        child = el.find(tag, ns) if ns else el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    @staticmethod
    def _clean_html(text: str) -> str:
        """Strip HTML tags from text."""
        return re.sub(r"<[^>]+>", "", text).strip()

    def _fetch_all_rss(self) -> list[dict]:
        """Fetch articles from all configured RSS feeds."""
        feeds = self._load_rss_feeds()
        all_articles = []
        for name, url in feeds.items():
            articles = self._fetch_rss(name, url)
            all_articles.extend(articles)
            log.debug("intel_briefing: %s: %d articles", name, len(articles))
        return all_articles

    # ── GDELT ────────────────────────────────────────────────────────────

    def _fetch_gdelt_trending(self) -> list[dict]:
        """Fetch trending articles from GDELT across key topic areas."""
        queries = [
            "geopolitics conflict",
            "cybersecurity threat",
            "defence military",
            "sanctions trade policy",
            "AI technology semiconductor",
        ]
        all_articles = []
        for query in queries:
            try:
                params = urllib.parse.urlencode({
                    "query": query,
                    "mode": "artlist",
                    "maxrecords": 8,
                    "format": "json",
                    "sort": "DateDesc",
                    "timespan": "24h",
                })
                url = f"{GDELT_DOC_API}?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "PureTensor-Intel/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read())
                for art in data.get("articles", [])[:5]:
                    all_articles.append({
                        "source": f"GDELT ({query.split()[0]})",
                        "title": art.get("title", "(no title)"),
                        "summary": "",
                        "url": art.get("url", ""),
                        "date": art.get("seendate", ""),
                        "tone": art.get("tone", ""),
                    })
            except Exception as e:
                log.debug("intel_briefing: GDELT query '%s' failed: %s", query, e)

        return all_articles[:self.MAX_GDELT]

    # ── Ollama LLM Call ──────────────────────────────────────────────────

    SYSTEM_PROMPT = (
        "You are a senior intelligence analyst at PureTensor Intel. "
        "You produce concise, evidence-based strategic intelligence briefings. "
        "Your tone is analytical, dispassionate, and precise. "
        "You cite sources by name. You avoid speculation beyond what the evidence supports. "
        "You use British English spelling conventions. "
        "Do NOT use markdown formatting. Output clean plain text with section headers in UPPERCASE. "
        "Do NOT include any <think> tags or thinking process in your output."
    )

    def _call_llm(self, prompt: str, timeout: int = 360) -> tuple[str, str]:
        """Call LLM via shared module — Gemini-first to free up GPU for
        interactive work.  Uses gemini-2.5-flash (not lite) for better
        analytical writing quality.  Falls back to Ollama if Gemini fails.

        Returns:
            (content, backend_name) — the generated text and which backend was used.
        """
        from observers.llm import call_llm
        return call_llm(
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=prompt,
            timeout=timeout,
            num_predict=6144,
            temperature=0.4,
            preferred_backend="gemini",
            override_gemini_model="gemini-2.5-flash",
        )

    # ── Deduplication ────────────────────────────────────────────────────

    def _deduplicate_articles(self, articles: list[dict]) -> list[dict]:
        """Remove duplicate articles by title similarity."""
        seen_titles = set()
        unique = []
        for art in articles:
            # Normalise title for comparison
            norm = re.sub(r"[^a-z0-9 ]", "", art["title"].lower()).strip()
            # Use first 60 chars as key to catch near-duplicates
            key = norm[:60]
            if key not in seen_titles:
                seen_titles.add(key)
                unique.append(art)
        return unique

    # ── Briefing Generation ──────────────────────────────────────────────

    def _generate_briefing(self, rss_articles: list[dict], gdelt_articles: list[dict]) -> dict:
        """Generate a briefing from collected articles using Ollama."""
        # Deduplicate
        all_articles = self._deduplicate_articles(rss_articles + gdelt_articles)

        # Cap total
        if len(all_articles) > self.MAX_ARTICLES_FOR_LLM:
            all_articles = all_articles[:self.MAX_ARTICLES_FOR_LLM]

        if not all_articles:
            raise RuntimeError("No articles collected from any source")

        # Format articles for the prompt
        article_text = ""
        for i, art in enumerate(all_articles, 1):
            article_text += f"\n[{i}] {art['title']}"
            article_text += f"\n    Source: {art['source']}"
            if art.get("summary"):
                article_text += f"\n    Summary: {art['summary'][:300]}"
            if art.get("url"):
                article_text += f"\n    URL: {art['url']}"
            if art.get("tone"):
                article_text += f"\n    Tone: {art['tone']}"
            article_text += "\n"

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%d %B %Y")
        time_str = now.strftime("%H:%M UTC")

        prompt = f"""Analyse the following {len(all_articles)} news articles and intelligence signals collected over the past 24 hours. Produce a strategic intelligence briefing.

DATE: {date_str}, {time_str}

ARTICLES:
{article_text}

INSTRUCTIONS:
1. Write a briefing title (max 10 words) that captures the dominant strategic theme.
2. Write a one-sentence subtitle/summary (max 30 words).
3. Identify 4-6 key developments and group them into thematic sections.
4. For each section:
   - Write a section header (UPPERCASE, 2-5 words)
   - Write 2-4 paragraphs of analytical commentary (not just summaries — add strategic context, implications, and connections between events)
   - Reference specific sources by name
5. End with a KEY ASSESSMENTS section: 3-5 bullet-point forward-looking assessments with confidence indicators (HIGH/MEDIUM/LOW).
6. Total length: 1,500-2,500 words.

OUTPUT FORMAT (follow exactly):
TITLE: [your title]
SUBTITLE: [your subtitle]
CATEGORY: [one of: Geopolitical, Technology, Defence, Financial, Multi-Domain]

[section headers and body text]

KEY ASSESSMENTS
[bullet points with confidence levels]

Remember: analytical, dispassionate, evidence-based. British English. No markdown. No speculation beyond evidence."""

        log.info("intel_briefing: sending %d articles to LLM for analysis", len(all_articles))
        raw, self._last_backend = self._call_llm(prompt, timeout=360)
        log.info("intel_briefing: briefing generated via %s (%d chars)", self._last_backend, len(raw))

        # Parse the response
        title = "Strategic Intelligence Briefing"
        subtitle = "Multi-domain intelligence analysis and assessment."
        category = "Multi-Domain"

        lines = raw.split("\n")
        body_start = 0
        for i, line in enumerate(lines):
            if line.startswith("TITLE:"):
                title = line[6:].strip().strip('"').strip("'")
            elif line.startswith("SUBTITLE:"):
                subtitle = line[9:].strip().strip('"').strip("'")
            elif line.startswith("CATEGORY:"):
                category = line[9:].strip()
                body_start = i + 1
                break
            else:
                body_start = i
                break

        # Everything after the header metadata is the body
        body_text = "\n".join(lines[body_start:]).strip()

        # Generate slug from title
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
        date_prefix = now.strftime("%Y-%m-%d")
        slug = f"{date_prefix}-{slug}"

        return {
            "title": title,
            "subtitle": subtitle,
            "category": category,
            "body": body_text,
            "slug": slug,
            "date": date_str,
            "time": time_str,
            "datetime": now,
            "article_count": len(all_articles),
            "source_count": len(set(a["source"] for a in all_articles)),
            "backend": getattr(self, "_last_backend", "Ollama/qwen3-235b-a22b-q4km"),
        }

    # ── HTML Generation ──────────────────────────────────────────────────

    def _body_to_html(self, body: str) -> str:
        """Convert the LLM plain text body to styled HTML."""
        html_parts = []
        paragraphs = body.split("\n\n")

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Check for section headers (ALL CAPS lines)
            if re.match(r"^[A-Z][A-Z &:\-,/]{3,}$", para.split("\n")[0].strip()):
                header_line = para.split("\n")[0].strip()
                rest = "\n".join(para.split("\n")[1:]).strip()
                html_parts.append(f"            <h2>{html_escape(header_line)}</h2>")
                if rest:
                    for sub_para in rest.split("\n"):
                        sub_para = sub_para.strip()
                        if sub_para:
                            html_parts.append(f"            <p>{self._format_inline(sub_para)}</p>")
                continue

            # Check for KEY ASSESSMENTS section
            if para.upper().startswith("KEY ASSESSMENTS"):
                html_parts.append('            <div class="key-assessment">')
                html_parts.append('                <div class="key-assessment-label">Key Assessments</div>')
                lines = para.split("\n")[1:]
                for line in lines:
                    line = line.strip().lstrip("-*").strip()
                    if line:
                        # Highlight confidence levels
                        line = re.sub(
                            r"\b(HIGH|MEDIUM|LOW)\b",
                            r'<span style="color: var(--cyan); font-family: var(--font-mono); font-size: 0.8em; font-weight: 500;">\1</span>',
                            line,
                        )
                        html_parts.append(f"                <p>{self._format_inline(line)}</p>")
                html_parts.append("            </div>")
                continue

            # Bullet points
            if para.startswith("- ") or para.startswith("* "):
                html_parts.append("            <ul>")
                for line in para.split("\n"):
                    line = line.strip().lstrip("-*").strip()
                    if line:
                        line = re.sub(
                            r"\b(HIGH|MEDIUM|LOW)\b",
                            r'<span style="color: var(--cyan); font-family: var(--font-mono); font-size: 0.8em; font-weight: 500;">\1</span>',
                            line,
                        )
                        html_parts.append(f"                <li>{self._format_inline(line)}</li>")
                html_parts.append("            </ul>")
                continue

            # Regular paragraph — might contain bullet points within
            sub_lines = para.split("\n")
            in_list = False
            for line in sub_lines:
                stripped = line.strip()
                if stripped.startswith("- ") or stripped.startswith("* "):
                    if not in_list:
                        html_parts.append("            <ul>")
                        in_list = True
                    item = stripped.lstrip("-*").strip()
                    item = re.sub(
                        r"\b(HIGH|MEDIUM|LOW)\b",
                        r'<span style="color: var(--cyan); font-family: var(--font-mono); font-size: 0.8em; font-weight: 500;">\1</span>',
                        item,
                    )
                    html_parts.append(f"                <li>{self._format_inline(item)}</li>")
                else:
                    if in_list:
                        html_parts.append("            </ul>")
                        in_list = False
                    if stripped:
                        html_parts.append(f"            <p>{self._format_inline(stripped)}</p>")
            if in_list:
                html_parts.append("            </ul>")

        return "\n".join(html_parts)

    @staticmethod
    def _format_inline(text: str) -> str:
        """Apply inline formatting (bold, etc.) and escape HTML."""
        # Escape HTML first
        text = html_escape(text)
        # Bold: **text** or __text__
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
        return text

    def _generate_briefing_html(self, briefing: dict) -> str:
        """Generate the full HTML page for a briefing."""
        body_html = self._body_to_html(briefing["body"])
        title_escaped = html_escape(briefing["title"])
        subtitle_escaped = html_escape(briefing["subtitle"])
        category_escaped = html_escape(briefing["category"])
        date_escaped = html_escape(briefing["date"])
        time_escaped = html_escape(briefing["time"])
        article_count = briefing["article_count"]
        source_count = briefing["source_count"]

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google Analytics 4 -->
<script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag("js", new Date());
  gtag("config", "{GA_ID}");
</script>

    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title_escaped} | PureTensor // Intel</title>
    <meta name="description" content="{subtitle_escaped}">
    <meta name="theme-color" content="#0A0C10">
    <meta property="og:title" content="{title_escaped}">
    <meta property="og:description" content="{subtitle_escaped}">
    <meta property="og:type" content="article">
    <meta property="og:url" content="https://intel.puretensor.ai/briefings/{briefing['slug']}.html">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><polygon points='16,2 28,28 16,22 4,28' fill='%2300E5FF'/></svg>">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
    <style>
        :root {{ --bg-primary: #0A0C10; --bg-surface: #0F1218; --bg-elevated: #161B24; --cyan: #00E5FF; --cyan-bright: #7DE3FF; --cyan-dim: #0088A3; --cyan-border: rgba(0, 229, 255, 0.2); --cyan-glow: rgba(0, 229, 255, 0.06); --text-primary: #E6EAF0; --text-secondary: #8B95A5; --text-muted: #4A5568; --border: rgba(255, 255, 255, 0.06); --font-serif: 'Cormorant Garamond', Georgia, 'Times New Roman', serif; --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; --font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace; }}
        *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html {{ scroll-behavior: smooth; font-size: 16px; }}
        body {{ font-family: var(--font-sans); background: var(--bg-primary); color: var(--text-primary); line-height: 1.65; font-weight: 300; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; overflow-x: hidden; }}
        body::before {{ content: ''; position: fixed; inset: 0; opacity: 0.025; pointer-events: none; z-index: 9999; background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); background-size: 256px 256px; }}
        nav {{ position: fixed; top: 0; left: 0; right: 0; z-index: 1000; padding: 1.25rem 2rem; display: flex; align-items: center; justify-content: space-between; background: rgba(10, 12, 16, 0.85); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border-bottom: 1px solid var(--border); }}
        .nav-brand {{ font-family: var(--font-serif); font-weight: 600; font-size: 1.15rem; color: var(--cyan); letter-spacing: 0.15em; text-transform: uppercase; text-decoration: none; }}
        .nav-back {{ font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-secondary); text-decoration: none; letter-spacing: 0.1em; text-transform: uppercase; transition: color 0.2s ease; }}
        .nav-back:hover {{ color: var(--cyan); }}
        .container {{ max-width: 760px; margin: 0 auto; padding: 0 2rem; }}
        .article-header {{ padding-top: 8rem; padding-bottom: 3rem; border-bottom: 1px solid var(--border); margin-bottom: 3rem; }}
        .article-meta {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
        .article-badge {{ font-family: var(--font-mono); font-size: 0.6rem; font-weight: 500; color: var(--cyan); letter-spacing: 0.1em; text-transform: uppercase; padding: 0.2rem 0.6rem; border: 1px solid var(--cyan-border); background: rgba(0, 229, 255, 0.05); }}
        .article-badge--briefing {{ color: #FF6B35; border-color: rgba(255, 107, 53, 0.3); background: rgba(255, 107, 53, 0.05); }}
        .article-date {{ font-family: var(--font-mono); font-size: 0.7rem; color: var(--text-muted); letter-spacing: 0.05em; }}
        .article-title {{ font-family: var(--font-serif); font-weight: 700; font-size: clamp(2rem, 4vw, 3rem); line-height: 1.15; color: var(--text-primary); margin-bottom: 1.5rem; letter-spacing: -0.01em; }}
        .article-subtitle {{ font-size: 1.15rem; color: var(--text-secondary); line-height: 1.7; max-width: 640px; }}
        .briefing-meta {{ display: flex; gap: 2rem; margin-top: 1.5rem; flex-wrap: wrap; }}
        .briefing-meta-item {{ display: flex; flex-direction: column; gap: 0.2rem; }}
        .briefing-meta-value {{ font-family: var(--font-mono); font-size: 0.75rem; font-weight: 500; color: var(--cyan); }}
        .briefing-meta-label {{ font-family: var(--font-mono); font-size: 0.6rem; color: var(--text-muted); letter-spacing: 0.1em; text-transform: uppercase; }}
        .article-body {{ padding-bottom: 5rem; }}
        .article-body h2 {{ font-family: var(--font-serif); font-size: 1.65rem; font-weight: 600; color: var(--text-primary); margin-top: 3rem; margin-bottom: 1.25rem; line-height: 1.25; }}
        .article-body h3 {{ font-family: var(--font-serif); font-size: 1.3rem; font-weight: 600; color: var(--text-primary); margin-top: 2.5rem; margin-bottom: 1rem; line-height: 1.3; }}
        .article-body p {{ font-size: 1.05rem; color: var(--text-secondary); line-height: 1.85; margin-bottom: 1.5rem; }}
        .article-body strong {{ color: var(--text-primary); font-weight: 500; }}
        .article-body blockquote {{ border-left: 2px solid var(--cyan-dim); padding-left: 1.5rem; margin: 2rem 0; font-family: var(--font-serif); font-style: italic; font-size: 1.2rem; color: var(--text-primary); line-height: 1.6; }}
        .key-assessment {{ background: var(--bg-surface); border: 1px solid var(--border); border-left: 2px solid var(--cyan-dim); padding: 1.5rem 2rem; margin: 2.5rem 0; }}
        .key-assessment-label {{ font-family: var(--font-mono); font-size: 0.65rem; font-weight: 500; color: var(--cyan); letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 0.75rem; }}
        .key-assessment p {{ font-size: 0.95rem; color: var(--text-secondary); line-height: 1.7; margin-bottom: 0.75rem; }}
        .key-assessment p:last-child {{ margin-bottom: 0; }}
        .article-body ul {{ list-style: none; margin: 1.5rem 0; padding: 0; }}
        .article-body ul li {{ font-size: 1rem; color: var(--text-secondary); line-height: 1.7; padding-left: 1.5rem; margin-bottom: 0.75rem; position: relative; }}
        .article-body ul li::before {{ content: '\\25C6'; position: absolute; left: 0; color: var(--cyan-dim); font-size: 0.5rem; top: 0.4rem; }}
        .cyan-divider {{ width: 100%; height: 1px; background: linear-gradient(90deg, transparent, var(--cyan-border), transparent); }}
        .automated-notice {{ background: var(--bg-surface); border: 1px solid var(--border); padding: 1.25rem 1.5rem; margin: 2rem 0 0 0; font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted); line-height: 1.6; }}
        .automated-notice strong {{ color: var(--text-secondary); }}
        footer {{ border-top: 1px solid var(--border); padding: 2rem 0; }}
        .footer-inner {{ display: flex; justify-content: space-between; align-items: center; }}
        .footer-entity {{ font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-muted); letter-spacing: 0.08em; }}
        .footer-mark {{ font-family: var(--font-serif); font-size: 0.8rem; color: var(--text-muted); letter-spacing: 0.1em; }}
        .footer-parent {{ text-align: center; margin-bottom: 1.25rem; }}
        .footer-parent a {{ font-family: var(--font-mono); font-size: 0.65rem; color: var(--text-muted); text-decoration: none; letter-spacing: 0.1em; text-transform: uppercase; transition: color 0.2s ease; }}
        .footer-parent a:hover {{ color: var(--cyan); }}
        .footer-links {{ display: flex; justify-content: center; gap: 2rem; margin: 0.75rem 0; font-family: var(--font-mono); font-size: 0.7rem; letter-spacing: 0.05em; }}
        .footer-links a {{ color: var(--text-secondary); text-decoration: none; transition: color 0.2s; }}
        .footer-links a:hover {{ color: var(--cyan); }}
        @media (max-width: 768px) {{ nav {{ padding: 1rem 1.5rem; }} .container {{ padding: 0 1.5rem; }} .article-header {{ padding-top: 6rem; }} .briefing-meta {{ gap: 1.5rem; }} }}
    </style>
</head>
<body>

<nav>
    <a href="/" class="nav-brand">PureTensor // Intel</a>
    <a href="/" class="nav-back">&larr; All Analysis</a>
</nav>

<article>
    <header class="article-header">
        <div class="container">
            <div class="article-meta">
                <span class="article-badge article-badge--briefing">Briefing</span>
                <span class="article-badge">{category_escaped}</span>
                <span class="article-date">{date_escaped} &middot; {time_escaped}</span>
            </div>
            <h1 class="article-title">{title_escaped}</h1>
            <p class="article-subtitle">{subtitle_escaped}</p>
            <div class="briefing-meta">
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">{article_count}</span>
                    <span class="briefing-meta-label">Sources Analysed</span>
                </div>
                <div class="briefing-meta-item">
                    <span class="briefing-meta-value">{source_count}</span>
                    <span class="briefing-meta-label">Feed Channels</span>
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

            <div class="article-disclaimer" style="background: rgba(255, 180, 0, 0.06); border-left: 2px solid rgba(255, 180, 0, 0.5); padding: 1rem 1.5rem; margin: 0 0 2rem 0; border-radius: 0 6px 6px 0; font-family: var(--font-sans); font-size: 0.82rem; line-height: 1.6; color: var(--text-secondary);">
                <span style="font-family: var(--font-mono); font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.1em; color: rgba(255, 180, 0, 0.7); display: block; margin-bottom: 0.5rem;">Disclaimer</span>
                This analysis is provided for informational and educational purposes only and does not constitute investment, financial, legal, or professional advice. Content is AI-assisted and human-reviewed. See our full <a href="/disclaimer.html" style="color: var(--cyan-dim); text-decoration: underline; text-decoration-color: rgba(0, 229, 255, 0.3);">Disclaimer</a> for important limitations.
            </div>

{body_html}

            <div class="automated-notice">
                <strong>Automated Intelligence Briefing</strong> &mdash; This briefing was generated by the PureTensor Intel pipeline: open-source data fusion (GDELT + {source_count} RSS feeds), LLM inference ({html_escape(briefing.get('backend', 'Ollama/qwen3-235b-a22b-q4km'))}), structured analytical framework. {article_count} sources processed at {time_escaped} on {date_escaped}. All automated briefings are subject to editorial review.
            </div>

        </div>
    </div>
</article>

<div class="cyan-divider"></div>

<footer>
    <div class="container">
        <div class="footer-parent">
            <a href="https://www.puretensor.ai">A PureTensor Inc company</a>
        </div>
        <div class="footer-links">
            <a href="/disclaimer.html">Disclaimer</a>
            <a href="https://www.puretensor.ai/privacy.html">Privacy Policy</a>
            <a href="https://www.puretensor.ai/terms.html">Terms of Service</a>
        </div>
        <div class="footer-inner">
            <span class="footer-entity">PureTensor Inc &middot; United States</span>
            <span class="footer-mark">&copy; 2026</span>
        </div>
    </div>
</footer>

</body>
</html>"""

    # ── Index Page Update ────────────────────────────────────────────────

    def _generate_briefing_card_html(self, briefing: dict) -> str:
        """Generate an analysis card for the briefing index."""
        title_escaped = html_escape(briefing["title"])
        subtitle_escaped = html_escape(briefing["subtitle"])
        category_escaped = html_escape(briefing["category"])
        date_escaped = html_escape(briefing["date"])

        return f"""            <a href="/briefings/{briefing['slug']}.html" class="analysis-card">
                <div class="analysis-card-meta">
                    <span class="analysis-card-badge" style="color: #FF6B35; border-color: rgba(255, 107, 53, 0.3);">Briefing</span>
                    <span class="analysis-card-badge">{category_escaped}</span>
                    <span class="analysis-card-date">{date_escaped}</span>
                </div>
                <h3 class="analysis-card-title">{title_escaped}</h3>
                <p class="analysis-card-summary">{subtitle_escaped}</p>
                <span class="analysis-card-link">Read briefing &rarr;</span>
            </a>"""

    def _update_index(self, briefing: dict) -> None:
        """Download index.html, inject new briefing card, re-upload."""
        # Download current index.html
        result = subprocess.run(
            ["ssh", GCP_SSH_HOST, f"cat {WEBROOT}/index.html"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.error("intel_briefing: failed to download index.html: %s", result.stderr)
            return

        index_html = result.stdout
        card_html = self._generate_briefing_card_html(briefing)

        # Check if this briefing is already in the index (prevent duplicates)
        briefing_url = f"/briefings/{briefing['slug']}.html"
        if briefing_url in index_html:
            log.info("intel_briefing: briefing already in index.html, skipping update")
            return

        # Insert the new card at the top of the analysis grid
        marker = '<div class="analysis-grid reveal">'
        if marker in index_html:
            insert_point = index_html.index(marker) + len(marker)
            index_html = index_html[:insert_point] + "\n" + card_html + "\n" + index_html[insert_point:]
        else:
            log.warning("intel_briefing: could not find analysis-grid marker in index.html")
            return

        # Upload updated index.html
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(index_html)
            tmp_path = f.name

        try:
            subprocess.run(
                ["scp", "-q", tmp_path, f"{GCP_SSH_HOST}:/tmp/_intel_index.html"],
                check=True, timeout=15,
            )
            subprocess.run(
                ["ssh", GCP_SSH_HOST,
                 f"sudo cp /tmp/_intel_index.html {WEBROOT}/index.html && "
                 f"sudo chown www-data:www-data {WEBROOT}/index.html && "
                 f"rm /tmp/_intel_index.html"],
                check=True, timeout=15,
            )
            log.info("intel_briefing: updated index.html with new briefing card")
        except subprocess.CalledProcessError as e:
            log.error("intel_briefing: failed to update index.html: %s", e)
        finally:
            os.unlink(tmp_path)

    # ── Deployment ───────────────────────────────────────────────────────

    def _deploy_briefing(self, briefing: dict, html: str) -> str:
        """Deploy the briefing HTML to GCP e2-micro. Returns the URL."""
        filename = f"{briefing['slug']}.html"

        # Ensure briefings directory exists
        subprocess.run(
            ["ssh", GCP_SSH_HOST,
             f"sudo mkdir -p {BRIEFINGS_DIR} && "
             f"sudo chown www-data:www-data {BRIEFINGS_DIR}"],
            capture_output=True, timeout=15,
        )

        # Write HTML to temp file and SCP
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html)
            tmp_path = f.name

        try:
            subprocess.run(
                ["scp", "-q", tmp_path, f"{GCP_SSH_HOST}:/tmp/_intel_briefing.html"],
                check=True, timeout=15,
            )
            subprocess.run(
                ["ssh", GCP_SSH_HOST,
                 f"sudo cp /tmp/_intel_briefing.html {BRIEFINGS_DIR}/{filename} && "
                 f"sudo chown www-data:www-data {BRIEFINGS_DIR}/{filename} && "
                 f"sudo chmod 644 {BRIEFINGS_DIR}/{filename} && "
                 f"rm /tmp/_intel_briefing.html"],
                check=True, timeout=15,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"SCP deployment failed: {e}")
        finally:
            os.unlink(tmp_path)

        url = f"https://intel.puretensor.ai/briefings/{filename}"
        log.info("intel_briefing: deployed to %s", url)
        return url

    # ── State ────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load published briefing state."""
        if self.STATE_FILE.exists():
            try:
                return json.loads(self.STATE_FILE.read_text())
            except (json.JSONDecodeError, TypeError):
                return {"published": []}
        return {"published": []}

    def _save_state(self, state: dict) -> None:
        """Persist published briefing state."""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE_FILE.write_text(json.dumps(state, indent=2))

    # ── Observer Entry Point ─────────────────────────────────────────────

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Main observer: fetch data, generate briefing, deploy."""
        start_time = time.time()

        # 1. Fetch data sources
        try:
            rss_articles = self._fetch_all_rss()
            log.info("intel_briefing: fetched %d RSS articles", len(rss_articles))
        except Exception as e:
            log.error("intel_briefing: RSS fetch failed: %s", e)
            rss_articles = []

        try:
            gdelt_articles = self._fetch_gdelt_trending()
            log.info("intel_briefing: fetched %d GDELT articles", len(gdelt_articles))
        except Exception as e:
            log.error("intel_briefing: GDELT fetch failed: %s", e)
            gdelt_articles = []

        total_articles = len(rss_articles) + len(gdelt_articles)
        if total_articles < 5:
            msg = f"Only {total_articles} articles collected — skipping briefing"
            log.warning("intel_briefing: %s", msg)
            return ObserverResult(success=True, message="", data={"skipped": msg})

        # 2. Generate briefing via Ollama
        try:
            briefing = self._generate_briefing(rss_articles, gdelt_articles)
        except Exception as e:
            error_msg = f"Briefing generation failed: {e}"
            self.send_telegram(f"[intel_briefing] ERROR: {error_msg}", token=ALERT_BOT_TOKEN)
            return ObserverResult(success=False, error=error_msg)

        # 3. Generate HTML
        html = self._generate_briefing_html(briefing)

        # 4. Deploy to GCP
        try:
            url = self._deploy_briefing(briefing, html)
        except Exception as e:
            error_msg = f"Deployment failed: {e}"
            self.send_telegram(f"[intel_briefing] ERROR: {error_msg}", token=ALERT_BOT_TOKEN)
            return ObserverResult(success=False, error=error_msg)

        # 5. Update index.html
        try:
            self._update_index(briefing)
        except Exception as e:
            log.error("intel_briefing: index update failed (briefing still deployed): %s", e)

        # 6. Save state
        state = self._load_state()
        state["published"].append({
            "slug": briefing["slug"],
            "title": briefing["title"],
            "date": briefing["date"],
            "url": url,
        })
        # Keep last 100 entries
        state["published"] = state["published"][-100:]
        self._save_state(state)

        # 7. Report
        elapsed = time.time() - start_time
        backend_label = getattr(self, "_last_backend", "Unknown")
        message = (
            f"Intel briefing published:\n"
            f"  {briefing['title']}\n"
            f"  {url}\n"
            f"  Engine: {backend_label}\n"
            f"  {total_articles} sources | {elapsed:.0f}s pipeline"
        )
        self.send_telegram(message, token=ALERT_BOT_TOKEN)

        return ObserverResult(
            success=True,
            message=message,
            data={
                "title": briefing["title"],
                "url": url,
                "articles": total_articles,
                "elapsed_seconds": round(elapsed, 1),
            },
        )


# ── Standalone testing ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="Intel briefing observer (standalone)")
    parser.add_argument("--dry-run", action="store_true", help="Generate but don't deploy")
    parser.add_argument("--rss-only", action="store_true", help="Skip GDELT, RSS only")
    parser.add_argument("--skip-ollama", action="store_true", help="Test data fetch only")
    args = parser.parse_args()

    obs = IntelBriefingObserver()

    print("[intel_briefing] Fetching RSS feeds...")
    rss = obs._fetch_all_rss()
    print(f"  RSS: {len(rss)} articles from {len(obs._load_rss_feeds())} feeds")

    if not args.rss_only:
        print("[intel_briefing] Fetching GDELT trending...")
        gdelt = obs._fetch_gdelt_trending()
        print(f"  GDELT: {len(gdelt)} articles")
    else:
        gdelt = []

    if args.skip_ollama:
        print("[intel_briefing] Skipping Ollama (--skip-ollama)")
        print(f"  Total articles: {len(rss) + len(gdelt)}")
        # Print sample articles
        for art in (rss + gdelt)[:10]:
            print(f"  - [{art['source']}] {art['title'][:80]}")
        sys.exit(0)

    print("[intel_briefing] Generating briefing via LLM (Ollama → Gemini fallback)...")
    briefing = obs._generate_briefing(rss, gdelt)
    print(f"  Title: {briefing['title']}")
    print(f"  Backend: {briefing.get('backend', 'unknown')}")
    print(f"  Category: {briefing['category']}")
    print(f"  Slug: {briefing['slug']}")

    html = obs._generate_briefing_html(briefing)
    print(f"  HTML: {len(html):,} bytes")

    if args.dry_run:
        # Save locally for review
        out_path = Path.home() / "intel_briefing_preview.html"
        out_path.write_text(html)
        print(f"  [DRY RUN] Saved preview to {out_path}")
        print(f"  Body preview (first 500 chars):")
        print(f"  {briefing['body'][:500]}...")
    else:
        print("[intel_briefing] Deploying to GCP...")
        url = obs._deploy_briefing(briefing, html)
        print(f"  Deployed: {url}")

        print("[intel_briefing] Updating index.html...")
        obs._update_index(briefing)
        print("  Done.")
