"""Tests for calendar integration in MorningBriefObserver."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch.dict("os.environ", {
    "TELEGRAM_BOT_TOKEN": "fake:token",
    "AUTHORIZED_USER_ID": "12345",
}):
    from observers.morning_brief import MorningBriefObserver


class TestFetchCalendar:

    def test_no_gcalendar_script(self, tmp_path):
        """If gcalendar.py doesn't exist, return 'not configured'."""
        obs = MorningBriefObserver()
        obs.GCALENDAR_SCRIPT = tmp_path / "nonexistent.py"
        result = obs.fetch_calendar()
        assert "not configured" in result.lower()

    @patch("observers.morning_brief.subprocess.run")
    def test_parses_events(self, mock_run, tmp_path):
        """Should extract event lines starting with dates."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "Today (Tuesday 2026-02-10):\n"
                "\n"
                "Time                  Summary                  ID\n"
                "---------------------------------------------------\n"
                "2026-02-10  11:00-12:00  Pay Vodafone Bill   _abc123\n"
                "2026-02-10  14:00-15:00  Sprint Review       _def456\n"
                "\n"
                "  Showing 2 events"
            ),
            stderr="",
        )

        obs = MorningBriefObserver()
        obs.GCALENDAR_SCRIPT = tmp_path / "gcalendar.py"
        (tmp_path / "gcalendar.py").write_text("")  # Just needs to exist
        obs.CALENDAR_ACCOUNTS = ["personal"]  # Single account for deterministic count

        result = obs.fetch_calendar()
        assert "2 event(s) today" in result
        assert "Pay Vodafone Bill" in result
        assert "Sprint Review" in result

    @patch("observers.morning_brief.subprocess.run")
    def test_no_events(self, mock_run, tmp_path):
        """Empty calendar should return 'No calendar events today'."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Today (Tuesday 2026-02-10):\n  No events found.",
            stderr="",
        )

        obs = MorningBriefObserver()
        obs.GCALENDAR_SCRIPT = tmp_path / "gcalendar.py"
        (tmp_path / "gcalendar.py").write_text("")

        result = obs.fetch_calendar()
        assert "No calendar events today" in result

    @patch("observers.morning_brief.subprocess.run")
    def test_multiple_accounts(self, mock_run, tmp_path):
        """Events from multiple accounts should be tagged."""
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stdout="Today:\n2026-02-10  09:00-10:00  Personal meeting  _x",
                stderr="",
            ),
            MagicMock(
                returncode=0,
                stdout="Today:\n2026-02-10  14:00-15:00  Work meeting  _y",
                stderr="",
            ),
        ]

        obs = MorningBriefObserver()
        obs.GCALENDAR_SCRIPT = tmp_path / "gcalendar.py"
        (tmp_path / "gcalendar.py").write_text("")
        obs.CALENDAR_ACCOUNTS = ["personal", "ops"]

        result = obs.fetch_calendar()
        assert "[personal]" in result
        assert "[ops]" in result
        assert "2 event(s) today" in result


class TestBuildPromptIncludesCalendar:

    def test_prompt_has_calendar_section(self):
        """The Claude prompt should include a CALENDAR section."""
        obs = MorningBriefObserver()
        sections = {
            "emails": "No unread emails.",
            "infrastructure": "All nodes up.",
            "weather": "Sunny, 15C.",
            "calendar": "1 event(s) today:\n[personal] 2026-02-10  11:00-12:00  Meeting",
        }
        prompt = obs._build_prompt(sections)
        assert "== CALENDAR ==" in prompt
        assert "Meeting" in prompt
        assert "calendar events" in prompt.lower()


class TestGatherDataIncludesCalendar:

    @patch.object(MorningBriefObserver, "fetch_calendar", return_value="No calendar events today.")
    @patch.object(MorningBriefObserver, "fetch_weather", return_value="Sunny.")
    @patch.object(MorningBriefObserver, "fetch_node_health", return_value="All up.")
    @patch.object(MorningBriefObserver, "fetch_emails", return_value="No emails.")
    def test_gather_data_has_calendar_key(self, *mocks):
        obs = MorningBriefObserver()
        sections = obs._gather_data()
        assert "calendar" in sections
        assert sections["calendar"] == "No calendar events today."
