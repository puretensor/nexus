#!/usr/bin/env python3
"""Daily report compilation observer.

Runs at 01:00 UTC daily. Compiles the PREVIOUS day's CC session reports
and voice KB memos via Claude into a structured daily report, generates
a branded PDF, uploads to Google Drive, and sends a summary to Telegram.

Running at 01:00 UTC (after midnight) ensures all sessions from the
previous day have been synced, and avoids US afternoon API congestion
that caused the 2026-02-28 407-page fallback incident.

Data sources:
  - /sync/reports/cc/{YYYY-MM-DD}*.md  (rsync'd from tensor-core every 15 min)
  - /sync/voice-kb/kb/{YYYYMMDD}*.md   (rsync'd from tensor-core every 15 min)
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# Sync mount (hostPath from fox-n1)
SYNC_DIR = Path("/sync")
CC_REPORTS_DIR = SYNC_DIR / "reports" / "cc"
VOICE_KB_DIR = SYNC_DIR / "voice-kb" / "kb"

# Output directory (NFS PVC)
OUTPUT_DIR = Path("/output/daily")

# Google Drive folder ID for daily reports (set via env)
DRIVE_FOLDER_ID = os.environ.get("DRIVE_DAILY_REPORTS_FOLDER", "")
DRIVE_TOKEN_PATH = Path.home() / ".config" / "puretensor" / "gdrive_tokens" / "token_ops.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# PDF styling
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
DARK_BLUE = (26, 60, 110)
ACCENT_BLUE = (52, 103, 172)
BODY_GREY = (51, 51, 51)
META_GREY = (85, 85, 85)
LIGHT_GREY = (119, 119, 119)
TABLE_BG = (240, 244, 248)
WHITE = (255, 255, 255)


# ---------------------------------------------------------------------------
# DailyReport PDF class — ported from established gen_*.py template
# ---------------------------------------------------------------------------

def _make_daily_pdf_class(date_str: str):
    """Create a DailyReport FPDF subclass with proper header/footer.

    Uses a factory function because fpdf2 requires header/footer to be
    overridden via subclassing (monkey-patching breaks internal state).
    The date_str is captured in the closure.
    """
    from fpdf import FPDF

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%d %B %Y")
    except ValueError:
        date_display = date_str

    doc_title = f"Daily Report \u2014 {date_display}"

    class _PDF(FPDF):
        def __init__(self):
            super().__init__()
            self.add_font("DejaVu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"), uni=True)
            self.add_font("DejaVu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), uni=True)
            self.add_font("DejaVu", "I", os.path.join(FONT_DIR, "DejaVuSans-Oblique.ttf"), uni=True)
            self.add_font("DejaVu", "BI", os.path.join(FONT_DIR, "DejaVuSans-BoldOblique.ttf"), uni=True)
            self.set_auto_page_break(auto=True, margin=20)

        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("DejaVu", "", 7.5)
            self.set_text_color(*LIGHT_GREY)
            self.cell(0, 6, doc_title, ln=False)
            self.cell(0, 6, f"Page {self.page_no()}", align="R", ln=True)
            self.set_draw_color(*ACCENT_BLUE)
            self.set_line_width(0.4)
            self.line(10, self.get_y(), 200, self.get_y())
            self.ln(6)

        def footer(self):
            self.set_y(-15)
            self.set_draw_color(*TABLE_BG)
            self.set_line_width(0.3)
            self.line(10, self.get_y(), 200, self.get_y())
            self.set_font("DejaVu", "", 7.5)
            self.set_text_color(*LIGHT_GREY)
            self.cell(0, 10, f"PureTensor Inc  |  {date_display}  |  CONFIDENTIAL", align="C")

    return _PDF(), date_display


class DailyReport:
    """Branded PureTensor daily report PDF renderer.

    Provides semantic methods (h1, h2, h3, body, bullet, bold_bullet,
    table_header, table_row, cover_page) that produce consistent formatting
    matching the established manual report template.
    """

    def __init__(self, date_str: str):
        """Initialise with a date string in YYYY-MM-DD format."""
        self._pdf, self.date_display = _make_daily_pdf_class(date_str)

    # -- Delegate page management ------------------------------------------

    def add_page(self):
        self._pdf.add_page()

    def output(self, path: str):
        self._pdf.output(path)

    # -- Semantic rendering methods ----------------------------------------

    def h1(self, text: str):
        """Section heading — 18pt bold dark blue with accent underline."""
        self._pdf.ln(8)
        self._pdf.set_font("DejaVu", "B", 18)
        self._pdf.set_text_color(*DARK_BLUE)
        self._pdf.cell(0, 11, text, ln=True)
        self._pdf.set_draw_color(*ACCENT_BLUE)
        self._pdf.set_line_width(0.8)
        self._pdf.line(10, self._pdf.get_y() + 1, 200, self._pdf.get_y() + 1)
        self._pdf.ln(6)

    def h2(self, text: str):
        """Sub-heading — 13pt bold dark blue."""
        self._pdf.ln(5)
        self._pdf.set_font("DejaVu", "B", 13)
        self._pdf.set_text_color(*DARK_BLUE)
        self._pdf.cell(0, 8, text, ln=True)
        self._pdf.ln(3)

    def h3(self, text: str):
        """Minor heading — 11pt bold dark blue."""
        self._pdf.ln(3)
        self._pdf.set_font("DejaVu", "B", 11)
        self._pdf.set_text_color(*ACCENT_BLUE)
        self._pdf.cell(0, 7, text, ln=True)
        self._pdf.ln(2)

    def body(self, text: str):
        """Body paragraph — 10pt body grey, multi_cell with improved line spacing."""
        self._pdf.set_font("DejaVu", "", 10)
        self._pdf.set_text_color(*BODY_GREY)
        self._pdf.multi_cell(0, 6, text)
        self._pdf.ln(2)

    def bullet(self, text: str, indent: int = 0):
        """Simple bullet point with optional indent level."""
        x_offset = 10 + (indent * 8)
        self._pdf.set_font("DejaVu", "", 10)
        self._pdf.set_text_color(*BODY_GREY)
        self._pdf.set_x(x_offset)
        self._pdf.cell(6, 6, "\u2022")
        self._pdf.multi_cell(0, 6, text)
        self._pdf.ln(1)

    def bold_bullet(self, label: str, text: str):
        """Bullet with bold label followed by normal text."""
        self._pdf.set_text_color(*BODY_GREY)
        self._pdf.set_font("DejaVu", "", 10)
        self._pdf.cell(6, 6, "\u2022")
        self._pdf.set_font("DejaVu", "B", 10)
        label_w = self._pdf.get_string_width(label + " ")
        self._pdf.cell(label_w, 6, label + " ")
        self._pdf.set_font("DejaVu", "", 10)
        self._pdf.multi_cell(0, 6, text)
        self._pdf.ln(1)

    def table_header(self, cols: list[str], widths: list[int]):
        """Dark blue header row for a table with rounded feel."""
        self._pdf.set_font("DejaVu", "B", 9)
        self._pdf.set_fill_color(*DARK_BLUE)
        self._pdf.set_text_color(*WHITE)
        self._pdf.set_draw_color(*DARK_BLUE)
        for i, col in enumerate(cols):
            self._pdf.cell(widths[i], 8, f"  {col}", border=1, fill=True)
        self._pdf.ln()

    def table_row(self, cols: list[str], widths: list[int], alt: bool = False):
        """Table data row with alternating background and text wrapping."""
        self._pdf.set_font("DejaVu", "", 9)
        self._pdf.set_text_color(*BODY_GREY)
        self._pdf.set_draw_color(*TABLE_BG)
        if alt:
            self._pdf.set_fill_color(*TABLE_BG)
        else:
            self._pdf.set_fill_color(*WHITE)
        # Calculate max row height needed for text wrapping
        max_lines = 1
        for i, col in enumerate(cols):
            text_w = self._pdf.get_string_width(col)
            cell_w = widths[i] - 4  # padding
            if cell_w > 0 and text_w > cell_w:
                max_lines = max(max_lines, int(text_w / cell_w) + 1)
        row_h = max(7, 7 * max_lines)
        for i, col in enumerate(cols):
            self._pdf.cell(widths[i], row_h, f"  {col}", border=1, fill=True)
        self._pdf.ln()

    def cover_page(self, subtitle: str, session_count: int, memo_count: int):
        """Render the established cover page layout with accent lines."""
        self._pdf.add_page()

        # Corporate header
        self._pdf.set_font("DejaVu", "", 9)
        self._pdf.set_text_color(*DARK_BLUE)
        self._pdf.cell(0, 5, "PureTensor Inc", align="R", ln=True)
        self._pdf.cell(0, 5, "131 Continental Dr, Suite 305", align="R", ln=True)
        self._pdf.cell(0, 5, "Newark, DE 19713, US", align="R", ln=True)
        self._pdf.ln(35)

        # Top accent line
        self._pdf.set_draw_color(*ACCENT_BLUE)
        self._pdf.set_line_width(1.5)
        self._pdf.line(60, self._pdf.get_y(), 150, self._pdf.get_y())
        self._pdf.ln(10)

        # Title — 36pt "Daily Report"
        self._pdf.set_font("DejaVu", "B", 36)
        self._pdf.set_text_color(*DARK_BLUE)
        self._pdf.cell(0, 18, "Daily Report", align="C", ln=True)
        self._pdf.ln(6)

        # Date
        self._pdf.set_font("DejaVu", "", 18)
        self._pdf.set_text_color(*META_GREY)
        self._pdf.cell(0, 12, self.date_display, align="C", ln=True)
        self._pdf.ln(6)

        # Bottom accent line
        self._pdf.set_draw_color(*ACCENT_BLUE)
        self._pdf.set_line_width(1.5)
        self._pdf.line(60, self._pdf.get_y(), 150, self._pdf.get_y())
        self._pdf.ln(10)

        # Thematic subtitle
        if subtitle:
            self._pdf.set_font("DejaVu", "I", 13)
            self._pdf.set_text_color(*META_GREY)
            self._pdf.cell(0, 8, subtitle, align="C", ln=True)
        self._pdf.ln(30)

        # Stats + meta
        self._pdf.set_font("DejaVu", "", 10)
        self._pdf.set_text_color(*LIGHT_GREY)
        gen_time = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
        self._pdf.cell(0, 7, f"Generated: {gen_time}", align="C", ln=True)
        self._pdf.ln(2)
        self._pdf.cell(
            0, 7,
            "Primary Node: tensor-core (AMD TR PRO 9975WX, 512 GB, 2\u00d7 RTX PRO 6000)",
            align="C", ln=True,
        )
        self._pdf.cell(
            0, 7,
            f"Sessions: {session_count}  |  Voice Memos: {memo_count}",
            align="C", ln=True,
        )
        self._pdf.ln(10)
        self._pdf.set_font("DejaVu", "B", 10)
        self._pdf.set_text_color(*DARK_BLUE)
        self._pdf.cell(0, 7, "CONFIDENTIAL", align="C", ln=True)


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------

class DailyReportObserver(Observer):
    """Compiles daily CC reports + voice memos into a branded PDF report."""

    name = "daily_report"
    schedule = "0 1 * * *"  # 01:00 UTC daily — compiles PREVIOUS day's report

    # Retry config for Claude API
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 10  # seconds, doubles each attempt

    # Content limits for fallback collation
    MAX_SESSION_CHARS_FALLBACK = 1500  # per session in raw collation
    MAX_TOTAL_CHARS_FALLBACK = 40000  # total collation text

    # -- Data gathering --------------------------------------------------------

    def gather_cc_reports(self, date_str: str) -> list[dict]:
        """Read all CC session reports for the given date.

        Args:
            date_str: Date in YYYY-MM-DD format.

        Returns:
            List of dicts with 'topic', 'filename', and 'content' keys.
        """
        reports = []
        if not CC_REPORTS_DIR.exists():
            log.warning("CC reports dir not found: %s", CC_REPORTS_DIR)
            return reports

        for path in sorted(CC_REPORTS_DIR.glob(f"{date_str}*.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                # Extract topic from filename: YYYY-MM-DD_HH-MM_topic-slug.md
                name = path.stem  # e.g. 2026-02-28_14-30_infra-audit
                parts = name.split("_", 2)
                topic = parts[2].replace("-", " ").replace("_", " ") if len(parts) > 2 else name
                reports.append({
                    "topic": topic,
                    "filename": path.name,
                    "content": content,
                })
            except Exception as e:
                log.warning("Failed to read CC report %s: %s", path.name, e)

        return reports

    def gather_voice_memos(self, date_str: str) -> list[dict]:
        """Read all voice KB memos for the given date.

        Args:
            date_str: Date in YYYY-MM-DD format (converted to YYYYMMDD for matching).

        Returns:
            List of dicts with 'timestamp', 'filename', 'content', and optional 'summary'/'topics'.
        """
        memos = []
        if not VOICE_KB_DIR.exists():
            log.warning("Voice KB dir not found: %s", VOICE_KB_DIR)
            return memos

        compact_date = date_str.replace("-", "")  # YYYYMMDD

        for path in sorted(VOICE_KB_DIR.glob(f"{compact_date}*.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                # Extract timestamp from filename: YYYYMMDD_HHMMSS.md
                name = path.stem
                timestamp = name[9:15] if len(name) >= 15 else ""
                if timestamp:
                    timestamp = f"{timestamp[:2]}:{timestamp[2:4]}:{timestamp[4:6]}"

                # Parse YAML frontmatter if present
                summary = ""
                topics = []
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        frontmatter = content[3:end]
                        for line in frontmatter.split("\n"):
                            line = line.strip()
                            if line.startswith("summary:"):
                                summary = line[8:].strip().strip('"').strip("'")
                            elif line.startswith("topics:"):
                                # Simple inline list: topics: [a, b, c]
                                match = re.search(r'\[(.+?)\]', line)
                                if match:
                                    topics = [t.strip().strip('"').strip("'")
                                              for t in match.group(1).split(",")]

                memos.append({
                    "timestamp": timestamp,
                    "filename": path.name,
                    "content": content,
                    "summary": summary,
                    "topics": topics,
                })
            except Exception as e:
                log.warning("Failed to read voice memo %s: %s", path.name, e)

        return memos

    # -- Synthesis -------------------------------------------------------------

    def _deduplicate_reports(self, cc_reports: list[dict]) -> list[dict]:
        """Deduplicate sessions on the same topic, keeping the longest version."""
        by_topic: dict[str, dict] = {}
        for r in cc_reports:
            key = re.sub(r'\d+', '', r["topic"].lower()).strip()
            key = re.sub(r'\s+', ' ', key)
            if key in by_topic:
                if len(r["content"]) > len(by_topic[key]["content"]):
                    by_topic[key] = r
            else:
                by_topic[key] = r

        deduped = list(by_topic.values())
        if len(deduped) < len(cc_reports):
            log.info("Deduplicated %d sessions -> %d unique topics",
                     len(cc_reports), len(deduped))
        return deduped

    def _build_json_prompt(self, cc_reports: list[dict], voice_memos: list[dict],
                           date_str: str) -> str:
        """Build the Claude prompt requesting structured JSON output."""
        parts = [
            f"Date: {date_str}. Below are all CC session reports and voice memos "
            "from this day's work. Synthesize them into a structured daily report.\n",
        ]

        if cc_reports:
            parts.append(f"== CC SESSION REPORTS ({len(cc_reports)} sessions) ==\n")
            for i, r in enumerate(cc_reports, 1):
                parts.append(f"--- Session {i}: {r['topic']} ({r['filename']}) ---")
                content = r["content"]
                if len(content) > 8000:
                    content = content[:8000] + "\n\n[... truncated ...]"
                parts.append(content)
                parts.append("")

        if voice_memos:
            parts.append(f"\n== VOICE MEMOS ({len(voice_memos)} memos) ==\n")
            for i, m in enumerate(voice_memos, 1):
                header = f"--- Memo {i}: {m['timestamp']}"
                if m.get("summary"):
                    header += f" \u2014 {m['summary']}"
                header += f" ({m['filename']}) ---"
                parts.append(header)
                parts.append(m["content"])
                parts.append("")

        parts.append("""
Return a JSON object with EXACTLY this structure (no markdown fences, just raw JSON):

{
  "executive_summary": "2-3 sentence summary of the day's work",
  "subtitle": "Short thematic line, e.g. 'Infrastructure, Security & Commerce'",
  "activities": [
    {
      "theme": "THEME NAME — Short Description",
      "paragraphs": ["Paragraph 1...", "Paragraph 2..."],
      "sub_sections": [
        {"title": "Sub-heading", "paragraphs": ["..."], "bullets": ["..."]}
      ]
    }
  ],
  "key_decisions": ["Decision 1...", "Decision 2..."],
  "files_modified": [
    {"area": "Email/DNS", "files": "Yggdrasil UFW, Postfix relay_domains"}
  ],
  "infrastructure_changes": [
    {"node": "Yggdrasil", "change": "UFW rule added: 25/tcp ALLOW"}
  ],
  "fleet_state": [
    {"label": "UP", "value": "tensor-core, fox-n0, fox-n1, arx1-4, mon1-3"}
  ],
  "voice_memo_highlights": ["Highlight 1...", "Highlight 2..."],
  "unresolved_issues": [
    {"id": 1, "description": "Issue text..."}
  ],
  "next_steps": {
    "categories": [
      {"name": "Infrastructure", "items": ["Item 1...", "Item 2..."]}
    ]
  }
}

Rules:
- Group related sessions into thematic activities (don't make one activity per session)
- Be concise but thorough — each paragraph should be substantive, not filler
- Include concrete details: numbers, file paths, node names, metrics
- If there are no voice memos, set voice_memo_highlights to an empty array
- If a section has no data, use an empty array
- fleet_state should reflect the end-of-day state of the infrastructure
- files_modified should list actual files/configs changed, grouped by area
- infrastructure_changes should list node-level changes (new services, config changes, etc.)
- Return ONLY the JSON object, no explanation text before or after
""")

        return "\n".join(parts)

    def _parse_json_response(self, response: str) -> dict | None:
        """Try to parse Claude's response as JSON.

        Handles common issues: markdown fences, leading/trailing text.
        Returns None if parsing fails.
        """
        text = response.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = text.find("\n")
            if first_newline > 0:
                text = text[first_newline + 1:]
            # Remove closing fence
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()

        # Try to find JSON object boundaries
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "executive_summary" in data:
                return data
            log.warning("JSON parsed but missing expected keys")
            return None
        except json.JSONDecodeError as e:
            log.warning("JSON parse failed: %s", e)
            return None

    # LLM providers for synthesis — tried in order: Bedrock (Claude) → DeepSeek → xAI
    OPENAI_COMPAT_PROVIDERS = [
        {
            "name": "DeepSeek",
            "env_key": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        {
            "name": "xAI Grok",
            "env_key": "XAI_API_KEY",
            "base_url": "https://api.x.ai/v1",
            "model": "grok-3-mini",
        },
    ]

    SYSTEM_PROMPT = (
        "You are a technical report synthesizer for PureTensor Inc. "
        "You produce structured JSON daily reports from raw session logs "
        "and voice memos. Return ONLY valid JSON, no markdown fences, "
        "no explanation text."
    )

    def _call_bedrock(self, prompt: str, timeout: int = 180) -> str:
        """Call AWS Bedrock Claude Sonnet 4.6 for synthesis.

        Uses boto3 converse API. Returns the response text.
        Raises on failure (missing creds, API errors, etc.).
        """
        import boto3

        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        if not access_key or not secret_key:
            raise ValueError("Bedrock: AWS credentials not set")

        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

        response = client.converse(
            modelId="us.anthropic.claude-sonnet-4-6",
            system=[{"text": self.SYSTEM_PROMPT}],
            messages=[
                {"role": "user", "content": [{"text": prompt}]},
            ],
            inferenceConfig={
                "temperature": 0.3,
                "maxTokens": 8192,
            },
        )

        # Extract text from converse response
        output = response.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])
        texts = [b["text"] for b in content_blocks if "text" in b]
        return "\n".join(texts)

    def _call_openai_compat(self, prompt: str, provider: dict, timeout: int = 180) -> str:
        """Call an OpenAI-compatible API endpoint for synthesis.

        Uses the openai SDK with custom base_url. Returns the response text.
        Raises on failure.
        """
        from openai import OpenAI

        api_key = os.environ.get(provider["env_key"], "")
        if not api_key:
            raise ValueError(f"{provider['name']}: {provider['env_key']} not set")

        client = OpenAI(
            api_key=api_key,
            base_url=provider["base_url"],
            timeout=timeout,
        )

        response = client.chat.completions.create(
            model=provider["model"],
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=8192,
        )

        return response.choices[0].message.content or ""

    def synthesize_report(self, cc_reports: list[dict], voice_memos: list[dict],
                          date_str: str) -> dict | str:
        """Synthesize a structured daily report using available LLM providers.

        Provider order: Bedrock (Claude Sonnet 4.6) -> DeepSeek -> xAI Grok.
        Returns a dict (structured JSON) on success, or a str (plain text
        from raw collation fallback) if all providers fail.
        """
        cc_reports = self._deduplicate_reports(cc_reports)
        prompt = self._build_json_prompt(cc_reports, voice_memos, date_str)

        last_error = None

        # 1. Try AWS Bedrock (Claude Sonnet 4.6) first
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                log.info("Bedrock (Claude Sonnet 4.6) synthesis attempt %d/%d",
                         attempt, self.MAX_RETRIES)
                result = self._call_bedrock(prompt)
                if result and len(result.strip()) > 100:
                    parsed = self._parse_json_response(result)
                    if parsed:
                        log.info("Bedrock returned valid structured JSON")
                        return parsed
                    log.warning("Bedrock response not valid JSON on attempt %d", attempt)
                    last_error = "Bedrock: invalid JSON"
                else:
                    log.warning("Bedrock returned insufficient content (%d chars)",
                                len(result) if result else 0)
                    last_error = "Bedrock: insufficient content"
            except Exception as e:
                last_error = f"Bedrock: {e}"
                log.warning("Bedrock attempt %d failed: %s", attempt, e)

            if attempt < self.MAX_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.info("Retrying Bedrock in %d seconds...", delay)
                time.sleep(delay)

        log.warning("All %d Bedrock attempts failed, trying fallback providers",
                    self.MAX_RETRIES)

        # 2. Try OpenAI-compatible fallbacks (DeepSeek, xAI)
        for provider in self.OPENAI_COMPAT_PROVIDERS:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    log.info("%s synthesis attempt %d/%d",
                             provider["name"], attempt, self.MAX_RETRIES)
                    result = self._call_openai_compat(prompt, provider)
                    if result and len(result.strip()) > 100:
                        parsed = self._parse_json_response(result)
                        if parsed:
                            log.info("%s returned valid structured JSON", provider["name"])
                            return parsed
                        log.warning("%s response not valid JSON on attempt %d",
                                    provider["name"], attempt)
                        last_error = f"{provider['name']}: invalid JSON"
                    else:
                        log.warning("%s returned insufficient content (%d chars)",
                                    provider["name"], len(result) if result else 0)
                        last_error = f"{provider['name']}: insufficient content"
                except Exception as e:
                    last_error = f"{provider['name']}: {e}"
                    log.warning("%s attempt %d failed: %s", provider["name"], attempt, e)

                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.info("Retrying %s in %d seconds...", provider["name"], delay)
                    time.sleep(delay)

            log.warning("All %d %s attempts failed, trying next provider",
                        self.MAX_RETRIES, provider["name"])

        log.warning("All providers failed (last: %s) \u2014 using smart collation", last_error)
        return self._raw_collation(cc_reports, voice_memos, date_str)

    @staticmethod
    def _extract_session_summary(content: str, max_chars: int) -> str:
        """Extract the most useful parts of a session report for fallback collation."""
        priority_headers = [
            r'(?:^|\n)#+\s*(Objective|Purpose|Goal)',
            r'(?:^|\n)#+\s*(Results?|Results?\s+Summary|Summary|Overall\s+Summary)',
            r'(?:^|\n)#+\s*(Issues?\s+(?:Found|Resolved|Encountered)|Issues?\s+&)',
            r'(?:^|\n)#+\s*(Next\s+Steps?|Recommendations?|Action\s+Items?)',
            r'(?:^|\n)#+\s*(Key\s+(?:Findings?|Results?|Decisions?))',
            r'(?:^|\n)\*\*(?:Objective|Results?|Summary|Issues?|Next Steps?)[\s:*]',
        ]

        extracted_parts = []

        lines = content.split("\n")
        header_lines = []
        for line in lines[:20]:
            stripped = line.strip()
            if stripped.startswith(("- **", "* **", "- Date", "- Node", "- Objective",
                                    "# ", "## ")):
                header_lines.append(stripped)
            elif stripped and not stripped.startswith("```"):
                header_lines.append(stripped)
            if len("\n".join(header_lines)) > max_chars // 3:
                break
        if header_lines:
            extracted_parts.append("\n".join(header_lines))

        already_extracted = "\n".join(extracted_parts)
        for pattern in priority_headers:
            matches = list(re.finditer(pattern, content, re.IGNORECASE))
            for match in matches:
                start = match.start()
                next_heading = re.search(r'\n#{1,3}\s', content[start + 1:])
                if next_heading:
                    end = start + 1 + next_heading.start()
                else:
                    end = min(start + max_chars // 2, len(content))
                section = content[start:end].strip()
                # Skip if the section's body text already appears in extracted parts
                # (handles the case where header block already captured this content)
                section_body = re.sub(r'^#+\s+\S+\s*\n?', '', section).strip()[:80]
                if section and section_body and section_body not in already_extracted:
                    extracted_parts.append(section)
                    already_extracted = "\n".join(extracted_parts)

        result = "\n\n".join(extracted_parts)

        if len(result) > 100:
            if len(result) > max_chars:
                result = result[:max_chars] + "\n[... truncated ...]"
            return result

        if len(content) > max_chars:
            return content[:max_chars] + "\n[... truncated ...]"
        return content

    # Character limits for voice memos in fallback collation
    MAX_MEMO_CHARS_FALLBACK = 400  # per memo — summary + transcript only
    MAX_TOTAL_MEMO_CHARS_FALLBACK = 15000  # total voice memos section

    @staticmethod
    def _extract_memo_essence(content: str, max_chars: int) -> str:
        """Extract only the useful parts of a voice memo (summary + transcript).

        Strips YAML frontmatter and verbose metadata fields, keeping only
        the human-readable summary and transcript.
        """
        text = content

        # Strip YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                text = text[end + 3:].strip()

        # Strip verbose metadata lines (key: value pairs at the start)
        lines = text.split("\n")
        useful_lines = []
        skip_metadata = True
        for line in lines:
            stripped = line.strip()
            # Skip metadata-like lines at the top (key: value or key_name: value)
            if skip_metadata and re.match(r'^[a-z_]+\s*:', stripped):
                continue
            skip_metadata = False
            # Skip empty lines between metadata and content
            if not useful_lines and not stripped:
                continue
            useful_lines.append(line)

        text = "\n".join(useful_lines).strip()

        # Extract just summary and transcript sections
        sections = []
        for header in ["## Summary", "## Transcript"]:
            idx = text.find(header)
            if idx >= 0:
                # Find next ## header or end
                next_h = text.find("\n## ", idx + len(header))
                section_text = text[idx + len(header):next_h if next_h > 0 else len(text)].strip()
                if section_text:
                    sections.append(section_text)

        if sections:
            text = " | ".join(sections)

        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        return text

    def _raw_collation(self, cc_reports: list[dict], voice_memos: list[dict],
                       date_str: str) -> str:
        """Smart fallback: extract headers + key sections from each session."""
        parts = [
            "DAILY REPORT (AUTOMATED COLLATION)",
            "",
            f"Date: {date_str}",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "NOTE: This report was compiled from session headers and key sections",
            "because Claude API synthesis was unavailable. Full session reports are",
            "available in ~/reports/cc/ on tensor-core.",
            "",
        ]

        if cc_reports:
            parts.append(f"CC SESSION REPORTS ({len(cc_reports)} sessions)")
            parts.append("=" * 50)
            total_chars = 0
            for r in cc_reports:
                if total_chars >= self.MAX_TOTAL_CHARS_FALLBACK:
                    parts.append(f"\n[... remaining sessions omitted \u2014 "
                                 f"total limit {self.MAX_TOTAL_CHARS_FALLBACK} chars reached ...]")
                    break
                summary = self._extract_session_summary(
                    r["content"], self.MAX_SESSION_CHARS_FALLBACK
                )
                parts.append(f"\n--- {r['topic']} ({r['filename']}) ---\n")
                parts.append(summary)
                total_chars += len(summary)

        if voice_memos:
            parts.append(f"\n\nVOICE MEMOS ({len(voice_memos)} memos)")
            parts.append("=" * 50)
            total_memo_chars = 0
            for m in voice_memos:
                if total_memo_chars >= self.MAX_TOTAL_MEMO_CHARS_FALLBACK:
                    remaining = len(voice_memos) - voice_memos.index(m)
                    parts.append(f"\n[... {remaining} additional memos omitted ...]")
                    break
                header = f"\n--- {m['timestamp']}"
                if m.get("summary"):
                    header += f" \u2014 {m['summary']}"
                header += " ---\n"
                parts.append(header)
                essence = self._extract_memo_essence(
                    m["content"], self.MAX_MEMO_CHARS_FALLBACK
                )
                parts.append(essence)
                total_memo_chars += len(essence)

        return "\n".join(parts)

    # -- PDF generation --------------------------------------------------------

    def generate_pdf(self, report_data, date_str: str,
                     session_count: int, memo_count: int) -> str:
        """Generate a branded PureTensor PDF.

        report_data can be:
          - dict: structured JSON from Claude (preferred path)
          - str: plain text from raw collation fallback

        Returns the output file path.
        """
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(OUTPUT_DIR / f"PureTensor_Daily_Report_{date_str}.pdf")

        if isinstance(report_data, dict):
            self._render_structured_pdf(report_data, date_str, session_count,
                                        memo_count, output_path)
        else:
            self._render_fallback_pdf(report_data, date_str, session_count,
                                      memo_count, output_path)

        log.info("PDF generated: %s", output_path)
        return output_path

    def _render_structured_pdf(self, data: dict, date_str: str,
                                session_count: int, memo_count: int,
                                output_path: str):
        """Render structured JSON report data into a branded PDF."""
        pdf = DailyReport(date_str)

        # Cover page
        subtitle = data.get("subtitle", "")
        pdf.cover_page(subtitle, session_count, memo_count)

        # Page 2 — Executive Summary (prominent placement)
        pdf.add_page()
        section_num = 1

        # 1. Executive Summary — highlighted with background box
        pdf.h1(f"{section_num}. Executive Summary")
        summary_text = data.get("executive_summary", "No summary available.")
        # Render summary with slightly larger font for prominence
        pdf._pdf.set_font("DejaVu", "", 11)
        pdf._pdf.set_text_color(*BODY_GREY)
        pdf._pdf.set_fill_color(*TABLE_BG)
        x = pdf._pdf.get_x()
        y = pdf._pdf.get_y()
        # Draw background rect then text
        pdf._pdf.multi_cell(0, 7, summary_text, fill=True)
        pdf._pdf.ln(4)
        section_num += 1

        # 2-N. Activities by Theme
        for activity in data.get("activities", []):
            theme = activity.get("theme", "Activity")
            pdf.h1(f"{section_num}. {theme}")
            for para in activity.get("paragraphs", []):
                pdf.body(para)
            for sub in activity.get("sub_sections", []):
                pdf.h2(sub.get("title", ""))
                for para in sub.get("paragraphs", []):
                    pdf.body(para)
                for b in sub.get("bullets", []):
                    pdf.bullet(b)
            section_num += 1

        # Key Decisions
        decisions = data.get("key_decisions", [])
        if decisions:
            pdf.h1(f"{section_num}. Key Decisions")
            for d in decisions:
                pdf.bullet(d)
            section_num += 1

        # Files Modified (table)
        files_modified = data.get("files_modified", [])
        if files_modified:
            pdf.h1(f"{section_num}. Files Modified")
            widths = [55, 135]
            pdf.table_header(["Area", "Files"], widths)
            for i, fm in enumerate(files_modified):
                pdf.table_row(
                    [fm.get("area", ""), fm.get("files", "")],
                    widths, alt=(i % 2 == 1),
                )
            section_num += 1

        # Infrastructure Changes
        infra_changes = data.get("infrastructure_changes", [])
        if infra_changes:
            pdf.h1(f"{section_num}. Infrastructure Changes")
            for ic in infra_changes:
                pdf.bold_bullet(
                    f"{ic.get('node', 'Unknown')}:",
                    ic.get("change", ""),
                )
            section_num += 1

        # Fleet State
        fleet_state = data.get("fleet_state", [])
        if fleet_state:
            pdf.h1(f"{section_num}. Fleet State (End of Day)")
            for fs in fleet_state:
                pdf.bold_bullet(f"{fs.get('label', '')}:", fs.get("value", ""))
            section_num += 1

        # Voice Memo Highlights
        voice_highlights = data.get("voice_memo_highlights", [])
        if voice_highlights:
            pdf.h1(f"{section_num}. Voice Memo Highlights")
            for vh in voice_highlights:
                pdf.bullet(vh)
            section_num += 1

        # Unresolved Issues
        issues = data.get("unresolved_issues", [])
        if issues:
            pdf.h1(f"{section_num}. Unresolved Issues")
            for issue in issues:
                issue_id = issue.get("id", "")
                desc = issue.get("description", "")
                if issue_id:
                    pdf.bold_bullet(f"{issue_id}.", desc)
                else:
                    pdf.bullet(desc)
            section_num += 1

        # Next Steps
        next_steps = data.get("next_steps", {})
        categories = next_steps.get("categories", []) if isinstance(next_steps, dict) else []
        if categories:
            pdf.h1(f"{section_num}. Next Steps")
            for cat in categories:
                cat_name = cat.get("name", "")
                if cat_name:
                    pdf.h2(cat_name)
                for item in cat.get("items", []):
                    pdf.bullet(item)

        pdf.output(output_path)

    @staticmethod
    def _strip_md_inline(text: str) -> str:
        """Strip inline markdown formatting (bold, italic, code, links)."""
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)   # **bold**
        text = re.sub(r'__(.+?)__', r'\1', text)        # __bold__
        text = re.sub(r'\*(.+?)\*', r'\1', text)        # *italic*
        text = re.sub(r'_(.+?)_', r'\1', text)          # _italic_
        text = re.sub(r'`(.+?)`', r'\1', text)          # `code`
        text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text) # [link](url)
        text = text.replace('**', '')                    # orphaned bold markers
        return text

    def _render_fallback_pdf(self, text: str, date_str: str,
                              session_count: int, memo_count: int,
                              output_path: str):
        """Render plain-text fallback through the DailyReport class.

        Handles markdown syntax (# headings, **bold**, tables, lists)
        and maps them to semantic PDF methods.
        """
        pdf = DailyReport(date_str)
        pdf.cover_page("Automated Collation (API Unavailable)", session_count, memo_count)

        pdf.add_page()

        lines = text.split("\n")
        i = 0
        in_table = False

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Skip YAML frontmatter blocks
            if stripped == "---" and i > 0:
                # Check if this is start of YAML frontmatter (next lines are key: value)
                if i + 1 < len(lines) and re.match(r'^[a-z_]+\s*:', lines[i + 1].strip()):
                    i += 1
                    while i < len(lines) and lines[i].strip() != "---":
                        i += 1
                    i += 1  # skip closing ---
                    continue

            # Section divider (=== lines)
            if stripped and all(c == "=" for c in stripped):
                i += 1
                continue

            # Markdown headings: # -> h2, ## -> h2, ### -> h3
            # (demoted because h1 is reserved for top-level section headers)
            heading_match = re.match(r'^(#{1,3})\s+(.+)$', stripped)
            if heading_match:
                level = len(heading_match.group(1))
                title = self._strip_md_inline(heading_match.group(2))
                if level <= 2:
                    pdf.h2(title)
                else:
                    pdf.h3(title)
                i += 1
                continue

            # ALL CAPS header (possibly numbered)
            if (stripped and re.match(r'^(\d+\.\s*)?[A-Z][A-Z\s&\-:,()]+$', stripped)
                    and 3 < len(stripped) < 80):
                pdf.h1(stripped)
                i += 1
                continue

            # Sub-header with --- prefix/suffix
            if stripped.startswith("---") and stripped.endswith("---"):
                title = stripped.strip("- ").strip()
                if title:
                    pdf.h2(self._strip_md_inline(title))
                i += 1
                continue

            # Markdown table — skip header separator rows, render data
            if stripped.startswith("|") and stripped.endswith("|"):
                # Table separator row (|---|---|)
                if re.match(r'^\|[\s\-:|\+]+\|$', stripped):
                    i += 1
                    continue
                # Table data row — extract cell content
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                row_text = " | ".join(self._strip_md_inline(c) for c in cells if c)
                if not in_table:
                    # First row is likely header — render as bold
                    pdf.h3(row_text)
                    in_table = True
                else:
                    pdf.bullet(row_text)
                i += 1
                continue
            else:
                in_table = False

            # Numbered list items (1. text, 2. text)
            num_match = re.match(r'^(\d+)\.\s+(.+)$', stripped)
            if num_match:
                bullet_text = self._strip_md_inline(num_match.group(2))
                pdf.bullet(f"{num_match.group(1)}. {bullet_text}")
                i += 1
                continue

            # Bullet points (-, *, •)
            if stripped.startswith(("-", "*", "\u2022")) and len(stripped) > 2:
                # Make sure it's not a horizontal rule (---, ***, etc.)
                if re.match(r'^[-*]{3,}\s*$', stripped):
                    i += 1
                    continue
                bullet_text = self._strip_md_inline(stripped.lstrip("-*\u2022 "))
                pdf.bullet(bullet_text)
                i += 1
                continue

            # Empty lines
            if not stripped:
                i += 1
                continue

            # Body text — strip inline markdown
            pdf.body(self._strip_md_inline(stripped))
            i += 1

        pdf.output(output_path)

    # -- Google Drive upload ---------------------------------------------------

    def upload_to_drive(self, pdf_path: str) -> str | None:
        """Upload PDF to Google Drive ops account.

        Returns the web view link, or None on failure.
        """
        if not DRIVE_FOLDER_ID:
            log.warning("DRIVE_DAILY_REPORTS_FOLDER not set, skipping Drive upload")
            return None

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            log.warning("Google API packages not available, skipping Drive upload")
            return None

        if not DRIVE_TOKEN_PATH.exists():
            log.warning("Drive token not found at %s", DRIVE_TOKEN_PATH)
            return None

        try:
            creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_PATH), DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                DRIVE_TOKEN_PATH.write_text(creds.to_json())

            service = build("drive", "v3", credentials=creds)

            file_metadata = {
                "name": Path(pdf_path).name,
                "parents": [DRIVE_FOLDER_ID],
            }
            media = MediaFileUpload(pdf_path, mimetype="application/pdf", resumable=True)

            result = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink",
            ).execute()

            link = result.get("webViewLink", "")
            log.info("Uploaded to Drive: %s -> %s", result.get("name"), link)
            return link

        except Exception as e:
            log.warning("Drive upload failed: %s", e)
            return None

    # -- Telegram notification -------------------------------------------------

    def send_telegram_document(self, file_path: str, caption: str = "") -> bool:
        """Send a document to Telegram using curl (multipart/form-data)."""
        from config import BOT_TOKEN, AUTHORIZED_USER_ID

        cmd = [
            "curl", "-s",
            "-F", f"chat_id={AUTHORIZED_USER_ID}",
            "-F", f"document=@{file_path}",
        ]
        if caption:
            cmd.extend(["-F", f"caption={caption[:1024]}"])
        cmd.append(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                resp = json.loads(result.stdout)
                if resp.get("ok"):
                    return True
                log.warning("Telegram sendDocument returned ok=false: %s",
                            resp.get("description", "unknown"))
            else:
                log.warning("curl sendDocument failed: %s", result.stderr[:200])
        except Exception as e:
            log.warning("Failed to send Telegram document: %s", e)
        return False

    def send_summary(self, date_str: str, session_count: int, memo_count: int,
                     themes: list[str], drive_link: str | None) -> None:
        """Send a compact summary notification to Telegram."""
        lines = [
            f"DAILY REPORT COMPILED \u2014 {date_str}",
            "",
            f"Sessions: {session_count} | Voice memos: {memo_count}",
        ]

        if themes:
            lines.append(f"Themes: {', '.join(themes[:6])}")

        lines.append("")

        if drive_link:
            lines.append(f"Drive: {drive_link}")
        else:
            lines.append("Drive upload: failed (PDF sent as document)")

        self.send_telegram("\n".join(lines))

    # -- State tracking --------------------------------------------------------

    def _state_file(self) -> Path:
        """Path to the state file tracking last compiled date."""
        state_dir = Path(os.environ.get("OBSERVER_STATE_DIR", "/data/state/observers"))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "daily_report_state.json"

    def _get_last_date(self) -> str:
        """Get the last compiled date from state file."""
        sf = self._state_file()
        if sf.exists():
            try:
                data = json.loads(sf.read_text())
                return data.get("last_compiled_date", "")
            except Exception:
                pass
        return ""

    def _set_last_date(self, date_str: str) -> None:
        """Record the last compiled date."""
        sf = self._state_file()
        sf.write_text(json.dumps({"last_compiled_date": date_str}))

    # -- Extract themes from report --------------------------------------------

    def _extract_themes(self, cc_reports: list[dict]) -> list[str]:
        """Extract unique topic themes from CC reports."""
        themes = []
        seen = set()
        for r in cc_reports:
            topic = r["topic"].strip()
            key = topic.lower()
            if key not in seen and topic:
                seen.add(key)
                themes.append(topic.title())
        return themes

    # -- Observer interface ----------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Execute the daily report pipeline."""
        now = self.now_utc()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

        log.info("Daily report observer starting for %s (triggered at %s)",
                 date_str, now.strftime("%Y-%m-%d %H:%M UTC"))

        # Prevent double-runs
        if self._get_last_date() == date_str:
            log.info("Daily report already compiled for %s, skipping", date_str)
            return ObserverResult(success=True, message="Already compiled today")

        # 1. Gather data
        cc_reports = self.gather_cc_reports(date_str)
        voice_memos = self.gather_voice_memos(date_str)

        session_count = len(cc_reports)
        memo_count = len(voice_memos)

        if session_count == 0 and memo_count == 0:
            self.send_telegram(f"Daily Report ({date_str}): No CC sessions or voice memos today.")
            self._set_last_date(date_str)
            return ObserverResult(success=True, message="No reports today")

        themes = self._extract_themes(cc_reports)

        # 2. Synthesize report (returns dict on success, str on fallback)
        synthesis_ok = False
        try:
            report_data = self.synthesize_report(cc_reports, voice_memos, date_str)
            synthesis_ok = isinstance(report_data, dict)
        except Exception as e:
            log.error("Report synthesis failed completely: %s", e)
            deduped = self._deduplicate_reports(cc_reports)
            report_data = self._raw_collation(deduped, voice_memos, date_str)

        # QUALITY GATE: Never publish a raw collation dump to Drive or Telegram.
        # If synthesis failed, alert and hold locally — do NOT upload garbage.
        if not synthesis_ok:
            log.warning("Synthesis failed — raw collation only. Will NOT upload to Drive.")
            self.send_telegram(
                f"DAILY REPORT ALERT — {date_str}\n\n"
                f"Synthesis FAILED (all providers down). "
                f"Raw fallback PDF saved locally but NOT uploaded to Drive.\n\n"
                f"Sessions: {session_count} | Memos: {memo_count}\n"
                f"Action required: re-run manually once API is available."
            )
            # Still generate local PDF for manual recovery
            try:
                pdf_path = self.generate_pdf(report_data, date_str, session_count, memo_count)
                log.info("Fallback PDF saved locally: %s", pdf_path)
            except Exception as e:
                log.error("Even fallback PDF generation failed: %s", e)
            # Do NOT mark as compiled — allows automatic retry next run
            return ObserverResult(
                success=False,
                error="Synthesis failed, raw fallback held locally",
                message=f"Report for {date_str} NOT published — synthesis unavailable",
            )

        # 3. Generate PDF (structured data only — quality guaranteed)
        pdf_path = None
        try:
            pdf_path = self.generate_pdf(report_data, date_str, session_count, memo_count)
        except Exception as e:
            log.error("PDF generation failed: %s", e)
            self.send_telegram(
                f"Daily Report ({date_str}) - PDF generation failed: {e}\n\n"
                f"Sessions: {session_count}, Memos: {memo_count}"
            )
            self._set_last_date(date_str)
            return ObserverResult(
                success=False,
                error=f"PDF generation failed: {e}",
                message=f"Text-only report sent for {date_str}",
            )

        # 4. Upload to Drive
        drive_link = None
        try:
            drive_link = self.upload_to_drive(pdf_path)
        except Exception as e:
            log.warning("Drive upload failed: %s", e)

        # 5. If Drive failed, send PDF directly via Telegram
        if not drive_link and pdf_path:
            self.send_telegram_document(
                pdf_path,
                caption=f"Daily Report {date_str} ({session_count} sessions, {memo_count} memos)"
            )

        # 6. Send summary notification
        try:
            self.send_summary(date_str, session_count, memo_count, themes, drive_link)
        except Exception as e:
            log.warning("Telegram summary failed: %s", e)

        # 7. Mark as compiled
        self._set_last_date(date_str)

        log.info("Daily report pipeline completed for %s", date_str)
        return ObserverResult(
            success=True,
            message=f"Daily report compiled for {date_str}: {session_count} sessions, {memo_count} memos",
            data={
                "date": date_str,
                "sessions": session_count,
                "memos": memo_count,
                "themes": themes,
                "pdf_path": pdf_path,
                "drive_link": drive_link,
            },
        )


# ---------------------------------------------------------------------------
# Standalone execution for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from config import log as _  # noqa: F401 — triggers logging setup

    observer = DailyReportObserver()

    # Allow testing with a specific date: python3 daily_report.py 2026-02-28
    # run() compiles for YESTERDAY, so we set now_utc to test_date + 1 day at 01:00
    if len(sys.argv) > 1:
        test_date = sys.argv[1]
        fake_now = datetime.strptime(test_date, "%Y-%m-%d").replace(
            hour=1, minute=0, tzinfo=timezone.utc
        ) + timedelta(days=1)
        observer.now_utc = lambda: fake_now
        # Reset state to allow re-run
        sf = observer._state_file()
        if sf.exists():
            data = json.loads(sf.read_text())
            if data.get("last_compiled_date") == test_date:
                sf.unlink()

    result = observer.run()

    if result.success:
        print(f"Daily report completed: {result.message}")
        if result.data:
            print(f"  PDF: {result.data.get('pdf_path', 'N/A')}")
            print(f"  Drive: {result.data.get('drive_link', 'N/A')}")
    else:
        print(f"Daily report failed: {result.error}", file=sys.stderr)
        sys.exit(1)
