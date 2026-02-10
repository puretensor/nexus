"""Tests for channels/email_in.py — EmailInputChannel.

Covers:
- IMAP email fetching (mocked)
- Email classification routing (ignore/notify/auto_reply)
- Draft creation for auto_reply
- Telegram notifications for notify
- Deduplication via email_seen
- Header decoding and body extraction
"""

import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from channels.email_in import (
        EmailInputChannel,
        fetch_new_emails,
        _decode_header,
        _extract_email_addr,
        _get_body,
    )
    from db import init_db, is_email_seen, mark_email_seen


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh temp database for each test."""
    db_file = tmp_path / "test_email_in.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    monkeypatch.setattr("drafts.queue.AUTHORIZED_USER_ID", 12345)
    init_db()
    yield db_file


@pytest.fixture
def accounts_file(tmp_path, monkeypatch):
    """Create a temporary email accounts file."""
    accts = [
        {
            "name": "test-acct",
            "server": "imap.example.com",
            "port": 993,
            "username": "test@example.com",
            "password": "secret",
        }
    ]
    path = tmp_path / "email_accounts.json"
    path.write_text(json.dumps(accts))
    monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", path)
    return path


# ---------------------------------------------------------------------------
# Header decoding helpers
# ---------------------------------------------------------------------------


class TestDecodeHeader:

    def test_plain_header(self):
        assert _decode_header("Hello World") == "Hello World"

    def test_empty_header(self):
        assert _decode_header("") == ""

    def test_none_header(self):
        assert _decode_header(None) == ""


class TestExtractEmailAddr:

    def test_bare_address(self):
        assert _extract_email_addr("user@example.com") == "user@example.com"

    def test_display_name_with_angle_brackets(self):
        assert _extract_email_addr("John Doe <john@example.com>") == "john@example.com"

    def test_empty_string(self):
        assert _extract_email_addr("") == ""

    def test_none_value(self):
        assert _extract_email_addr(None) == ""


class TestGetBody:

    def test_plain_text_message(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_payload.return_value = b"Hello plain text"
        msg.get_content_charset.return_value = "utf-8"
        assert _get_body(msg) == "Hello plain text"

    def test_empty_payload(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_payload.return_value = None
        assert _get_body(msg) == ""


# ---------------------------------------------------------------------------
# EmailInputChannel — poll_once routing
# ---------------------------------------------------------------------------


class TestPollOnceClassification:

    @pytest.mark.asyncio
    async def test_ignore_skips_notification(self, accounts_file):
        """Emails classified as 'ignore' should not send any notification."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-ignore-1",
            "from": "noreply@example.com",
            "from_addr": "noreply@example.com",
            "subject": "Your order shipped",
            "date": "Feb 10 12:00",
            "to": "ops@puretensor.ai",
            "body": "Your package is on the way.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            await channel._poll_once()

        # Should not send notification for ignored emails
        mock_bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_sends_telegram(self, accounts_file):
        """Emails classified as 'notify' should send a Telegram notification."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-notify-1",
            "from": "receipts@stripe.com",
            "from_addr": "receipts@stripe.com",
            "subject": "Payment receipt",
            "date": "Feb 10 12:00",
            "to": "ops@puretensor.ai",
            "body": "You received a payment of $100.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            await channel._poll_once()

        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert "stripe.com" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_auto_reply_creates_draft(self, accounts_file):
        """Emails classified as 'auto_reply' should call Claude and create a draft."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-auto-1",
            "from": "Alan Apter <alan.apter@bretalon.com>",
            "from_addr": "alan.apter@bretalon.com",
            "subject": "Report feedback",
            "date": "Feb 10 12:00",
            "to": "hal@puretensor.ai",
            "body": "The report looks good, please update section 3.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]), \
             patch("engine.call_sync", return_value={"result": "Thank you for the feedback, Alan."}), \
             patch("channels.email_in.create_email_draft", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = 1
            await channel._poll_once()

            mock_create.assert_awaited_once()
            call_kwargs = mock_create.call_args[1]
            assert call_kwargs["email_from"] == "alan.apter@bretalon.com"
            assert call_kwargs["email_subject"] == "Report feedback"
            assert "Thank you" in call_kwargs["draft_body"]

    @pytest.mark.asyncio
    async def test_auto_reply_fallback_on_claude_failure(self, accounts_file):
        """If Claude fails, auto_reply should fall back to notification."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-auto-fail",
            "from": "ops@puretensor.ai",
            "from_addr": "ops@puretensor.ai",
            "subject": "Server question",
            "date": "Feb 10 12:00",
            "to": "hal@puretensor.ai",
            "body": "What is the status of tensor-core?",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]), \
             patch("engine.call_sync", side_effect=RuntimeError("Claude unavailable")):
            await channel._poll_once()

        # Should fall back to notification
        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert "ops@puretensor.ai" in call_kwargs["text"]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:

    @pytest.mark.asyncio
    async def test_already_seen_email_skipped(self, accounts_file):
        """Emails already marked as seen should be skipped."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        # Pre-mark as seen
        mark_email_seen("msg-dup-1", "test-acct")

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-dup-1",
            "from": "receipts@stripe.com",
            "from_addr": "receipts@stripe.com",
            "subject": "Payment receipt",
            "date": "Feb 10 12:00",
            "to": "ops@puretensor.ai",
            "body": "Payment.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            await channel._poll_once()

        # Should not send notification for already-seen email
        mock_bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_email_marked_seen(self, accounts_file):
        """Processing a new email should mark it as seen."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-new-1",
            "from": "billing@provider.com",
            "from_addr": "billing@provider.com",
            "subject": "Your invoice",
            "date": "Feb 10 12:00",
            "to": "ops@puretensor.ai",
            "body": "Invoice attached.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            await channel._poll_once()

        assert is_email_seen("msg-new-1") is True


# ---------------------------------------------------------------------------
# Channel start/stop
# ---------------------------------------------------------------------------


class TestChannelLifecycle:

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        """start() should create a polling task."""
        channel = EmailInputChannel()
        with patch.object(channel, "_poll_loop", new_callable=AsyncMock):
            await channel.start()
            assert channel._task is not None
            # Clean up
            await channel.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        """stop() should cancel the polling task."""
        channel = EmailInputChannel()
        with patch.object(channel, "_poll_loop", new_callable=AsyncMock):
            await channel.start()
            task = channel._task
            await channel.stop()
            assert task.cancelled()


# ---------------------------------------------------------------------------
# No accounts file
# ---------------------------------------------------------------------------


class TestNoAccountsFile:

    @pytest.mark.asyncio
    async def test_poll_with_no_accounts_file(self, tmp_path, monkeypatch):
        """If accounts file doesn't exist, poll_once should return silently."""
        monkeypatch.setattr(
            "channels.email_in.ACCOUNTS_FILE",
            tmp_path / "nonexistent.json",
        )
        channel = EmailInputChannel()
        # Should not raise
        await channel._poll_once()


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


class TestNotificationFormatting:

    @pytest.mark.asyncio
    async def test_notify_includes_from_and_subject(self, accounts_file):
        """Notification text should include sender and subject."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=mock_bot)

        fake_email = {
            "id": "msg-fmt-1",
            "from": "security@bank.com",
            "from_addr": "security@bank.com",
            "subject": "Activity alert",
            "date": "Feb 10 14:30",
            "to": "ops@puretensor.ai",
            "body": "Unusual login detected from a new device.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            await channel._poll_once()

        text = mock_bot.send_message.call_args[1]["text"]
        assert "security@bank.com" in text
        assert "Activity alert" in text
        assert "[EMAIL]" in text

    @pytest.mark.asyncio
    async def test_no_bot_no_notification(self, accounts_file):
        """If no bot is set, notification should be silently skipped."""
        channel = EmailInputChannel(bot=None)

        fake_email = {
            "id": "msg-nobot-1",
            "from": "billing@provider.com",
            "from_addr": "billing@provider.com",
            "subject": "Invoice",
            "date": "Feb 10 12:00",
            "to": "ops@puretensor.ai",
            "body": "Invoice attached.",
        }

        with patch("channels.email_in.fetch_new_emails", return_value=[fake_email]):
            # Should not raise even with no bot
            await channel._poll_once()
