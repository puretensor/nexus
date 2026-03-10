#!/usr/bin/env python3
"""Documentation compiler observer.

Runs at 06:00 UTC daily (after the 01:00 daily_report). Analyzes the previous
day's CC session reports for significant work that warrants standalone
documentation beyond the daily report.

Produces:
  - Project Summary: 3+ sessions about the same project
  - Infrastructure Change Log: service deploys, config changes
  - Technical Decision Record: key architectural/technical decisions

Documents are generated as branded PDFs, uploaded to Google Drive, and
notified via Telegram. Quiet days produce no output (silent success).
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

# Sync mount (hostPath from fox-n1)
SYNC_DIR = Path("/sync")
CC_REPORTS_DIR = SYNC_DIR / "reports" / "cc"

# Output directory (NFS PVC)
OUTPUT_DIR = Path("/output/docs")

# Google Drive folder IDs (set via env)
DRIVE_PROJECTS_FOLDER = os.environ.get("DRIVE_PROJECTS_FOLDER", "")
DRIVE_INFRA_FOLDER = os.environ.get("DRIVE_INFRA_FOLDER", "")
DRIVE_TECH_REPORTS_FOLDER = os.environ.get("DRIVE_TECH_REPORTS_FOLDER", "")
DRIVE_TOKEN_PATH = Path.home() / ".config" / "puretensor" / "gdrive_tokens" / "token_ops.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# PDF styling (same as daily_report.py)
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
DARK_BLUE = (26, 60, 110)
ACCENT_BLUE = (52, 103, 172)
BODY_GREY = (51, 51, 51)
META_GREY = (85, 85, 85)
LIGHT_GREY = (119, 119, 119)
TABLE_BG = (240, 244, 248)
WHITE = (255, 255, 255)

# Document type -> Drive folder mapping
DOC_TYPE_FOLDERS = {
    "project_summary": DRIVE_PROJECTS_FOLDER,
    "infra_changelog": DRIVE_INFRA_FOLDER,
    "decision_record": DRIVE_TECH_REPORTS_FOLDER,
}

DOC_TYPE_TITLES = {
    "project_summary": "Project Summary",
    "infra_changelog": "Infrastructure Change Log",
    "decision_record": "Technical Decision Record",
}

CLASSIFICATION_PROMPT = """\
You are analyzing Claude Code session reports from a single day's work at PureTensor Inc.
Your job is to identify work that warrants standalone documentation beyond the daily report.

Classify the sessions into these categories:
1. **project_summary** — 3 or more sessions about the same project/feature (not routine maintenance)
2. **infra_changelog** — Any infrastructure changes: service deploys, config changes, K3s deployments, network changes, server setup
3. **decision_record** — Key architectural or technical decisions: technology choices, design patterns, migration decisions, trade-off analyses

Rules:
- Only flag genuinely significant work, not routine tasks
- A "project" needs coherent multi-session work on one topic, not unrelated small fixes
- Infrastructure changes should be non-trivial (not just restarting a service)
- Decision records should capture choices that affect future work
- If nothing qualifies, return empty arrays — silence is better than noise

Return ONLY valid JSON (no markdown fences):
{
  "documents": [
    {
      "type": "project_summary|infra_changelog|decision_record",
      "title": "Descriptive title for the document",
      "sessions": ["filename1.md", "filename2.md"],
      "summary": "One-line description of what this document should cover"
    }
  ]
}

If nothing warrants documentation, return: {"documents": []}

SESSION REPORTS:
"""

SYNTHESIS_PROMPTS = {
    "project_summary": """\
Write a concise Project Summary document for PureTensor Inc based on these session reports.

Structure:
1. **Objective** — What was the goal of this project work?
2. **Approach** — How was it implemented? Key design choices.
3. **Changes Made** — Specific files, services, and configurations changed.
4. **Results** — What was achieved? Metrics, before/after comparisons.
5. **Outstanding Items** — What remains to be done?
6. **Dependencies** — What does this work depend on or affect?

Be concise but thorough. Include concrete details: file paths, node names, commands, metrics.
No filler text. Every sentence should be informative.

Return ONLY valid JSON:
{
  "sections": [
    {"heading": "Section Title", "content": "Section text...", "bullets": ["bullet 1", "bullet 2"]}
  ]
}

SESSION REPORTS:
""",
    "infra_changelog": """\
Write an Infrastructure Change Log for PureTensor Inc based on these session reports.

Structure:
1. **Summary** — One paragraph overview of all changes.
2. **Changes** — Each change as a structured entry with:
   - Node/service affected
   - What changed (before -> after)
   - Why it was changed
   - Files/configs modified
   - Verification steps taken
3. **Rollback Notes** — How to revert each change if needed.
4. **Impact Assessment** — What systems are affected by these changes.

Be precise. Include exact commands, file paths, config values.

Return ONLY valid JSON:
{
  "summary": "Overview paragraph",
  "changes": [
    {
      "target": "node or service name",
      "description": "what changed",
      "reason": "why",
      "files": ["file1", "file2"],
      "rollback": "how to revert"
    }
  ],
  "impact": "Impact assessment paragraph"
}

SESSION REPORTS:
""",
    "decision_record": """\
Write a Technical Decision Record (TDR) for PureTensor Inc based on these session reports.

Structure:
1. **Decision** — Clear statement of what was decided.
2. **Context** — What problem or situation prompted this decision?
3. **Options Considered** — What alternatives were evaluated?
4. **Rationale** — Why was this option chosen? What trade-offs were accepted?
5. **Consequences** — What are the implications? What does this enable or preclude?
6. **Implementation Notes** — Key details about how it was implemented.

Be analytical and precise. This document should help future readers understand WHY.

Return ONLY valid JSON:
{
  "decision": "Clear decision statement",
  "context": "Context paragraph",
  "options": [
    {"name": "Option name", "description": "Description", "pros": ["pro1"], "cons": ["con1"]}
  ],
  "rationale": "Why this was chosen",
  "consequences": "Implications paragraph",
  "implementation": "Implementation notes"
}

SESSION REPORTS:
""",
}

SYSTEM_PROMPT = (
    "You are a technical documentation specialist for PureTensor Inc. "
    "You produce structured JSON documents from raw session logs. "
    "Return ONLY valid JSON, no markdown fences, no explanation text."
)


def _make_doc_pdf_class(doc_type: str, title: str, date_str: str):
    """Create a PDF class for a documentation document."""
    from fpdf import FPDF

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%d %B %Y")
    except ValueError:
        date_display = date_str

    type_label = DOC_TYPE_TITLES.get(doc_type, "Document")
    doc_title = f"{type_label} — {date_display}"

    class _PDF(FPDF):
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

    pdf = _PDF()

    # Cover page
    pdf.add_page()
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(*DARK_BLUE)
    pdf.cell(0, 5, "PureTensor Inc", align="R", ln=True)
    pdf.cell(0, 5, "131 Continental Dr, Suite 305", align="R", ln=True)
    pdf.cell(0, 5, "Newark, DE 19713, US", align="R", ln=True)
    pdf.ln(35)

    # Accent line
    pdf.set_draw_color(*ACCENT_BLUE)
    pdf.set_line_width(1.5)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(10)

    # Type label
    pdf.set_font("DejaVu", "B", 14)
    pdf.set_text_color(*ACCENT_BLUE)
    pdf.cell(0, 10, type_label, align="C", ln=True)
    pdf.ln(4)

    # Title
    pdf.set_font("DejaVu", "B", 24)
    pdf.set_text_color(*DARK_BLUE)
    pdf.multi_cell(0, 14, title, align="C")
    pdf.ln(6)

    # Date
    pdf.set_font("DejaVu", "", 16)
    pdf.set_text_color(*META_GREY)
    pdf.cell(0, 10, date_display, align="C", ln=True)
    pdf.ln(6)

    # Bottom accent line
    pdf.set_draw_color(*ACCENT_BLUE)
    pdf.set_line_width(1.5)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(20)

    # Meta
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(*LIGHT_GREY)
    gen_time = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
    pdf.cell(0, 7, f"Generated: {gen_time}", align="C", ln=True)
    pdf.ln(10)
    pdf.set_font("DejaVu", "B", 10)
    pdf.set_text_color(*DARK_BLUE)
    pdf.cell(0, 7, "CONFIDENTIAL", align="C", ln=True)

    return pdf


class DocCompilerObserver(Observer):
    """Analyzes CC reports for significant work and generates standalone documentation."""

    name = "doc_compiler"
    schedule = "0 6 * * *"  # 06:00 UTC daily

    MAX_RETRIES = 2
    RETRY_DELAY = 15

    # -- Data gathering --------------------------------------------------------

    def _gather_reports(self, date_str: str) -> list[dict]:
        """Read CC session reports for the given date."""
        reports = []
        if not CC_REPORTS_DIR.exists():
            return reports

        for path in sorted(CC_REPORTS_DIR.glob(f"{date_str}*.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                name = path.stem
                parts = name.split("_", 2)
                topic = parts[2].replace("-", " ").replace("_", " ") if len(parts) > 2 else name
                reports.append({
                    "topic": topic,
                    "filename": path.name,
                    "content": content,
                })
            except Exception as e:
                log.warning("Failed to read %s: %s", path.name, e)
        return reports

    # -- LLM calls -------------------------------------------------------------

    def _call_gemini(self, prompt: str, system: str = "") -> str:
        """Call Gemini 2.5 Flash for synthesis."""
        from google import genai
        from google.genai import types

        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("Gemini: GOOGLE_API_KEY not set")

        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=8192,
            system_instruction=system or SYSTEM_PROMPT,
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        )
        return response.text or ""

    def _parse_json(self, text: str) -> dict | None:
        """Parse JSON from LLM response, handling markdown fences."""
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

    # -- Classification --------------------------------------------------------

    def _classify_reports(self, reports: list[dict]) -> list[dict]:
        """Ask Bedrock to classify reports into document-worthy categories."""
        prompt_parts = [CLASSIFICATION_PROMPT]
        for r in reports:
            content = r["content"]
            if len(content) > 6000:
                content = content[:6000] + "\n[... truncated ...]"
            prompt_parts.append(f"\n--- {r['filename']} ({r['topic']}) ---\n{content}\n")

        prompt = "\n".join(prompt_parts)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = self._call_gemini(prompt)
                parsed = self._parse_json(result)
                if parsed and "documents" in parsed:
                    return parsed["documents"]
                log.warning("Classification attempt %d: invalid response", attempt)
            except Exception as e:
                log.warning("Classification attempt %d failed: %s", attempt, e)
            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY)

        return []

    # -- Document synthesis ----------------------------------------------------

    def _synthesize_document(self, doc_spec: dict, reports: list[dict]) -> dict | None:
        """Generate document content for a classified item."""
        doc_type = doc_spec["type"]
        if doc_type not in SYNTHESIS_PROMPTS:
            log.warning("Unknown doc type: %s", doc_type)
            return None

        # Filter to only the relevant session files
        relevant = [r for r in reports if r["filename"] in doc_spec.get("sessions", [])]
        if not relevant:
            relevant = reports  # fallback to all if filenames don't match

        prompt_parts = [SYNTHESIS_PROMPTS[doc_type]]
        for r in relevant:
            content = r["content"]
            if len(content) > 8000:
                content = content[:8000] + "\n[... truncated ...]"
            prompt_parts.append(f"\n--- {r['filename']} ({r['topic']}) ---\n{content}\n")

        prompt = "\n".join(prompt_parts)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = self._call_gemini(prompt)
                parsed = self._parse_json(result)
                if parsed:
                    return parsed
                log.warning("Synthesis attempt %d: invalid JSON", attempt)
            except Exception as e:
                log.warning("Synthesis attempt %d failed: %s", attempt, e)
            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY)

        return None

    # -- PDF generation --------------------------------------------------------

    def _generate_pdf(self, doc_type: str, title: str, data: dict,
                      date_str: str) -> str | None:
        """Generate a branded PDF for the document."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
        filename = f"PureTensor_{DOC_TYPE_TITLES[doc_type].replace(' ', '_')}_{date_str}_{slug}.pdf"
        output_path = str(OUTPUT_DIR / filename)

        try:
            pdf = _make_doc_pdf_class(doc_type, title, date_str)

            # Content page
            pdf.add_page()

            if doc_type == "project_summary":
                self._render_project_summary(pdf, data)
            elif doc_type == "infra_changelog":
                self._render_infra_changelog(pdf, data)
            elif doc_type == "decision_record":
                self._render_decision_record(pdf, data)

            pdf.output(output_path)
            log.info("PDF generated: %s", output_path)
            return output_path

        except Exception as e:
            log.error("PDF generation failed for %s: %s", title, e)
            return None

    def _h1(self, pdf, text: str):
        pdf.ln(8)
        pdf.set_font("DejaVu", "B", 18)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(0, 11, text, ln=True)
        pdf.set_draw_color(*ACCENT_BLUE)
        pdf.set_line_width(0.8)
        pdf.line(10, pdf.get_y() + 1, 200, pdf.get_y() + 1)
        pdf.ln(6)

    def _h2(self, pdf, text: str):
        pdf.ln(5)
        pdf.set_font("DejaVu", "B", 13)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(0, 8, text, ln=True)
        pdf.ln(3)

    def _body(self, pdf, text: str):
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(*BODY_GREY)
        pdf.multi_cell(0, 6, text)
        pdf.ln(2)

    def _bullet(self, pdf, text: str):
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(*BODY_GREY)
        pdf.cell(6, 6, "\u2022")
        pdf.multi_cell(0, 6, text)
        pdf.ln(1)

    def _bold_bullet(self, pdf, label: str, text: str):
        pdf.set_text_color(*BODY_GREY)
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(6, 6, "\u2022")
        pdf.set_font("DejaVu", "B", 10)
        label_w = pdf.get_string_width(label + " ")
        pdf.cell(label_w, 6, label + " ")
        pdf.set_font("DejaVu", "", 10)
        pdf.multi_cell(0, 6, text)
        pdf.ln(1)

    def _render_project_summary(self, pdf, data: dict):
        """Render project summary content."""
        for section in data.get("sections", []):
            self._h1(pdf, section.get("heading", ""))
            content = section.get("content", "")
            if content:
                self._body(pdf, content)
            for b in section.get("bullets", []):
                self._bullet(pdf, b)

    def _render_infra_changelog(self, pdf, data: dict):
        """Render infrastructure change log content."""
        summary = data.get("summary", "")
        if summary:
            self._h1(pdf, "Summary")
            self._body(pdf, summary)

        changes = data.get("changes", [])
        if changes:
            self._h1(pdf, "Changes")
            for i, change in enumerate(changes, 1):
                self._h2(pdf, f"{i}. {change.get('target', 'Unknown')}")
                self._body(pdf, change.get("description", ""))
                if change.get("reason"):
                    self._bold_bullet(pdf, "Reason:", change["reason"])
                for f in change.get("files", []):
                    self._bullet(pdf, f"File: {f}")
                if change.get("rollback"):
                    self._bold_bullet(pdf, "Rollback:", change["rollback"])

        impact = data.get("impact", "")
        if impact:
            self._h1(pdf, "Impact Assessment")
            self._body(pdf, impact)

    def _render_decision_record(self, pdf, data: dict):
        """Render technical decision record content."""
        if data.get("decision"):
            self._h1(pdf, "Decision")
            self._body(pdf, data["decision"])

        if data.get("context"):
            self._h1(pdf, "Context")
            self._body(pdf, data["context"])

        options = data.get("options", [])
        if options:
            self._h1(pdf, "Options Considered")
            for opt in options:
                self._h2(pdf, opt.get("name", ""))
                if opt.get("description"):
                    self._body(pdf, opt["description"])
                for pro in opt.get("pros", []):
                    self._bullet(pdf, f"+ {pro}")
                for con in opt.get("cons", []):
                    self._bullet(pdf, f"- {con}")

        if data.get("rationale"):
            self._h1(pdf, "Rationale")
            self._body(pdf, data["rationale"])

        if data.get("consequences"):
            self._h1(pdf, "Consequences")
            self._body(pdf, data["consequences"])

        if data.get("implementation"):
            self._h1(pdf, "Implementation Notes")
            self._body(pdf, data["implementation"])

    # -- Drive upload ----------------------------------------------------------

    def _upload_to_drive(self, pdf_path: str, folder_id: str) -> str | None:
        """Upload PDF to Google Drive. Returns web view link or None."""
        if not folder_id:
            log.warning("No Drive folder ID for upload, skipping")
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
            file_metadata = {"name": Path(pdf_path).name, "parents": [folder_id]}
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
        return state_dir / "doc_compiler_state.json"

    def _get_last_date(self) -> str:
        sf = self._state_file()
        if sf.exists():
            try:
                return json.loads(sf.read_text()).get("last_compiled_date", "")
            except Exception:
                pass
        return ""

    def _set_last_date(self, date_str: str):
        self._state_file().write_text(json.dumps({"last_compiled_date": date_str}))

    # -- Observer interface ----------------------------------------------------

    def run(self, ctx=None) -> ObserverResult:
        """Execute the documentation compiler pipeline."""
        now = self.now_utc()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

        log.info("Doc compiler starting for %s", date_str)

        # Prevent double-runs
        if self._get_last_date() == date_str:
            log.info("Doc compiler already ran for %s, skipping", date_str)
            return ObserverResult(success=True)

        # 1. Gather reports
        reports = self._gather_reports(date_str)
        if len(reports) < 2:
            log.info("Only %d reports for %s — nothing to compile", len(reports), date_str)
            self._set_last_date(date_str)
            return ObserverResult(success=True)

        # 2. Classify
        classifications = self._classify_reports(reports)
        if not classifications:
            log.info("No significant work detected for %s — silent success", date_str)
            self._set_last_date(date_str)
            return ObserverResult(success=True)

        log.info("Classified %d document-worthy items for %s", len(classifications), date_str)

        # 3. Generate each document
        generated = []
        for doc_spec in classifications:
            doc_type = doc_spec.get("type", "")
            title = doc_spec.get("title", "Untitled")

            if doc_type not in DOC_TYPE_TITLES:
                log.warning("Skipping unknown doc type: %s", doc_type)
                continue

            log.info("Synthesizing %s: %s", doc_type, title)

            # Synthesize content
            content = self._synthesize_document(doc_spec, reports)
            if not content:
                log.warning("Synthesis failed for %s: %s", doc_type, title)
                continue

            # Generate PDF
            pdf_path = self._generate_pdf(doc_type, title, content, date_str)
            if not pdf_path:
                continue

            # Upload to Drive
            folder_id = DOC_TYPE_FOLDERS.get(doc_type, "")
            drive_link = self._upload_to_drive(pdf_path, folder_id)

            generated.append({
                "type": doc_type,
                "title": title,
                "pdf_path": pdf_path,
                "drive_link": drive_link,
            })

        # 4. Telegram notification
        if generated:
            lines = [f"DOC COMPILER — {date_str}", ""]
            for doc in generated:
                type_label = DOC_TYPE_TITLES.get(doc["type"], doc["type"])
                lines.append(f"  {type_label}: {doc['title']}")
                if doc.get("drive_link"):
                    lines.append(f"    Drive: {doc['drive_link']}")
                else:
                    lines.append(f"    (Drive upload failed)")
            self.send_telegram("\n".join(lines))
        else:
            log.info("No documents generated for %s after synthesis", date_str)

        self._set_last_date(date_str)

        return ObserverResult(
            success=True,
            message=f"Doc compiler: {len(generated)} documents for {date_str}",
            data={"date": date_str, "documents": generated},
        )


# Standalone execution for testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import log as _  # noqa: F401

    observer = DocCompilerObserver()

    if len(sys.argv) > 1:
        test_date = sys.argv[1]
        fake_now = datetime.strptime(test_date, "%Y-%m-%d").replace(
            hour=6, minute=0, tzinfo=timezone.utc
        ) + timedelta(days=1)
        observer.now_utc = lambda: fake_now
        sf = observer._state_file()
        if sf.exists():
            data = json.loads(sf.read_text())
            if data.get("last_compiled_date") == test_date:
                sf.unlink()

    result = observer.run()
    if result.success:
        print(f"Doc compiler completed: {result.message}")
    else:
        print(f"Doc compiler failed: {result.error}", file=sys.stderr)
        sys.exit(1)
