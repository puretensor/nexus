#!/usr/bin/env python3
"""Daily Intelligence Snippet observer — fact-checked geopolitical briefing.

Runs at 8 AM weekdays via the Observer registry:
  1. Fetches today's headlines from major news RSS feeds
  2. Asks Claude to write a structured intelligence brief
  3. Fact-checks claims via Gemini with Google Search grounding
  4. Amends the brief to correct any errors
  5. Sends as HTML email to configured recipients

Claude is the reasoning engine. RSS provides the raw material.
Gemini with Google Search grounding provides fact-checking.
"""

import json
import logging
import os
import re
import smtplib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from observers.base import Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")


class DailySnippetObserver(Observer):
    """Fact-checked daily intelligence brief delivered by email."""

    name = "daily_snippet"
    schedule = "0 8 * * 1-5"  # 8 AM weekdays (UTC)

    # RSS feeds — broad coverage, no paywalls on RSS
    RSS_FEEDS = {
        "Reuters":        "https://news.google.com/rss/search?q=site:reuters.com+world&hl=en-US&gl=US&ceid=US:en",
        "AP News":        "https://news.google.com/rss/search?q=site:apnews.com+world&hl=en-US&gl=US&ceid=US:en",
        "BBC World":      "https://feeds.bbci.co.uk/news/world/rss.xml",
        "Bloomberg":      "https://feeds.bloomberg.com/markets/news.rss",
        "Guardian":       "https://www.theguardian.com/world/rss",
        "Politico EU":    "https://www.politico.eu/feed/",
        "NPR World":      "https://feeds.npr.org/1004/rss.xml",
        "SCMP":           "https://www.scmp.com/rss/91/feed",
        "Jerusalem Post": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx",
        "Foreign Policy": "https://foreignpolicy.com/feed/",
        "Economist":      "https://www.economist.com/international/rss.xml",
        "Al Jazeera":     "https://www.aljazeera.com/xml/rss/all.xml",
    }

    # Gemini fact-checking
    GEMINI_MODEL = "gemini-2.0-flash"
    GEMINI_API_URL = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    # -----------------------------------------------------------------------
    # Observer interface
    # -----------------------------------------------------------------------

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Execute the daily snippet pipeline.

        Returns an ObserverResult with a short Telegram summary on success.
        """
        date_str = ctx.now.strftime("%B %d, %Y")
        log.info("Daily Intelligence Snippet -- %s", date_str)

        # 1. Fetch headlines
        log.info("Fetching RSS feeds...")
        headlines = self.fetch_all_feeds()
        if not headlines:
            return ObserverResult(
                success=False, error="No headlines fetched from any RSS feed."
            )

        log.info("Total: %d headlines. Generating brief with Claude...", len(headlines))

        # 2. Generate brief
        prompt = self.build_prompt(headlines, date_str)
        brief = self.call_claude(prompt, timeout=600)
        if not brief:
            msg = "Claude returned empty response"
            self.send_telegram(f"[SNIPPET ERROR] {msg}")
            return ObserverResult(success=False, error=msg)

        # 3. Fact-check with Gemini
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            log.info("Running Gemini fact-check with Google Search grounding...")
            fact_results = self.fact_check_with_gemini(brief, gemini_key)

            if fact_results.get("issues"):
                log.info(
                    "Amending brief (%d issues)...", len(fact_results["issues"])
                )
                brief = self.amend_brief(brief, fact_results)
            else:
                log.info("Brief passed fact-check -- no amendments needed")
        else:
            log.warning("GEMINI_API_KEY not set -- skipping fact-check")

        # Clean up SKIP_QUOTE
        brief = brief.replace("SKIP_QUOTE", "").strip()

        # 4. Format and send email
        subject = f"Daily Intelligence Snippet -- {date_str}"
        html_content = self.brief_to_html(brief, date_str)
        sent = self.send_email(subject, html_content, brief)

        if sent:
            snippet_to = os.environ.get("SNIPPET_TO", "(unknown)")
            log.info("Email sent to %s", snippet_to)
            short = brief[:2000] + "..." if len(brief) > 2000 else brief
            self.send_telegram(f"[SNIPPET] Sent to recipients\n\n{short}")
            return ObserverResult(
                success=True,
                data={"headlines": len(headlines), "email_sent": True},
            )
        else:
            msg = "Email sending failed -- check SMTP config"
            self.send_telegram(f"[SNIPPET ERROR] {msg}")
            return ObserverResult(success=False, error=msg)

    # -----------------------------------------------------------------------
    # RSS fetching
    # -----------------------------------------------------------------------

    def fetch_rss(self, name: str, url: str) -> list[dict]:
        """Fetch and parse one RSS feed. Returns list of {title, summary, source}."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            root = ET.fromstring(resp.read())
        except Exception as e:
            log.warning("[%s] RSS fetch failed: %s", name, e)
            return []

        items = []
        # Standard RSS
        for item in root.findall(".//item"):
            title = item.find("title")
            desc = item.find("description")
            if title is not None and title.text:
                summary = ""
                if desc is not None and desc.text:
                    summary = re.sub(r"<[^>]+>", "", desc.text)[:300]
                items.append({
                    "title": title.text.strip(),
                    "summary": summary.strip(),
                    "source": name,
                })

        # Atom fallback
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns):
                title = entry.find("atom:title", ns)
                summary = entry.find("atom:summary", ns)
                if title is not None and title.text:
                    s = ""
                    if summary is not None and summary.text:
                        s = re.sub(r"<[^>]+>", "", summary.text)[:300]
                    items.append({
                        "title": title.text.strip(),
                        "summary": s.strip(),
                        "source": name,
                    })

        return items[:15]  # Cap per source

    def deduplicate_headlines(self, items: list[dict]) -> list[dict]:
        """Remove near-duplicate headlines by normalizing and comparing titles."""
        seen = set()
        unique = []
        for item in items:
            norm = re.sub(r"[^a-z0-9 ]", "", item["title"].lower())
            norm = re.sub(r"\s+", " ", norm).strip()
            key = norm[:60]
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def fetch_all_feeds(self) -> list[dict]:
        """Fetch all RSS feeds and return combined, deduplicated headline list."""
        all_items = []
        for name, url in self.RSS_FEEDS.items():
            items = self.fetch_rss(name, url)
            all_items.extend(items)
            log.info("  [%s] %d items", name, len(items))

        before = len(all_items)
        all_items = self.deduplicate_headlines(all_items)
        after = len(all_items)
        if before != after:
            log.info(
                "Deduplicated: %d -> %d headlines (%d duplicates removed)",
                before, after, before - after,
            )

        return all_items

    # -----------------------------------------------------------------------
    # Brief generation
    # -----------------------------------------------------------------------

    def build_prompt(self, headlines: list[dict], date_str: str) -> str:
        """Build the Claude prompt from today's headlines."""
        headline_text = ""
        for i, h in enumerate(headlines, 1):
            line = f"{i}. [{h['source']}] {h['title']}"
            if h["summary"]:
                line += f" -- {h['summary']}"
            headline_text += line + "\n"

        return f"""You are writing a daily intelligence brief for a small strategic advisory firm. Today is {date_str}.

Here are today's news headlines from major sources:

{headline_text}

Write a "Daily Intelligence Snippet". You MUST follow this EXACT output format -- do NOT deviate from the structure, headings, or prefixes shown below.

=== BEGIN FORMAT ===

ON THIS DAY: [2-3 sentences about a significant historical event on this calendar date. Connect briefly to today's geopolitical landscape.]

QUOTE: "[A real, verifiable quote from a statesman, strategist, or thinker relevant to today's top stories]" -- [Attribution with role/title]

AMERICAS

**[Topic headline, 5-10 words]**
[One sentence: what happened]
-> [One sentence: strategic implication]

**[Topic headline]**
[What happened]
-> [Strategic implication]

EUROPE

[Same format -- 2-4 stories per section]

MIDDLE EAST

[Same format]

ASIA-PACIFIC

[Same format]

GLOBAL

[Same format -- commodity markets, trade, climate, tech]

=== END FORMAT ===

STRICT FORMATTING RULES:
- Section headers must be EXACTLY: AMERICAS, EUROPE, MIDDLE EAST, ASIA-PACIFIC, GLOBAL -- on a line by themselves, no colons, no extra text
- Story headlines must use **bold** markers
- Strategic implications must start with -> on a new line
- "ON THIS DAY:" must be the literal prefix (not "Historical Anchor" or any variant)
- "QUOTE:" must be the literal prefix (not "QOTD" or any variant)

QUOTE GUIDELINES:
- Use quotes from statesmen, diplomats, strategists, military leaders, or serious thinkers
- The quote must be REAL and VERIFIABLE -- not fabricated
- If you cannot think of a genuinely relevant and real quote, output SKIP_QUOTE on a line by itself instead of the QUOTE section
- Do NOT use quotes that are inflammatory, hateful, or incite violence -- even if newsworthy

CONTENT RULES:
- Each news item must appear in EXACTLY ONE section. Never repeat.
- Place each item in its most relevant geographic region.
- If a story spans regions, pick where the primary action is.
- GLOBAL is for commodity markets, trade, climate, tech -- things without a single regional home.
- Skip any region with no significant news today.
- Keep the total brief under 800 words.
- Tone: analytical, detached, realpolitik. No moralizing.
- Do NOT invent news. Only use the headlines provided.
- Do NOT add items not in the headlines list."""

    # -----------------------------------------------------------------------
    # Gemini fact-checking
    # -----------------------------------------------------------------------

    def fact_check_with_gemini(self, brief_text: str, api_key: str) -> dict:
        """Verify factual claims in the brief using Gemini with Google Search grounding.

        Returns dict with 'corrections' (list), 'issues' (list), and 'raw_result' (str).
        """
        prompt = f"""You have access to Google Search. USE IT to verify FACTUAL CLAIMS in this intelligence brief.

WHAT TO CHECK (search for each):
- The QUOTE -- is this a real, documented, word-for-word quote? SEARCH for the exact quote text. If fabricated or paraphrased, mark INCORRECT.
- The ON THIS DAY section -- is the date, event, and every detail correct? SEARCH to verify.
- People's names, roles, and titles (SEARCH to verify current positions)
- Specific numbers, statistics, dollar amounts, vote counts (SEARCH to confirm)
- Specific events claimed to have happened (SEARCH to verify they occurred)

WHAT NOT TO CHECK:
- Lines starting with -> (these are strategic analysis/commentary, not factual claims)
- Subjective assessments or predictions
- General geopolitical framing

BRIEF TO VERIFY:
{brief_text}

Return ONLY a JSON array with verification results for FACTUAL CLAIMS ONLY:
[
  {{"claim": "exact claim text", "status": "VERIFIED", "correction": "", "source": "URL from search"}},
  {{"claim": "exact claim text", "status": "INCORRECT", "correction": "correct info found via search", "source": "URL"}}
]

IMPORTANT:
- You MUST use Google Search to verify each claim. Do not guess or say "unable to verify".
- Only include claims you actually searched for and found evidence about.
- The QUOTE is the highest priority -- fabricated quotes are unacceptable.
- Do NOT include analysis lines (->) in your results.
- If a claim cannot be found via search, omit it from results rather than marking it UNVERIFIABLE."""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.1},
        }

        url = f"{self.GEMINI_API_URL}?key={api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            result = json.loads(resp.read())
        except Exception as e:
            log.error("Gemini fact-check failed: %s", e)
            return {"corrections": [], "issues": [], "raw_result": f"ERROR: {e}"}

        # Extract text from response
        text = ""
        try:
            text = result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            log.error("Gemini returned unexpected format")
            return {"corrections": [], "issues": [], "raw_result": str(result)[:500]}

        # Parse JSON from response (may be wrapped in markdown code fences)
        json_text = text.strip()
        if json_text.startswith("```"):
            json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
            json_text = re.sub(r"\s*```$", "", json_text)

        corrections = []
        try:
            corrections = json.loads(json_text)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", json_text, re.DOTALL)
            if match:
                try:
                    corrections = json.loads(match.group())
                except json.JSONDecodeError:
                    log.warning("Could not parse fact-check JSON from Gemini")

        # Filter to only actionable problems
        issues = [c for c in corrections if c.get("status") in ("INCORRECT", "OUTDATED")]

        log.info(
            "Fact-check: %d claims checked, %d issues found",
            len(corrections), len(issues),
        )
        for issue in issues:
            log.info("  [%s] %s", issue.get("status"), issue.get("claim", "")[:80])
            if issue.get("correction"):
                log.info("    -> %s", issue.get("correction", "")[:100])

        return {"corrections": corrections, "issues": issues, "raw_result": text}

    def amend_brief(self, brief_text: str, fact_results: dict) -> str:
        """Use Claude to fix errors found in fact-checking."""
        issues = fact_results.get("issues", [])
        if not issues:
            log.info("No issues found -- brief passes fact-check")
            return brief_text

        issues_text = json.dumps(issues, indent=2)
        prompt = f"""Here is a daily intelligence brief and fact-check results showing errors.

BRIEF:
{brief_text}

FACT-CHECK ISSUES:
{issues_text}

Your task:
1. Fix claims marked INCORRECT -- use the correction provided
2. Fix claims marked OUTDATED -- update with current info
3. If a QUOTE is marked INCORRECT (fabricated/paraphrased), either replace it with a real, verifiable quote relevant to the day's news, or remove the QUOTE section entirely and replace with SKIP_QUOTE
4. If ON THIS DAY facts are wrong, fix them
5. Do NOT add new stories or change the structure
6. Maintain the exact same format and tone
7. Output ONLY the corrected brief -- no preamble, no commentary"""

        result = self.call_claude(prompt, timeout=600)
        if not result:
            log.warning("Amendment failed -- Claude returned empty response")
            return brief_text

        return result

    # -----------------------------------------------------------------------
    # HTML email formatting
    # -----------------------------------------------------------------------

    def brief_to_html(self, brief_text: str, date_str: str) -> str:
        """Convert the plain text brief to styled HTML email."""
        html_body = ""
        for line in brief_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Bold markers
            line_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)

            # Section headers
            if line.startswith("ON THIS DAY"):
                html_body += (
                    '<h2 style="font-size:19px; color:#1a1a1a; text-transform:uppercase; '
                    'letter-spacing:1px; margin-top:24px; border-bottom:1px solid #ccc; '
                    'padding-bottom:4px;">On This Day</h2>\n'
                )
                content = re.sub(r"^ON THIS DAY:?\s*", "", line)
                if content:
                    html_body += (
                        f'<p style="font-style:italic; color:#555; '
                        f'margin:8px 0 16px 0;">{content}</p>\n'
                    )
            elif line.startswith("QUOTE:") or line.startswith('"'):
                quote_text = re.sub(r"^QUOTE:?\s*", "", line)
                if "SKIP_QUOTE" not in quote_text:
                    html_body += (
                        f'<div style="background:#f5f5f5; padding:12px 16px; '
                        f'border-left:3px solid #333; margin:16px 0; '
                        f'font-style:italic;">{quote_text}</div>\n'
                    )
            elif line == "SKIP_QUOTE":
                pass
            elif line in ("AMERICAS", "EUROPE", "MIDDLE EAST", "ASIA-PACIFIC", "GLOBAL"):
                html_body += (
                    f'<h2 style="font-size:19px; color:#1a1a1a; text-transform:uppercase; '
                    f'letter-spacing:1px; margin-top:24px; border-bottom:1px solid #ccc; '
                    f'padding-bottom:4px;">{line}</h2>\n'
                )
            elif line.startswith("\u2192") or line.startswith("->"):
                arrow_text = re.sub(r"^(\u2192|->)\s*", "", line_html)
                html_body += (
                    f'<p style="margin:4px 0 12px 16px; color:#555; '
                    f'font-style:italic; font-size:15px;">\u2192 {arrow_text}</p>\n'
                )
            else:
                html_body += f'<p style="margin:4px 0 4px 0;">{line_html}</p>\n'

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Georgia, serif; font-size: 17px; line-height: 1.6; color: #333; max-width: 700px; margin: 0 auto; padding: 20px;">

  <h1 style="font-size: 26px; border-bottom: 2px solid #333; padding-bottom: 8px; margin-bottom: 4px;">Daily Intelligence Snippet</h1>
  <p style="color: #666; margin-bottom: 16px;">{date_str}</p>

  {html_body}

  <div style="margin-top: 30px; padding-top: 16px; border-top: 1px solid #ccc; font-size: 11px; color: #888;">
    Generated by HAL Claude | Sources: Reuters, AP, BBC, Bloomberg, Politico, Al Jazeera, SCMP, JPost, Foreign Policy, NPR, Economist
  </div>

</body>
</html>"""

    # -----------------------------------------------------------------------
    # Email sending
    # -----------------------------------------------------------------------

    def send_email(self, subject: str, html_content: str, plain_text: str) -> bool:
        """Send HTML email via SMTP. Config from environment variables."""
        host = os.environ.get("SNIPPET_SMTP_HOST", "mail.privateemail.com")
        port = int(os.environ.get("SNIPPET_SMTP_PORT", "587"))
        user = os.environ.get("SNIPPET_SMTP_USER", "")
        password = os.environ.get("SNIPPET_SMTP_PASS", "")
        from_addr = os.environ.get("SNIPPET_FROM", user)
        to_addrs = [
            a.strip()
            for a in os.environ.get("SNIPPET_TO", "").split(",")
            if a.strip()
        ]

        if not user or not password or not to_addrs:
            log.error("SMTP not configured -- set SNIPPET_SMTP_* env vars")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"REDACTED_NAME_SHORT <{from_addr}>"
        msg["To"] = ", ".join(to_addrs)

        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        try:
            with smtplib.SMTP(host, port) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())
            return True
        except Exception as e:
            log.error("SMTP error: %s", e)
            return False


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    # When running standalone, load .env from the nexus project root
    project_dir = Path(__file__).parent.parent
    env_path = project_dir / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    # Ensure config can be imported when running standalone
    sys.path.insert(0, str(project_dir))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    observer = DailySnippetObserver()
    ctx = ObserverContext()
    result = observer.run(ctx)

    if result.success:
        print(f"SUCCESS: {result.data}")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
