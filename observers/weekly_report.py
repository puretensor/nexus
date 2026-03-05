#!/usr/bin/env python3
"""Weekly report compilation observer.

Runs Sunday 02:00 UTC. Aggregates the week's CC session reports into a
comprehensive weekly digest PDF, uploads to Google Drive, and notifies
via Telegram.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observers.base import Observer, ObserverResult

log = logging.getLogger("nexus")

# Sync mount
SYNC_DIR = Path("/sync")
CC_REPORTS_DIR = SYNC_DIR / "reports" / "cc"

# Output directory
OUTPUT_DIR = Path("/output/weekly")

# Google Drive
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

SYSTEM_PROMPT = (
    "You are a technical report synthesizer for PureTensor Inc. "
    "You produce structured JSON weekly digests from raw session logs. "
    "Return ONLY valid JSON, no markdown fences, no explanation text."
)

WEEKLY_PROMPT = """\
Below are all Claude Code session reports from PureTensor Inc for the week of {start_date} to {end_date}.
Compile them into a comprehensive weekly executive summary.

Structure your response as JSON:
{{
  "executive_summary": "3-5 sentence overview of the week's work",
  "subtitle": "Short thematic line for the cover, e.g. 'Infrastructure, AI & Commerce'",
  "highlights": ["Top achievement 1", "Top achievement 2", "Top achievement 3"],
  "themes": [
    {{
      "name": "Theme Name",
      "summary": "2-3 sentence summary of work in this theme",
      "sessions": 5,
      "key_items": ["Specific item 1", "Specific item 2"]
    }}
  ],
  "infrastructure_changes": [
    {{"node": "node-name", "change": "What changed"}}
  ],
  "decisions_made": ["Decision 1", "Decision 2"],
  "metrics": {{
    "total_sessions": 0,
    "files_modified_estimate": 0,
    "services_deployed": 0
  }},
  "unresolved": ["Issue still open 1"],
  "next_week": ["Priority 1", "Priority 2"]
}}

Rules:
- Group related work into coherent themes (max 6 themes)
- Highlights should be the 3-5 most impactful accomplishments
- Be concise but specific — include node names, file counts, metrics
- If a section has no data, use an empty array/object
- Return ONLY the JSON object

SESSION REPORTS ({total_sessions} sessions across {days_with_work} active days):
"""


class WeeklyReportObserver(Observer):
    """Compiles weekly CC reports into a comprehensive digest PDF."""

    name = "weekly_report"
    schedule = "0 2 * * 0"  # Sunday 02:00 UTC

    MAX_RETRIES = 3
    RETRY_DELAY = 15

    # -- Data gathering --------------------------------------------------------

    def _gather_week_reports(self, start_date: str, end_date: str) -> list[dict]:
        """Gather all CC reports for the week (Mon-Sun)."""
        reports = []
        if not CC_REPORTS_DIR.exists():
            return reports

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            for path in sorted(CC_REPORTS_DIR.glob(f"{date_str}*.md")):
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    name = path.stem
                    parts = name.split("_", 2)
                    topic = parts[2].replace("-", " ").replace("_", " ") if len(parts) > 2 else name
                    reports.append({
                        "date": date_str,
                        "topic": topic,
                        "filename": path.name,
                        "content": content,
                    })
                except Exception as e:
                    log.warning("Failed to read %s: %s", path.name, e)
            current += timedelta(days=1)

        return reports

    # -- LLM -------------------------------------------------------------------

    def _call_bedrock(self, prompt: str) -> str:
        """Call Bedrock Claude Sonnet for synthesis."""
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
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.3, "maxTokens": 8192},
        )

        output = response.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])
        return "\n".join(b["text"] for b in content_blocks if "text" in b)

    def _parse_json(self, text: str) -> dict | None:
        """Parse JSON from LLM response."""
        text = text.strip()
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl > 0:
                text = text[first_nl + 1:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].rstrip()

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("JSON parse failed: %s", e)
            return None

    # -- Synthesis -------------------------------------------------------------

    def _synthesize(self, reports: list[dict], start_date: str, end_date: str) -> dict | None:
        """Synthesize weekly report from session reports."""
        days_with_work = len(set(r["date"] for r in reports))

        prompt_parts = [WEEKLY_PROMPT.format(
            start_date=start_date,
            end_date=end_date,
            total_sessions=len(reports),
            days_with_work=days_with_work,
        )]

        # Group by day for clearer presentation to LLM
        by_date = {}
        for r in reports:
            by_date.setdefault(r["date"], []).append(r)

        for date_str in sorted(by_date.keys()):
            day_reports = by_date[date_str]
            prompt_parts.append(f"\n=== {date_str} ({len(day_reports)} sessions) ===\n")
            for r in day_reports:
                content = r["content"]
                # Truncate long reports more aggressively for weekly (many sessions)
                if len(content) > 4000:
                    content = content[:4000] + "\n[... truncated ...]"
                prompt_parts.append(f"--- {r['filename']} ({r['topic']}) ---\n{content}\n")

        prompt = "\n".join(prompt_parts)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                log.info("Weekly synthesis attempt %d/%d", attempt, self.MAX_RETRIES)
                result = self._call_bedrock(prompt)
                if result and len(result.strip()) > 100:
                    parsed = self._parse_json(result)
                    if parsed and "executive_summary" in parsed:
                        return parsed
                    log.warning("Attempt %d: invalid JSON response", attempt)
                else:
                    log.warning("Attempt %d: insufficient content", attempt)
            except Exception as e:
                log.warning("Attempt %d failed: %s", attempt, e)
            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY * (2 ** (attempt - 1)))

        return None

    # -- PDF generation --------------------------------------------------------

    def _generate_pdf(self, data: dict, start_date: str, end_date: str,
                      total_sessions: int, active_days: int) -> str | None:
        """Generate the weekly report PDF."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        try:
            start_obj = datetime.strptime(start_date, "%Y-%m-%d")
            week_label = f"Week of {start_obj.strftime('%d %B %Y')}"
        except ValueError:
            week_label = f"Week of {start_date}"

        filename = f"PureTensor_Weekly_Report_{start_date}.pdf"
        output_path = str(OUTPUT_DIR / filename)

        try:
            from fpdf import FPDF

            class WeeklyPDF(FPDF):
                def __init__(self):
                    super().__init__()
                    self.add_font("DejaVu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"), uni=True)
                    self.add_font("DejaVu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), uni=True)
                    self.add_font("DejaVu", "I", os.path.join(FONT_DIR, "DejaVuSans-Oblique.ttf"), uni=True)
                    self.set_auto_page_break(auto=True, margin=20)

                def header(self):
                    if self.page_no() == 1:
                        return
                    self.set_font("DejaVu", "", 7.5)
                    self.set_text_color(*LIGHT_GREY)
                    self.cell(0, 6, f"Weekly Report — {week_label}", ln=False)
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
                    self.cell(0, 10, f"PureTensor Inc  |  {week_label}  |  CONFIDENTIAL", align="C")

            pdf = WeeklyPDF()

            # Cover page
            pdf.add_page()
            pdf.set_font("DejaVu", "", 9)
            pdf.set_text_color(*DARK_BLUE)
            pdf.cell(0, 5, "PureTensor Inc", align="R", ln=True)
            pdf.cell(0, 5, "131 Continental Dr, Suite 305", align="R", ln=True)
            pdf.cell(0, 5, "Newark, DE 19713, US", align="R", ln=True)
            pdf.ln(35)

            pdf.set_draw_color(*ACCENT_BLUE)
            pdf.set_line_width(1.5)
            pdf.line(60, pdf.get_y(), 150, pdf.get_y())
            pdf.ln(10)

            pdf.set_font("DejaVu", "B", 36)
            pdf.set_text_color(*DARK_BLUE)
            pdf.cell(0, 18, "Weekly Report", align="C", ln=True)
            pdf.ln(6)

            pdf.set_font("DejaVu", "", 18)
            pdf.set_text_color(*META_GREY)
            pdf.cell(0, 12, week_label, align="C", ln=True)
            pdf.ln(6)

            pdf.set_draw_color(*ACCENT_BLUE)
            pdf.set_line_width(1.5)
            pdf.line(60, pdf.get_y(), 150, pdf.get_y())
            pdf.ln(10)

            subtitle = data.get("subtitle", "")
            if subtitle:
                pdf.set_font("DejaVu", "I", 13)
                pdf.set_text_color(*META_GREY)
                pdf.cell(0, 8, subtitle, align="C", ln=True)
            pdf.ln(30)

            pdf.set_font("DejaVu", "", 10)
            pdf.set_text_color(*LIGHT_GREY)
            gen_time = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
            pdf.cell(0, 7, f"Generated: {gen_time}", align="C", ln=True)
            pdf.ln(2)
            pdf.cell(0, 7, f"Sessions: {total_sessions}  |  Active Days: {active_days}/7", align="C", ln=True)
            pdf.ln(10)
            pdf.set_font("DejaVu", "B", 10)
            pdf.set_text_color(*DARK_BLUE)
            pdf.cell(0, 7, "CONFIDENTIAL", align="C", ln=True)

            # Helper lambdas
            def h1(text):
                pdf.ln(8)
                pdf.set_font("DejaVu", "B", 18)
                pdf.set_text_color(*DARK_BLUE)
                pdf.cell(0, 11, text, ln=True)
                pdf.set_draw_color(*ACCENT_BLUE)
                pdf.set_line_width(0.8)
                pdf.line(10, pdf.get_y() + 1, 200, pdf.get_y() + 1)
                pdf.ln(6)

            def h2(text):
                pdf.ln(5)
                pdf.set_font("DejaVu", "B", 13)
                pdf.set_text_color(*DARK_BLUE)
                pdf.cell(0, 8, text, ln=True)
                pdf.ln(3)

            def body(text):
                pdf.set_font("DejaVu", "", 10)
                pdf.set_text_color(*BODY_GREY)
                pdf.multi_cell(0, 6, text)
                pdf.ln(2)

            def bullet(text):
                pdf.set_font("DejaVu", "", 10)
                pdf.set_text_color(*BODY_GREY)
                pdf.cell(6, 6, "\u2022")
                pdf.multi_cell(0, 6, text)
                pdf.ln(1)

            def bold_bullet(label, text):
                pdf.set_text_color(*BODY_GREY)
                pdf.set_font("DejaVu", "", 10)
                pdf.cell(6, 6, "\u2022")
                pdf.set_font("DejaVu", "B", 10)
                lw = pdf.get_string_width(label + " ")
                pdf.cell(lw, 6, label + " ")
                pdf.set_font("DejaVu", "", 10)
                pdf.multi_cell(0, 6, text)
                pdf.ln(1)

            # Content pages
            pdf.add_page()
            section = 1

            # Executive Summary
            h1(f"{section}. Executive Summary")
            pdf.set_font("DejaVu", "", 11)
            pdf.set_text_color(*BODY_GREY)
            pdf.set_fill_color(*TABLE_BG)
            pdf.multi_cell(0, 7, data.get("executive_summary", "No summary available."), fill=True)
            pdf.ln(4)
            section += 1

            # Highlights
            highlights = data.get("highlights", [])
            if highlights:
                h1(f"{section}. Key Highlights")
                for hl in highlights:
                    bullet(hl)
                section += 1

            # Themes
            themes = data.get("themes", [])
            if themes:
                h1(f"{section}. Work Themes")
                for theme in themes:
                    h2(f"{theme.get('name', '')} ({theme.get('sessions', '?')} sessions)")
                    if theme.get("summary"):
                        body(theme["summary"])
                    for item in theme.get("key_items", []):
                        bullet(item)
                section += 1

            # Infrastructure Changes
            infra = data.get("infrastructure_changes", [])
            if infra:
                h1(f"{section}. Infrastructure Changes")
                for ic in infra:
                    bold_bullet(f"{ic.get('node', 'Unknown')}:", ic.get("change", ""))
                section += 1

            # Decisions
            decisions = data.get("decisions_made", [])
            if decisions:
                h1(f"{section}. Decisions Made")
                for d in decisions:
                    bullet(d)
                section += 1

            # Metrics
            metrics = data.get("metrics", {})
            if metrics:
                h1(f"{section}. Week Metrics")
                bold_bullet("Total Sessions:", str(metrics.get("total_sessions", "?")))
                bold_bullet("Files Modified (est.):", str(metrics.get("files_modified_estimate", "?")))
                bold_bullet("Services Deployed:", str(metrics.get("services_deployed", "?")))
                section += 1

            # Unresolved
            unresolved = data.get("unresolved", [])
            if unresolved:
                h1(f"{section}. Unresolved Items")
                for u in unresolved:
                    bullet(u)
                section += 1

            # Next Week
            next_week = data.get("next_week", [])
            if next_week:
                h1(f"{section}. Next Week Priorities")
                for nw in next_week:
                    bullet(nw)

            pdf.output(output_path)
            log.info("Weekly PDF generated: %s", output_path)
            return output_path

        except Exception as e:
            log.error("Weekly PDF generation failed: %s", e)
            return None

    # -- Drive upload ----------------------------------------------------------

    def _upload_to_drive(self, pdf_path: str) -> str | None:
        """Upload weekly PDF to Drive (same folder as daily reports)."""
        if not DRIVE_FOLDER_ID:
            log.warning("DRIVE_DAILY_REPORTS_FOLDER not set, skipping")
            return None

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            log.warning("Google API packages not available")
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
            file_metadata = {"name": Path(pdf_path).name, "parents": [DRIVE_FOLDER_ID]}
            media = MediaFileUpload(pdf_path, mimetype="application/pdf", resumable=True)

            result = service.files().create(
                body=file_metadata, media_body=media,
                fields="id, name, webViewLink",
            ).execute()

            link = result.get("webViewLink", "")
            log.info("Uploaded to Drive: %s -> %s", result.get("name"), link)
            return link
        except Exception as e:
            log.warning("Drive upload failed: %s", e)
            return None

    # -- State tracking --------------------------------------------------------

    def _state_file(self) -> Path:
        state_dir = Path(os.environ.get("OBSERVER_STATE_DIR", "/data/state/observers"))
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "weekly_report_state.json"

    def _get_last_week(self) -> str:
        sf = self._state_file()
        if sf.exists():
            try:
                return json.loads(sf.read_text()).get("last_compiled_week", "")
            except Exception:
                pass
        return ""

    def _set_last_week(self, start_date: str):
        self._state_file().write_text(json.dumps({"last_compiled_week": start_date}))

    # -- Observer interface ----------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Execute the weekly report pipeline."""
        now = self.now_utc()

        # Calculate the Mon-Sun range for the week that just ended
        # now is Sunday 02:00 — the week is Mon (6 days ago) to Sun (today, yesterday's date)
        end_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")  # Saturday
        start_dt = now - timedelta(days=6)  # Monday
        start_date = start_dt.strftime("%Y-%m-%d")

        log.info("Weekly report for %s to %s", start_date, end_date)

        # Prevent double-runs
        if self._get_last_week() == start_date:
            log.info("Weekly report already compiled for week of %s", start_date)
            return ObserverResult(success=True)

        # 1. Gather all week's reports
        reports = self._gather_week_reports(start_date, end_date)
        if not reports:
            log.info("No reports for week of %s", start_date)
            self.send_telegram(f"Weekly Report ({start_date}): No CC sessions this week.")
            self._set_last_week(start_date)
            return ObserverResult(success=True, message="No reports this week")

        total_sessions = len(reports)
        active_days = len(set(r["date"] for r in reports))

        # 2. Synthesize
        data = self._synthesize(reports, start_date, end_date)
        if not data:
            self.send_telegram(
                f"WEEKLY REPORT ALERT — Week of {start_date}\n\n"
                f"Synthesis FAILED. {total_sessions} sessions across {active_days} days.\n"
                f"Action required: re-run manually."
            )
            return ObserverResult(
                success=False,
                error="Weekly synthesis failed (all attempts exhausted)",
            )

        # 3. Generate PDF
        pdf_path = self._generate_pdf(data, start_date, end_date, total_sessions, active_days)
        if not pdf_path:
            self.send_telegram(
                f"Weekly Report ({start_date}): Synthesis OK but PDF generation failed."
            )
            self._set_last_week(start_date)
            return ObserverResult(success=False, error="PDF generation failed")

        # 4. Upload to Drive
        drive_link = self._upload_to_drive(pdf_path)

        # 5. Telegram notification
        highlights = data.get("highlights", [])
        lines = [
            f"WEEKLY REPORT — Week of {start_date}",
            "",
            f"Sessions: {total_sessions} | Active Days: {active_days}/7",
        ]
        if highlights:
            lines.append("")
            lines.append("Highlights:")
            for hl in highlights[:5]:
                lines.append(f"  - {hl}")
        lines.append("")
        if drive_link:
            lines.append(f"Drive: {drive_link}")
        else:
            lines.append("Drive upload: failed")

        self.send_telegram("\n".join(lines))

        # If Drive failed, send PDF directly
        if not drive_link:
            try:
                from observers.daily_report import DailyReportObserver
                dr = DailyReportObserver()
                dr.send_telegram_document(
                    pdf_path,
                    caption=f"Weekly Report — Week of {start_date}"
                )
            except Exception as e:
                log.warning("Failed to send weekly PDF via Telegram: %s", e)

        self._set_last_week(start_date)

        return ObserverResult(
            success=True,
            message=f"Weekly report: {total_sessions} sessions, {active_days} active days",
            data={
                "start_date": start_date,
                "end_date": end_date,
                "sessions": total_sessions,
                "active_days": active_days,
                "pdf_path": pdf_path,
                "drive_link": drive_link,
            },
        )


# Standalone execution for testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F401

    observer = WeeklyReportObserver()

    # Test with specific date: python3 weekly_report.py 2026-03-02 (a Sunday)
    if len(sys.argv) > 1:
        test_sunday = sys.argv[1]
        fake_now = datetime.strptime(test_sunday, "%Y-%m-%d").replace(
            hour=2, minute=0, tzinfo=timezone.utc
        )
        observer.now_utc = lambda: fake_now
        start_date = (fake_now - timedelta(days=6)).strftime("%Y-%m-%d")
        sf = observer._state_file()
        if sf.exists():
            data = json.loads(sf.read_text())
            if data.get("last_compiled_week") == start_date:
                sf.unlink()

    result = observer.run()
    if result.success:
        print(f"Weekly report completed: {result.message}")
        if result.data:
            print(f"  PDF: {result.data.get('pdf_path', 'N/A')}")
            print(f"  Drive: {result.data.get('drive_link', 'N/A')}")
    else:
        print(f"Weekly report failed: {result.error}", file=sys.stderr)
        sys.exit(1)
