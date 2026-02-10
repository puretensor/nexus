"""Tests for observers/followup_reminder.py — follow-up reminder observer."""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.followup_reminder import FollowupReminderObserver
    from db import init_db, create_followup, update_followup_reminded, resolve_followup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_followup.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    init_db()
    yield db_file


# ---------------------------------------------------------------------------
# Observer tests
# ---------------------------------------------------------------------------


class TestFollowupReminderObserver:

    def test_no_followups_silent_success(self):
        obs = FollowupReminderObserver()
        result = obs.run()
        assert result.success
        assert not result.message

    def test_schedule(self):
        obs = FollowupReminderObserver()
        assert obs.schedule == "0 9 * * 1-5"

    @patch("observers.base.urllib.request.urlopen")
    def test_reminds_overdue_followup(self, mock_urlopen):
        """Followups older than reminder_days should trigger a reminder."""
        # Create a followup that was "sent" 5 days ago
        fid = create_followup(
            chat_id=12345,
            email_to="client@example.com",
            email_subject="Proposal",
            email_message_id="msg-fu-1",
            reminder_days=3,
        )
        # Backdate the sent_at
        from db import _connect
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        con = _connect()
        con.execute("UPDATE followups SET sent_at = ? WHERE id = ?", (old_date, fid))
        con.commit()
        con.close()

        obs = FollowupReminderObserver()
        result = obs.run()

        assert result.success
        assert "1" in result.message
        mock_urlopen.assert_called_once()  # Telegram notification sent

    @patch("observers.base.urllib.request.urlopen")
    def test_skips_recent_followup(self, mock_urlopen):
        """Followups newer than reminder_days should NOT trigger a reminder."""
        fid = create_followup(
            chat_id=12345,
            email_to="person@example.com",
            email_subject="Quick question",
            email_message_id="msg-fu-2",
            reminder_days=3,
        )
        # sent_at is "now" by default — only 0 days ago
        obs = FollowupReminderObserver()
        result = obs.run()

        assert result.success
        assert not result.message  # Silent success, nothing due
        mock_urlopen.assert_not_called()

    @patch("observers.base.urllib.request.urlopen")
    def test_skips_resolved_followup(self, mock_urlopen):
        """Resolved followups should not trigger reminders."""
        fid = create_followup(
            chat_id=12345,
            email_to="done@example.com",
            email_subject="Done deal",
            email_message_id="msg-fu-3",
            reminder_days=1,
        )
        # Backdate and resolve
        from db import _connect
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        con = _connect()
        con.execute("UPDATE followups SET sent_at = ? WHERE id = ?", (old_date, fid))
        con.commit()
        con.close()
        resolve_followup(fid)

        obs = FollowupReminderObserver()
        result = obs.run()

        assert result.success
        assert not result.message
        mock_urlopen.assert_not_called()

    @patch("observers.base.urllib.request.urlopen")
    def test_skips_already_reminded_today(self, mock_urlopen):
        """Followups already reminded today should be skipped."""
        fid = create_followup(
            chat_id=12345,
            email_to="nagged@example.com",
            email_subject="Nagged",
            email_message_id="msg-fu-4",
            reminder_days=1,
        )
        # Backdate sent_at and set last_reminded to now
        from db import _connect
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        con = _connect()
        con.execute("UPDATE followups SET sent_at = ? WHERE id = ?", (old_date, fid))
        con.commit()
        con.close()
        update_followup_reminded(fid)

        obs = FollowupReminderObserver()
        result = obs.run()

        assert result.success
        assert not result.message
        mock_urlopen.assert_not_called()

    @patch("observers.base.urllib.request.urlopen")
    def test_multiple_overdue(self, mock_urlopen):
        """Multiple overdue followups should all be included in reminder."""
        from db import _connect
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        for i in range(3):
            fid = create_followup(
                chat_id=12345,
                email_to=f"person{i}@example.com",
                email_subject=f"Topic {i}",
                email_message_id=f"msg-fu-multi-{i}",
                reminder_days=2,
            )
            con = _connect()
            con.execute("UPDATE followups SET sent_at = ? WHERE id = ?", (old_date, fid))
            con.commit()
            con.close()

        obs = FollowupReminderObserver()
        result = obs.run()

        assert result.success
        assert "3" in result.message
