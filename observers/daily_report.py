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
import mimetypes
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

# PDF styling (matches gen_2026-02-25_parallelism_report.py)
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
DARK_BLUE = (26, 60, 110)
BODY_GREY = (51, 51, 51)
META_GREY = (85, 85, 85)
LIGHT_GREY = (119, 119, 119)
WHITE = (255, 255, 255)


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
        """Deduplicate sessions on the same topic, keeping the longest version.

        Prevents the same assessment appearing 3 times (e.g. network fabric
        assessment had 3 near-identical sessions on 2026-02-28).
        """
        by_topic: dict[str, dict] = {}
        for r in cc_reports:
            # Normalise topic for grouping: lowercase, strip timestamps/numbers
            key = re.sub(r'\d+', '', r["topic"].lower()).strip()
            key = re.sub(r'\s+', ' ', key)
            if key in by_topic:
                # Keep the longer version (more complete)
                if len(r["content"]) > len(by_topic[key]["content"]):
                    by_topic[key] = r
            else:
                by_topic[key] = r

        deduped = list(by_topic.values())
        if len(deduped) < len(cc_reports):
            log.info("Deduplicated %d sessions -> %d unique topics",
                     len(cc_reports), len(deduped))
        return deduped

    def synthesize_report(self, cc_reports: list[dict], voice_memos: list[dict],
                          date_str: str) -> str:
        """Call Claude to synthesize a structured daily report.

        Retries up to MAX_RETRIES times with exponential backoff.
        Falls back to smart collation (headers + summaries only) if all retries fail.
        """
        # Deduplicate overlapping sessions
        cc_reports = self._deduplicate_reports(cc_reports)

        # Build prompt
        parts = [
            f"Date: {date_str}. Below are all CC session reports and voice memos "
            "from this day's work. Synthesize them into a structured daily operations report.\n",
        ]

        if cc_reports:
            parts.append(f"== CC SESSION REPORTS ({len(cc_reports)} sessions) ==\n")
            for i, r in enumerate(cc_reports, 1):
                parts.append(f"--- Session {i}: {r['topic']} ({r['filename']}) ---")
                # Truncate very long reports to fit context
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
                    header += f" — {m['summary']}"
                header += f" ({m['filename']}) ---"
                parts.append(m["content"])
                parts.append("")

        parts.append(
            "\nWrite a structured daily report with these sections:\n"
            "1. EXECUTIVE SUMMARY (2-3 sentences)\n"
            "2. ACTIVITIES BY THEME (group related work, use clear sub-headings)\n"
            "3. KEY DECISIONS (bullet list of decisions made today)\n"
            "4. UNRESOLVED ISSUES (anything left open)\n"
            "5. VOICE MEMO HIGHLIGHTS (if any memos, summarize key points)\n"
            "6. NEXT STEPS (actionable items for tomorrow)\n\n"
            "Be concise but thorough. Use plain text with simple section headers. "
            "No markdown formatting. Each section header should be on its own line "
            "in ALL CAPS followed by a blank line."
        )

        prompt = "\n".join(parts)

        # Retry loop with exponential backoff
        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                log.info("Claude synthesis attempt %d/%d", attempt, self.MAX_RETRIES)
                result = self.call_claude(prompt, timeout=180)
                if result and len(result.strip()) > 100:
                    return result.strip()
                log.warning("Claude returned insufficient content (%d chars) on attempt %d",
                            len(result) if result else 0, attempt)
                last_error = f"insufficient content ({len(result) if result else 0} chars)"
            except Exception as e:
                last_error = str(e)
                log.warning("Claude synthesis attempt %d failed: %s", attempt, e)

            if attempt < self.MAX_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.info("Retrying in %d seconds...", delay)
                time.sleep(delay)

        log.warning("All %d Claude attempts failed (last: %s) — using smart collation",
                    self.MAX_RETRIES, last_error)
        return self._raw_collation(cc_reports, voice_memos, date_str)

    @staticmethod
    def _extract_session_summary(content: str, max_chars: int) -> str:
        """Extract the most useful parts of a session report for fallback collation.

        Prioritises: frontmatter/metadata, Objective, Results/Summary, Issues,
        Next Steps sections. Falls back to first N chars if no structure found.
        """
        # Priority sections to extract (case-insensitive)
        priority_headers = [
            r'(?:^|\n)#+\s*(Objective|Purpose|Goal)',
            r'(?:^|\n)#+\s*(Results?|Results?\s+Summary|Summary|Overall\s+Summary)',
            r'(?:^|\n)#+\s*(Issues?\s+(?:Found|Resolved|Encountered)|Issues?\s+&)',
            r'(?:^|\n)#+\s*(Next\s+Steps?|Recommendations?|Action\s+Items?)',
            r'(?:^|\n)#+\s*(Key\s+(?:Findings?|Results?|Decisions?))',
            r'(?:^|\n)\*\*(?:Objective|Results?|Summary|Issues?|Next Steps?)[\s:*]',
        ]

        # Try to extract structured sections
        extracted_parts = []

        # Always grab the first few lines (usually has metadata like Date, Node, etc.)
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

        # Extract priority sections
        for pattern in priority_headers:
            matches = list(re.finditer(pattern, content, re.IGNORECASE))
            for match in matches:
                start = match.start()
                # Find the end of this section (next heading or end of content)
                next_heading = re.search(r'\n#{1,3}\s', content[start + 1:])
                if next_heading:
                    end = start + 1 + next_heading.start()
                else:
                    end = min(start + max_chars // 2, len(content))
                section = content[start:end].strip()
                if section and section not in "\n".join(extracted_parts):
                    extracted_parts.append(section)

        result = "\n\n".join(extracted_parts)

        # If we got meaningful extracted content, use it
        if len(result) > 100:
            if len(result) > max_chars:
                result = result[:max_chars] + "\n[... truncated ...]"
            return result

        # Fallback: just use the first N chars
        if len(content) > max_chars:
            return content[:max_chars] + "\n[... truncated ...]"
        return content

    def _raw_collation(self, cc_reports: list[dict], voice_memos: list[dict],
                       date_str: str) -> str:
        """Smart fallback: extract headers + key sections from each session.

        Unlike the old verbatim dump, this caps per-session content and
        extracts only the most useful sections (objective, results, issues,
        next steps). This prevents 407-page PDFs when Claude API is unavailable.
        """
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
                    parts.append(f"\n[... remaining sessions omitted — "
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
            for m in voice_memos:
                header = f"\n--- {m['timestamp']}"
                if m.get("summary"):
                    header += f" — {m['summary']}"
                header += f" ({m['filename']}) ---\n"
                parts.append(header)
                # Voice memos are typically short, include full content
                content = m["content"]
                if len(content) > 2000:
                    content = content[:2000] + "\n[... truncated ...]"
                parts.append(content)

        return "\n".join(parts)

    # -- PDF generation --------------------------------------------------------

    def generate_pdf(self, report_text: str, date_str: str,
                     session_count: int, memo_count: int) -> str:
        """Generate a branded PureTensor PDF from the report text.

        Returns the output file path.
        """
        from fpdf import FPDF

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(OUTPUT_DIR / f"PureTensor_Daily_Report_{date_str}.pdf")

        # Parse date for display
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            date_display = date_obj.strftime("%d %B %Y")
        except ValueError:
            date_display = date_str

        class DailyPDF(FPDF):
            def __init__(self, title, footer_date):
                super().__init__()
                self.doc_title = title
                self.footer_date = footer_date
                self.add_font("DejaVu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"), uni=True)
                self.add_font("DejaVu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), uni=True)
                self.add_font("DejaVu", "I", os.path.join(FONT_DIR, "DejaVuSans-Oblique.ttf"), uni=True)
                self.add_font("DejaVu", "BI", os.path.join(FONT_DIR, "DejaVuSans-BoldOblique.ttf"), uni=True)
                self.set_auto_page_break(auto=True, margin=20)

            def header(self):
                if self.page_no() == 1:
                    return
                self.set_font("DejaVu", "", 8)
                self.set_text_color(*DARK_BLUE)
                self.cell(0, 6, self.doc_title, ln=False)
                self.cell(0, 6, f"Page {self.page_no()}", align="R", ln=True)
                self.set_draw_color(*DARK_BLUE)
                self.line(10, self.get_y(), 200, self.get_y())
                self.ln(4)

            def footer(self):
                self.set_y(-15)
                self.set_font("DejaVu", "I", 8)
                self.set_text_color(*LIGHT_GREY)
                self.cell(0, 10, f"PureTensor Inc \u2014 Confidential \u2014 {self.footer_date}",
                          align="C")

        pdf = DailyPDF(f"Daily Operations Report \u2014 {date_display}", date_display)

        # -- Cover page --
        pdf.add_page()

        # Corporate header
        pdf.set_font("DejaVu", "", 9)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(0, 5, "PureTensor Inc", align="R", ln=True)
        pdf.cell(0, 5, "131 Continental Dr, Suite 305", align="R", ln=True)
        pdf.cell(0, 5, "Newark, DE 19713, US", align="R", ln=True)
        pdf.ln(30)

        # Title
        pdf.set_font("DejaVu", "B", 28)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(0, 14, "Daily Operations Report", align="C", ln=True)
        pdf.ln(4)

        # Date
        pdf.set_font("DejaVu", "", 16)
        pdf.set_text_color(*META_GREY)
        pdf.cell(0, 10, date_display, align="C", ln=True)
        pdf.ln(6)

        # Divider
        pdf.set_draw_color(*DARK_BLUE)
        pdf.set_line_width(0.8)
        pdf.line(60, pdf.get_y(), 150, pdf.get_y())
        pdf.ln(12)

        # Stats
        pdf.set_font("DejaVu", "B", 36)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(95, 18, str(session_count), align="C")
        pdf.cell(95, 18, str(memo_count), align="C", ln=True)
        pdf.set_font("DejaVu", "", 12)
        pdf.set_text_color(*META_GREY)
        pdf.cell(95, 7, "Sessions", align="C")
        pdf.cell(95, 7, "Voice Memos", align="C", ln=True)
        pdf.ln(20)

        # Meta
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(*LIGHT_GREY)
        pdf.cell(0, 6, f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                 align="C", ln=True)
        pdf.cell(0, 6, "Classification: Internal \u2014 Operations", align="C", ln=True)
        pdf.ln(6)
        pdf.set_font("DejaVu", "B", 10)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(0, 6, "CONFIDENTIAL", align="C", ln=True)

        # -- Content pages --
        pdf.add_page()

        # Parse report into sections and render
        lines = report_text.split("\n")
        for line in lines:
            stripped = line.strip()

            # Section headers (ALL CAPS lines, possibly numbered)
            if stripped and re.match(r'^(\d+\.\s*)?[A-Z][A-Z\s&\-:]+$', stripped) and len(stripped) < 80:
                pdf.ln(4)
                pdf.set_font("DejaVu", "B", 14)
                pdf.set_text_color(*DARK_BLUE)
                pdf.cell(0, 9, stripped, ln=True)
                pdf.set_draw_color(*DARK_BLUE)
                pdf.set_line_width(0.3)
                pdf.line(10, pdf.get_y(), 200, pdf.get_y())
                pdf.ln(3)
            # Sub-headers (Title Case, shorter)
            elif stripped and not stripped.startswith(("-", "*", "\u2022")) and stripped == stripped.title() and len(stripped) < 60 and len(stripped) > 3:
                pdf.ln(2)
                pdf.set_font("DejaVu", "B", 11)
                pdf.set_text_color(*DARK_BLUE)
                pdf.cell(0, 7, stripped, ln=True)
                pdf.ln(1)
            # Bullet points
            elif stripped.startswith(("-", "*", "\u2022")):
                text = stripped.lstrip("-*\u2022 ")
                pdf.set_font("DejaVu", "", 10)
                pdf.set_text_color(*BODY_GREY)
                pdf.cell(8)
                pdf.multi_cell(0, 5.5, f"\u2022  {text}")
                pdf.ln(1)
            # Empty lines
            elif not stripped:
                pdf.ln(3)
            # Body text
            else:
                pdf.set_font("DejaVu", "", 10)
                pdf.set_text_color(*BODY_GREY)
                pdf.multi_cell(0, 5.5, stripped)
                pdf.ln(1)

        pdf.output(output_path)
        log.info("PDF generated: %s", output_path)
        return output_path

    # -- Google Drive upload ---------------------------------------------------

    def upload_to_drive(self, pdf_path: str) -> str | None:
        """Upload PDF to Google Drive ops account.

        Returns the web view link, or None on failure.
        """
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
                # Save refreshed token
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
        """Execute the daily report pipeline.

        Compiles the PREVIOUS day's report (runs at 01:00 UTC, so yesterday
        is the complete workday). This ensures all sessions have been synced
        and avoids API congestion during US afternoon peak hours.
        """
        now = self.now_utc()
        # Compile for yesterday — the observer runs after midnight
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

        # 2. Synthesize report (includes deduplication and retry logic)
        try:
            report_text = self.synthesize_report(cc_reports, voice_memos, date_str)
        except Exception as e:
            log.error("Report synthesis failed completely: %s", e)
            deduped = self._deduplicate_reports(cc_reports)
            report_text = self._raw_collation(deduped, voice_memos, date_str)

        # 3. Generate PDF
        pdf_path = None
        try:
            pdf_path = self.generate_pdf(report_text, date_str, session_count, memo_count)
        except Exception as e:
            log.error("PDF generation failed: %s", e)
            # Send text-only fallback
            self.send_telegram(
                f"Daily Report ({date_str}) - PDF generation failed: {e}\n\n"
                f"Sessions: {session_count}, Memos: {memo_count}\n\n"
                f"{report_text[:3500]}"
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
