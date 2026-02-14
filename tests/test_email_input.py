"""Tests for channels/email_in.py — email input channel."""

import asyncio
import json
import sys
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
    from db import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Use a fresh temp database for each test."""
    db_file = tmp_path / "test_email_in.db"
    monkeypatch.setattr("db.DB_PATH", db_file)
    init_db()
    yield db_file


# ---------------------------------------------------------------------------
# Header decoding
# ---------------------------------------------------------------------------


class TestDecodeHeader:

    def test_plain_text(self):
        assert _decode_header("Hello World") == "Hello World"

    def test_empty(self):
        assert _decode_header("") == ""

    def test_none(self):
        assert _decode_header(None) == ""


class TestExtractEmailAddr:

    def test_with_display_name(self):
        assert _extract_email_addr("John Doe <john@example.com>") == "john@example.com"

    def test_bare_email(self):
        assert _extract_email_addr("user@example.com") == "user@example.com"

    def test_empty(self):
        assert _extract_email_addr("") == ""


class TestGetBody:

    def test_plain_text_message(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_payload.return_value = b"Hello world"
        msg.get_content_charset.return_value = "utf-8"
        assert _get_body(msg) == "Hello world"

    def test_empty_payload(self):
        msg = MagicMock()
        msg.is_multipart.return_value = False
        msg.get_payload.return_value = None
        assert _get_body(msg) == ""


# ---------------------------------------------------------------------------
# EmailInputChannel
# ---------------------------------------------------------------------------


class TestEmailInputChannel:

    def test_init_with_bot(self):
        bot = MagicMock()
        channel = EmailInputChannel(bot=bot)
        assert channel._bot is bot

    def test_init_without_bot(self):
        channel = EmailInputChannel()
        assert channel._bot is None

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        channel = EmailInputChannel()
        # Mock to prevent actual polling
        with patch.object(channel, "_poll_loop", new_callable=AsyncMock):
            await channel.start()
            assert channel._task is not None
            await channel.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        channel = EmailInputChannel()
        with patch.object(channel, "_poll_loop", new_callable=AsyncMock):
            await channel.start()
            task = channel._task
            await channel.stop()
            assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_poll_once_no_accounts_file(self, tmp_path, monkeypatch):
        """If no accounts file exists, poll_once should do nothing."""
        monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", tmp_path / "nonexistent.json")
        channel = EmailInputChannel()
        # Should not raise
        await channel._poll_once()

    @pytest.mark.asyncio
    async def test_poll_once_classifies_and_notifies(self, tmp_path, monkeypatch):
        """Emails classified as 'notify' should send a Telegram message."""
        # Write fake accounts file
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.test.com",
            "username": "user@test.com",
            "password": "pass",
        }]))
        monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", accounts_file)

        # Mock fetch_new_emails to return a notify-classified email
        fake_email = {
            "id": "test-msg-001@test.com",
            "from": "random@company.com",
            "from_addr": "random@company.com",
            "subject": "Meeting next week",
            "date": "Feb 10 14:00",
            "to": "user@test.com",
            "body": "Hi, can we schedule a meeting?",
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)
        monkeypatch.setattr("channels.email_in.fetch_new_emails", lambda acc: [fake_email])

        await channel._poll_once()

        # Should have sent a notification
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert "random@company.com" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_poll_once_skips_already_seen(self, tmp_path, monkeypatch):
        """Already-seen messages should be skipped."""
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.test.com",
            "username": "user@test.com",
            "password": "pass",
        }]))
        monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", accounts_file)

        fake_email = {
            "id": "already-seen@test.com",
            "from": "person@company.com",
            "from_addr": "person@company.com",
            "subject": "Old message",
            "date": "Feb 10 10:00",
            "to": "user@test.com",
            "body": "Old content",
        }

        # Mark as seen first
        from db import mark_email_seen
        mark_email_seen("already-seen@test.com", "test")

        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)
        monkeypatch.setattr("channels.email_in.fetch_new_emails", lambda acc: [fake_email])

        await channel._poll_once()

        # Should NOT have sent any message
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_once_ignores_spam(self, tmp_path, monkeypatch):
        """Emails classified as 'ignore' should be silently skipped."""
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.test.com",
            "username": "user@test.com",
            "password": "pass",
        }]))
        monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", accounts_file)

        fake_email = {
            "id": "spam-msg@test.com",
            "from": "noreply@spammer.com",
            "from_addr": "noreply@spammer.com",
            "subject": "Buy our stuff",
            "date": "Feb 10 12:00",
            "to": "user@test.com",
            "body": "Spam content",
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)
        monkeypatch.setattr("channels.email_in.fetch_new_emails", lambda acc: [fake_email])

        await channel._poll_once()

        # Should NOT have sent any message (classified as ignore)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_once_auto_reply_creates_draft(self, tmp_path, monkeypatch):
        """Emails from VIP senders should trigger Claude draft creation."""
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([{
            "name": "test",
            "server": "imap.test.com",
            "username": "user@test.com",
            "password": "pass",
        }]))
        monkeypatch.setattr("channels.email_in.ACCOUNTS_FILE", accounts_file)
        monkeypatch.setattr("drafts.queue.AUTHORIZED_USER_ID", 12345)

        fake_email = {
            "id": "vip-msg@test.com",
            "from": "Alan Apter <alan.apter@bretalon.com>",
            "from_addr": "alan.apter@bretalon.com",
            "subject": "Report feedback",
            "date": "Feb 10 09:00",
            "to": "hal@puretensor.ai",
            "body": "Hi PureClaw, the latest report was excellent. Can you update the analysis?",
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)
        monkeypatch.setattr("channels.email_in.fetch_new_emails", lambda acc: [fake_email])

        # Mock call_sync (used by _create_auto_reply) to return a draft
        with patch("engine.call_sync", return_value={"result": "Thank you for the feedback, Alan."}):
            await channel._poll_once()

        # Should have called bot.send_message with approval buttons (from create_email_draft)
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert "alan.apter@bretalon.com" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] is not None


# ---------------------------------------------------------------------------
# Notification format
# ---------------------------------------------------------------------------


class TestNotification:

    @pytest.mark.asyncio
    async def test_send_notification_format(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)

        em = {
            "id": "fmt-test@test.com",
            "from": "sender@test.com",
            "from_addr": "sender@test.com",
            "subject": "Test subject",
            "date": "Feb 10 15:00",
            "to": "user@test.com",
            "body": "Some email body content here.",
        }

        await channel._send_notification(em)

        bot.send_message.assert_called_once()
        text = bot.send_message.call_args[1]["text"]
        assert "[EMAIL]" in text
        assert "sender@test.com" in text
        assert "Test subject" in text

    @pytest.mark.asyncio
    async def test_send_followup_notification(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        channel = EmailInputChannel(bot=bot)

        em = {
            "id": "fu-test@test.com",
            "from": "someone@test.com",
            "from_addr": "someone@test.com",
            "subject": "Follow-up needed",
            "date": "Feb 10 16:00",
            "to": "user@test.com",
            "body": "",
        }

        await channel._send_notification(em, followup=True)

        text = bot.send_message.call_args[1]["text"]
        assert "[FOLLOW-UP]" in text

    @pytest.mark.asyncio
    async def test_send_notification_no_bot(self):
        """No bot means no notification — should not raise."""
        channel = EmailInputChannel(bot=None)
        em = {
            "id": "no-bot@test.com",
            "from": "test@test.com",
            "from_addr": "test@test.com",
            "subject": "Test",
            "date": "Feb 10",
            "to": "",
            "body": "",
        }
        await channel._send_notification(em)  # Should not raise
