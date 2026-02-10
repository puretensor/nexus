"""Tests for drafts/classifier.py — rule-based email classification."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from drafts.classifier import classify_email


# ---------------------------------------------------------------------------
# Ignore classification
# ---------------------------------------------------------------------------


class TestIgnoreSenders:

    def test_noreply(self):
        assert classify_email("noreply@example.com", "Your order") == "ignore"

    def test_no_reply_with_dash(self):
        assert classify_email("no-reply@example.com", "Update") == "ignore"

    def test_donotreply(self):
        assert classify_email("donotreply@company.com", "Info") == "ignore"

    def test_github_notifications(self):
        assert classify_email("notifications@github.com", "[repo] PR #42") == "ignore"

    def test_newsletter(self):
        assert classify_email("newsletter@tech.co", "Weekly digest") == "ignore"

    def test_marketing(self):
        assert classify_email("marketing@store.com", "Big sale!") == "ignore"

    def test_mailer_daemon(self):
        assert classify_email("mailer-daemon@mail.com", "Undelivered") == "ignore"

    def test_case_insensitive_sender(self):
        assert classify_email("NoReply@Example.COM", "Test") == "ignore"


class TestIgnoreSubjects:

    def test_unsubscribe(self):
        assert classify_email("user@example.com", "Click to unsubscribe") == "ignore"

    def test_out_of_office(self):
        assert classify_email("user@example.com", "Out of Office: Re: Meeting") == "ignore"

    def test_automatic_reply(self):
        assert classify_email("user@example.com", "Automatic reply: Vacation") == "ignore"

    def test_delivery_status(self):
        assert classify_email("user@example.com", "Delivery status notification (failure)") == "ignore"


# ---------------------------------------------------------------------------
# Notify classification
# ---------------------------------------------------------------------------


class TestNotifySenders:

    def test_stripe(self):
        assert classify_email("receipts@stripe.com", "Payment receipt") == "notify"

    def test_paypal(self):
        assert classify_email("service@paypal.com", "You sent a payment") == "notify"

    def test_security_sender(self):
        assert classify_email("security@bank.com", "Activity alert") == "notify"

    def test_billing(self):
        assert classify_email("billing@provider.com", "Your invoice") == "notify"


class TestNotifySubjects:

    def test_receipt(self):
        assert classify_email("shop@example.com", "Your receipt for order #123") == "notify"

    def test_invoice(self):
        assert classify_email("accounts@example.com", "Invoice #456 attached") == "notify"

    def test_payment_confirmation(self):
        assert classify_email("shop@store.com", "Payment confirmed for your order") == "notify"

    def test_security_alert(self):
        assert classify_email("admin@corp.com", "Security alert: new login") == "notify"

    def test_password_reset(self):
        assert classify_email("auth@service.com", "Password reset requested") == "notify"

    def test_verification_code(self):
        assert classify_email("auth@service.com", "Your verification code is 123456") == "notify"


# ---------------------------------------------------------------------------
# Auto-reply classification
# ---------------------------------------------------------------------------


class TestAutoReply:

    def test_vip_sender_alan(self):
        assert classify_email("alan.apter@bretalon.com", "Report feedback") == "auto_reply"

    def test_vip_sender_ops(self):
        assert classify_email("ops@puretensor.ai", "Server question") == "auto_reply"

    def test_vip_sender_personal(self):
        assert classify_email("heimir.helgason@gmail.com", "Quick thought") == "auto_reply"

    def test_vip_case_insensitive(self):
        assert classify_email("Alan.Apter@Bretalon.com", "Report") == "auto_reply"

    def test_vip_overrides_ignore(self):
        """VIP sender check runs before ignore rules."""
        # ops@puretensor.ai is VIP even though it might match some pattern
        assert classify_email("ops@puretensor.ai", "Unsubscribe link") == "auto_reply"

    def test_email_to_hal(self):
        """Emails addressed to hal@ get auto_reply."""
        assert classify_email(
            "stranger@company.com", "Hello", to_addr="hal@puretensor.ai"
        ) == "auto_reply"


# ---------------------------------------------------------------------------
# Default classification
# ---------------------------------------------------------------------------


class TestDefault:

    def test_unknown_sender_defaults_notify(self):
        """Unknown senders default to notify (safe — no draft created)."""
        assert classify_email("random.person@company.com", "Meeting request") == "notify"

    def test_empty_subject(self):
        assert classify_email("person@company.com", "") == "notify"

    def test_empty_from(self):
        assert classify_email("", "Hello") == "notify"
