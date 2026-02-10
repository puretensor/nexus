#!/usr/bin/env python3
"""Bretalon Article Review Observer — sends review emails before scheduled publishes.

Checks WordPress for articles scheduled to publish within the next 36 hours.
Sends a review email from REDACTED_HH_EMAIL to Alan and Heimir before publish.
Tracks sent emails in a state file to avoid duplicates.

Schedule: every 2 hours (0 */2 * * *)
"""

import json
import logging
import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from observers.base import Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")


class BretalonReviewObserver(Observer):
    """Sends review emails for Bretalon articles approaching their publish date."""

    name = "bretalon_review"
    schedule = "0 */2 * * *"  # every 2 hours

    # -- WordPress / SSH --
    SSH_HOST = "gcp-medium"
    WP_CONTAINER = "bretalon-wordpress"

    # WordPress PST offset (UTC-8)
    WP_TZ_OFFSET = timedelta(hours=-8)

    # Send review email when post is within this many hours of publishing
    SEND_WINDOW_HOURS = 36

    # State file for tracking already-emailed post IDs
    STATE_FILE = Path(__file__).parent / ".state" / "bretalon_review_sent.json"

    # -- SMTP config (env var overrides with sensible defaults) --

    @property
    def smtp_host(self):
        return os.environ.get("BRETALON_SMTP_HOST", "mail.privateemail.com")

    @property
    def smtp_port(self):
        return int(os.environ.get("BRETALON_SMTP_PORT", "587"))

    @property
    def smtp_user(self):
        return os.environ.get("BRETALON_SMTP_USER", "REDACTED_HH_EMAIL")

    @property
    def smtp_pass(self):
        return os.environ.get("BRETALON_SMTP_PASS", "")

    @property
    def sender_from(self):
        return os.environ.get("BRETALON_FROM", "REDACTED_NAME_SHORT <REDACTED_HH_EMAIL>")

    @property
    def recipients(self):
        raw = os.environ.get("BRETALON_TO", "REDACTED_ALAN_EMAIL,REDACTED_HH_EMAIL")
        return [r.strip() for r in raw.split(",") if r.strip()]

    # ── State helpers ──────────────────────────────────────────────────────

    def _load_state(self) -> set:
        """Load set of already-emailed post IDs."""
        if self.STATE_FILE.exists():
            try:
                return set(str(x) for x in json.loads(self.STATE_FILE.read_text()))
            except (json.JSONDecodeError, TypeError):
                return set()
        return set()

    def _save_state(self, sent_ids: set) -> None:
        """Persist set of emailed post IDs."""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE_FILE.write_text(json.dumps(sorted(sent_ids)))

    # ── SSH / WordPress helpers ────────────────────────────────────────────

    def _ssh_cmd(self, cmd: str, timeout: int = 30) -> str:
        """Run command on gcp-medium via SSH."""
        result = subprocess.run(
            ["ssh", self.SSH_HOST, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()

    def get_scheduled_posts(self) -> list:
        """Fetch scheduled (future) posts from WordPress as list of dicts."""
        raw = self._ssh_cmd(
            f"sudo docker exec {self.WP_CONTAINER} wp post list "
            f"--post_type=post --post_status=future "
            f"--fields=ID,post_title,post_date --format=json --allow-root"
        )
        # Strip PHP warnings — only keep lines starting with [
        lines = [line for line in raw.splitlines() if line.strip().startswith("[")]
        if not lines:
            return []
        return json.loads(lines[0])

    def get_post_content(self, post_id: str) -> str:
        """Fetch post content from WordPress."""
        raw = self._ssh_cmd(
            f"sudo docker exec {self.WP_CONTAINER} wp post get {post_id} "
            f"--field=post_content --allow-root",
            timeout=15,
        )
        # Strip PHP warnings
        lines = raw.splitlines()
        content_lines = [line for line in lines if not line.startswith("[")]
        return "\n".join(content_lines)

    @staticmethod
    def strip_gutenberg(html: str) -> str:
        """Strip Gutenberg block comments and HTML tags to get plain text."""
        text = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
        text = re.sub(r'<h[1-6][^>]*>', '\n\n', text)
        text = re.sub(r'</h[1-6]>', '\n', text)
        text = re.sub(r'<p[^>]*>', '', text)
        text = re.sub(r'</p>', '\n\n', text)
        text = re.sub(r'<hr[^>]*/?\s*>', '\n---\n', text)
        text = re.sub(r'<div[^>]*aria-hidden[^>]*>.*?</div>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        # Decode entities
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&#8217;', "'").replace('&#8216;', "'")
        text = text.replace('&#8220;', '"').replace('&#8221;', '"')
        text = text.replace('&mdash;', '\u2014').replace('&ndash;', '\u2013')
        # Clean up whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def count_words(text: str) -> int:
        """Rough word count."""
        return len(text.split())

    @staticmethod
    def wp_date_to_utc(date_str: str) -> datetime:
        """Convert WordPress date string (PST, UTC-8) to UTC datetime."""
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        # WordPress is in PST (UTC-8); subtract negative offset = add 8h
        dt_utc = dt - timedelta(hours=-8)
        return dt_utc.replace(tzinfo=timezone.utc)

    @staticmethod
    def _format_publish_date(date_str: str) -> str:
        """Format publish date for email display."""
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%A %d %B %Y, %H:%M PST")

    # ── Email ──────────────────────────────────────────────────────────────

    def send_review_email(self, post_id: str, title: str,
                          publish_date_str: str, plain_text: str) -> str:
        """Send review email via SMTP. Returns the subject line."""
        publish_display = self._format_publish_date(publish_date_str)
        word_count = self.count_words(plain_text)

        # Trim the disclaimer from the plain text for the review email
        disclaimer_marker = "Disclaimer: Important Legal"
        if disclaimer_marker in plain_text:
            body_text = plain_text[:plain_text.index(disclaimer_marker)].rstrip(" \n-")
        else:
            body_text = plain_text

        subject = f"[REVIEW] {title} \u2014 Scheduled {publish_display.split(',')[0].strip()}"

        # Parse sender name and email from BRETALON_FROM
        sender_from = self.sender_from
        if "<" in sender_from and ">" in sender_from:
            sender_name = sender_from.split("<")[0].strip()
            sender_email = sender_from.split("<")[1].rstrip(">").strip()
        else:
            sender_name = ""
            sender_email = sender_from

        email_body = (
            f"Alan,\n\n"
            f"Article below for your review. Scheduled for {publish_display} "
            f"on bretalon.com under Reports. "
            f"Please reply with any edits or approval \u2014 it will auto-publish "
            f"at the scheduled time unless changes are requested.\n\n"
            f"{title}\n"
            f"{body_text}\n\n"
            f"Scheduled:\t{publish_display}\n"
            f"Category:\tReports\n"
            f"Word count:\t~{round(word_count, -1):,}\n"
            f"Post ID:\t{post_id}\n\n"
            f"Best,\n"
            f"{sender_name or 'REDACTED_NAME_SHORT'}"
        )

        msg = MIMEMultipart("alternative")
        msg["From"] = sender_from
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(email_body, "plain", "utf-8"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_pass)
            server.sendmail(sender_email, self.recipients, msg.as_string())

        return subject

    # ── Observer entry point ───────────────────────────────────────────────

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Check for scheduled Bretalon posts and send review emails."""
        now = ctx.now
        window_end = now + timedelta(hours=self.SEND_WINDOW_HOURS)

        # Validate SMTP password is configured
        if not self.smtp_pass:
            return ObserverResult(
                success=False,
                error="BRETALON_SMTP_PASS not set — cannot send review emails",
            )

        try:
            posts = self.get_scheduled_posts()
        except Exception as e:
            msg = f"Failed to fetch scheduled posts: {e}"
            self.send_telegram(f"[bretalon_review] {msg}")
            return ObserverResult(success=False, error=msg)

        if not posts:
            return ObserverResult(
                success=True,
                data={"scheduled_posts": 0, "emails_sent": 0},
            )

        sent_ids = self._load_state()
        emails_sent = []
        errors = []

        for post in posts:
            pid = str(post["ID"])
            title = post["post_title"]
            pub_date = post["post_date"]
            pub_utc = self.wp_date_to_utc(pub_date)

            # Skip already emailed
            if pid in sent_ids:
                continue

            # Skip posts already past publish date
            if pub_utc <= now:
                continue

            # Skip posts outside the send window
            if pub_utc > window_end:
                continue

            # Within window — fetch content and send review email
            try:
                content = self.get_post_content(pid)
                plain = self.strip_gutenberg(content)
                subj = self.send_review_email(pid, title, pub_date, plain)
                sent_ids.add(pid)
                self._save_state(sent_ids)
                emails_sent.append(subj)
                log.info("bretalon_review: sent review for post %s: %s", pid, title)
            except Exception as e:
                err = f"Post {pid} ({title[:50]}): {e}"
                errors.append(err)
                log.error("bretalon_review: %s", err)

        # Report errors via Telegram
        if errors:
            self.send_telegram(
                f"[bretalon_review] Errors sending review emails:\n"
                + "\n".join(errors)
            )

        # Build result
        if errors and not emails_sent:
            return ObserverResult(
                success=False,
                error=f"All review emails failed: {'; '.join(errors)}",
                data={"scheduled_posts": len(posts), "emails_sent": 0},
            )

        message = ""
        if emails_sent:
            message = (
                f"Sent {len(emails_sent)} review email(s):\n"
                + "\n".join(f"  - {s}" for s in emails_sent)
            )

        return ObserverResult(
            success=True,
            message=message,
            error="; ".join(errors) if errors else "",
            data={
                "scheduled_posts": len(posts),
                "emails_sent": len(emails_sent),
                "errors": len(errors),
            },
        )


# ── Standalone testing ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="Bretalon review mailer (standalone)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent")
    parser.add_argument("--force", action="store_true", help="Ignore already-sent state")
    args = parser.parse_args()

    obs = BretalonReviewObserver()
    now = datetime.now(timezone.utc)

    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] "
          f"Checking scheduled Bretalon articles...")

    if not obs.smtp_pass:
        print("WARNING: BRETALON_SMTP_PASS not set. Set it or export the env var.")
        sys.exit(1)

    try:
        posts = obs.get_scheduled_posts()
    except Exception as e:
        print(f"ERROR fetching posts: {e}", file=sys.stderr)
        sys.exit(1)

    if not posts:
        print("  No scheduled posts found.")
        sys.exit(0)

    print(f"  Found {len(posts)} scheduled post(s).")

    sent_ids = obs._load_state()
    window_end = now + timedelta(hours=obs.SEND_WINDOW_HOURS)

    for post in posts:
        pid = str(post["ID"])
        title = post["post_title"]
        pub_date = post["post_date"]
        pub_utc = obs.wp_date_to_utc(pub_date)

        if pid in sent_ids and not args.force:
            print(f"  [{pid}] Already emailed: {title[:60]}...")
            continue

        if pub_utc <= now:
            print(f"  [{pid}] Already past publish date: {title[:60]}...")
            continue

        if pub_utc > window_end and not args.force:
            hours_until = (pub_utc - now).total_seconds() / 3600
            print(f"  [{pid}] Publishes in {hours_until:.0f}h, "
                  f"outside {obs.SEND_WINDOW_HOURS}h window: {title[:60]}...")
            continue

        print(f"  [{pid}] SENDING review for: {title}")
        content = obs.get_post_content(pid)
        plain = obs.strip_gutenberg(content)

        if args.dry_run:
            print(f"    [DRY RUN] Would send: [REVIEW] {title}")
            print(f"    Word count: ~{obs.count_words(plain)}")
            continue

        try:
            subj = obs.send_review_email(pid, title, pub_date, plain)
            sent_ids.add(pid)
            obs._save_state(sent_ids)
            print(f"    Sent: {subj}")
            print(f"    To: {', '.join(obs.recipients)}")
        except Exception as e:
            print(f"    ERROR sending email for {pid}: {e}", file=sys.stderr)

    print("Done.")
