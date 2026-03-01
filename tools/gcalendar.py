#!/usr/bin/env python3
"""
PureTensor Google Calendar CLI
Supports multiple accounts with separate token files.
Mirrors gmail.py/gdrive.py patterns with Calendar-specific functionality.
"""

import os
import sys
import json
import argparse
import re
from pathlib import Path
from datetime import datetime, timedelta, date

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    import zoneinfo
    ZoneInfo = zoneinfo.ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Calendar scope - full read/write
SCOPES = ['https://www.googleapis.com/auth/calendar']

CONFIG_DIR = Path.home() / '.config' / 'puretensor'
TOKENS_DIR = CONFIG_DIR / 'gdrive_tokens'
CLIENT_SECRET = CONFIG_DIR / 'client_secret.json'

DEFAULT_TZ = 'Europe/London'

ACCOUNTS = {
    'personal': {
        'name': os.environ.get('GMAIL_PERSONAL', 'personal@example.com'),
        'token_file': 'calendar_token_personal.json',
        'description': 'Personal Calendar'
    },
    'ops': {
        'name': os.environ.get('GMAIL_OPS', 'ops@example.com'),
        'token_file': 'calendar_token_ops.json',
        'description': 'Ops Workspace Calendar'
    }
}


def get_credentials(account_key: str, force: bool = False) -> Credentials:
    """Get or refresh credentials for specified account."""
    if account_key not in ACCOUNTS:
        print(f"Unknown account: {account_key}")
        print(f"Available: {', '.join(ACCOUNTS.keys())}")
        sys.exit(1)

    account = ACCOUNTS[account_key]
    token_path = TOKENS_DIR / account['token_file']
    creds = None

    # Force re-auth: delete existing token
    if force and token_path.exists():
        token_path.unlink()
        print(f"Removed existing token for {account['name']}")

    # Load existing token
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # Refresh or get new token
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print(f"Refreshing token for {account['name']}...")
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Token refresh failed: {e}")
                print("Re-authenticating...")
                token_path.unlink(missing_ok=True)
                creds = None

        if not creds:
            if not CLIENT_SECRET.exists():
                print(f"ERROR: Client secret not found at {CLIENT_SECRET}")
                print("Download from GCP Console and save as client_secret.json")
                sys.exit(1)

            print(f"\n=== OAuth for {account['name']} ({account['description']}) ===")

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES)
            try:
                creds = flow.run_local_server(
                    port=0,
                    login_hint=account['name'],
                    prompt='select_account'
                )
            except Exception:
                print("Browser not available — using manual auth flow.")
                print("Open the URL below in a browser, authorize, then paste the redirect URL.\n")
                creds = flow.run_local_server(
                    port=8086,
                    open_browser=False,
                    login_hint=account['name'],
                    prompt='select_account'
                )

        # Save token
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w') as f:
            f.write(creds.to_json())
        print(f"Token saved to {token_path}")

    return creds


def get_service(account_key: str):
    """Build Calendar API service."""
    creds = get_credentials(account_key)
    return build('calendar', 'v3', credentials=creds)


# --- Date/Time Helpers ---

def get_tz(tz_name: str = None):
    """Get timezone object."""
    return ZoneInfo(tz_name or DEFAULT_TZ)


def now_aware(tz_name: str = None) -> datetime:
    """Get timezone-aware current datetime."""
    return datetime.now(get_tz(tz_name))


def to_rfc3339(dt: datetime) -> str:
    """Convert datetime to RFC 3339 string."""
    return dt.isoformat()


def today_range(tz_name: str = None) -> tuple:
    """Return (start, end) RFC 3339 strings for today."""
    tz = get_tz(tz_name)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    return to_rfc3339(today), to_rfc3339(tomorrow)


def week_range(tz_name: str = None) -> tuple:
    """Return (start, end) RFC 3339 strings for this week (Mon-Sun)."""
    tz = get_tz(tz_name)
    now = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = now - timedelta(days=now.weekday())
    next_monday = monday + timedelta(days=7)
    return to_rfc3339(monday), to_rfc3339(next_monday)


def parse_date_input(date_str: str) -> date:
    """Parse flexible date input: YYYY-MM-DD, today, tomorrow, +Nd."""
    if date_str == 'today':
        return date.today()
    elif date_str == 'tomorrow':
        return date.today() + timedelta(days=1)
    elif re.match(r'^\+(\d+)d$', date_str):
        days = int(re.match(r'^\+(\d+)d$', date_str).group(1))
        return date.today() + timedelta(days=days)
    else:
        return date.fromisoformat(date_str)


def parse_datetime_input(dt_str: str, tz_name: str = None) -> datetime:
    """Parse datetime string like '2026-02-12 14:00' into timezone-aware datetime."""
    tz = get_tz(tz_name)
    # Try common formats
    for fmt in ['%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S']:
        try:
            naive = datetime.strptime(dt_str, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {dt_str} (use YYYY-MM-DD HH:MM)")


# --- Display Helpers ---

def format_event_time(event: dict) -> str:
    """Format event start/end for display."""
    start = event.get('start', {})
    end = event.get('end', {})

    if 'date' in start:
        # All-day event
        start_date = start['date']
        end_date = end.get('date', '')
        # End date is exclusive, so subtract 1 day for display
        if end_date:
            end_dt = date.fromisoformat(end_date) - timedelta(days=1)
            if str(end_dt) == start_date:
                return f"{start_date} (all day)"
            return f"{start_date} to {end_dt} (all day)"
        return f"{start_date} (all day)"
    else:
        # Timed event
        start_dt = start.get('dateTime', '')
        end_dt = end.get('dateTime', '')
        try:
            s = datetime.fromisoformat(start_dt)
            e = datetime.fromisoformat(end_dt)
            if s.date() == e.date():
                return f"{s.strftime('%Y-%m-%d')}  {s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
            return f"{s.strftime('%Y-%m-%d %H:%M')} to {e.strftime('%Y-%m-%d %H:%M')}"
        except (ValueError, TypeError):
            return start_dt


def print_events(events: list, title: str = ''):
    """Print a formatted list of events."""
    if title:
        print(f"\n{title}")

    if not events:
        print('  No events found.')
        return

    print(f"\n{'Time':<45} {'Summary':<50} {'ID'}")
    print('-' * 120)

    for event in events:
        time_str = format_event_time(event)
        summary = event.get('summary', '(no title)')[:48]
        event_id = event.get('id', '')[:24]
        print(f"{time_str:<45} {summary:<50} {event_id}")

    print(f"\n  Showing {len(events)} events")


# --- Commands ---

def cmd_calendars(account_key: str):
    """List all calendars on account."""
    service = get_service(account_key)
    calendar_list = service.calendarList().list().execute()
    items = calendar_list.get('items', [])

    if not items:
        print('No calendars found.')
        return []

    print(f"\n{'Calendar':<50} {'Access':<15} {'ID'}")
    print('-' * 110)

    for cal in items:
        name = cal.get('summary', '(unnamed)')[:48]
        access = cal.get('accessRole', '')
        cal_id = cal.get('id', '')[:45]
        primary = ' *' if cal.get('primary') else ''
        print(f"{name}{primary:<50} {access:<15} {cal_id}")

    print(f"\n  (* = primary)  {len(items)} calendars")
    return items


def cmd_today(account_key: str, tz_name: str = None, limit: int = 50,
              calendar_id: str = 'primary', query: str = None):
    """Show today's events."""
    service = get_service(account_key)
    time_min, time_max = today_range(tz_name)

    params = {
        'calendarId': calendar_id,
        'timeMin': time_min,
        'timeMax': time_max,
        'maxResults': limit,
        'singleEvents': True,
        'orderBy': 'startTime',
    }
    if query:
        params['q'] = query

    try:
        results = service.events().list(**params).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return []

    events = results.get('items', [])
    today_str = datetime.now(get_tz(tz_name)).strftime('%A %Y-%m-%d')
    print_events(events, title=f"Today ({today_str}):")
    return events


def cmd_week(account_key: str, tz_name: str = None, limit: int = 50,
             calendar_id: str = 'primary', query: str = None):
    """Show this week's events (Mon-Sun)."""
    service = get_service(account_key)
    time_min, time_max = week_range(tz_name)

    params = {
        'calendarId': calendar_id,
        'timeMin': time_min,
        'timeMax': time_max,
        'maxResults': limit,
        'singleEvents': True,
        'orderBy': 'startTime',
    }
    if query:
        params['q'] = query

    try:
        results = service.events().list(**params).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return []

    events = results.get('items', [])
    tz = get_tz(tz_name)
    now = datetime.now(tz)
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    print_events(events, title=f"This week ({monday.strftime('%Y-%m-%d')} to {sunday.strftime('%Y-%m-%d')}):")
    return events


def cmd_upcoming(account_key: str, tz_name: str = None, limit: int = 10,
                 calendar_id: str = 'primary', query: str = None):
    """Show next N upcoming events."""
    service = get_service(account_key)
    now = to_rfc3339(now_aware(tz_name))

    params = {
        'calendarId': calendar_id,
        'timeMin': now,
        'maxResults': limit,
        'singleEvents': True,
        'orderBy': 'startTime',
    }
    if query:
        params['q'] = query

    try:
        results = service.events().list(**params).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return []

    events = results.get('items', [])
    print_events(events, title=f"Upcoming (next {limit}):")
    return events


def cmd_search(account_key: str, query: str, tz_name: str = None,
               limit: int = 20, calendar_id: str = 'primary'):
    """Search events by text."""
    service = get_service(account_key)

    params = {
        'calendarId': calendar_id,
        'q': query,
        'maxResults': limit,
        'singleEvents': True,
        'orderBy': 'startTime',
        'timeMin': to_rfc3339(now_aware(tz_name) - timedelta(days=365)),
        'timeMax': to_rfc3339(now_aware(tz_name) + timedelta(days=365)),
    }

    try:
        results = service.events().list(**params).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return []

    events = results.get('items', [])
    print_events(events, title=f"Search results for '{query}':")
    return events


def cmd_get(account_key: str, event_id: str, calendar_id: str = 'primary'):
    """Get full event details."""
    service = get_service(account_key)

    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return None

    print(f"\n{'='*80}")
    print(f"  Summary:     {event.get('summary', '(no title)')}")
    print(f"  Time:        {format_event_time(event)}")
    print(f"  Status:      {event.get('status', '')}")

    if event.get('location'):
        print(f"  Location:    {event['location']}")
    if event.get('description'):
        print(f"  Description: {event['description'][:200]}")

    creator = event.get('creator', {})
    if creator:
        print(f"  Creator:     {creator.get('email', '')} {creator.get('displayName', '')}")

    organizer = event.get('organizer', {})
    if organizer and organizer.get('email') != creator.get('email'):
        print(f"  Organizer:   {organizer.get('email', '')} {organizer.get('displayName', '')}")

    attendees = event.get('attendees', [])
    if attendees:
        print(f"  Attendees:   ({len(attendees)})")
        for a in attendees[:10]:
            status = a.get('responseStatus', '')
            print(f"               {a.get('email', ''):<40} [{status}]")
        if len(attendees) > 10:
            print(f"               ... and {len(attendees) - 10} more")

    if event.get('recurrence'):
        print(f"  Recurrence:  {', '.join(event['recurrence'])}")
    if event.get('hangoutLink'):
        print(f"  Meet:        {event['hangoutLink']}")
    if event.get('htmlLink'):
        print(f"  Link:        {event['htmlLink']}")

    print(f"  ID:          {event.get('id', '')}")
    print(f"  Calendar:    {event.get('organizer', {}).get('email', '')}")
    print(f"{'='*80}")

    return event


def cmd_create(account_key: str, title: str, date_str: str = None,
               start_str: str = None, end_str: str = None,
               description: str = None, location: str = None,
               tz_name: str = None, calendar_id: str = 'primary'):
    """Create a calendar event."""
    service = get_service(account_key)
    tz = tz_name or DEFAULT_TZ

    event_body = {
        'summary': title,
    }

    if description:
        event_body['description'] = description
    if location:
        event_body['location'] = location

    if date_str:
        # All-day event
        d = parse_date_input(date_str)
        next_day = d + timedelta(days=1)
        event_body['start'] = {'date': d.isoformat()}
        event_body['end'] = {'date': next_day.isoformat()}  # exclusive end
    elif start_str:
        # Timed event
        start_dt = parse_datetime_input(start_str, tz_name)
        if end_str:
            end_dt = parse_datetime_input(end_str, tz_name)
        else:
            end_dt = start_dt + timedelta(hours=1)

        event_body['start'] = {'dateTime': to_rfc3339(start_dt), 'timeZone': tz}
        event_body['end'] = {'dateTime': to_rfc3339(end_dt), 'timeZone': tz}
    else:
        print("ERROR: Provide --date (all-day) or --start (timed event)")
        sys.exit(1)

    try:
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return None

    print(f"Created event: {event.get('summary')}")
    print(f"  Time: {format_event_time(event)}")
    print(f"  ID:   {event.get('id')}")
    print(f"  Link: {event.get('htmlLink', '')}")
    return event


def cmd_delete(account_key: str, event_id: str, skip_confirm: bool = False,
               calendar_id: str = 'primary'):
    """Delete an event (with confirmation)."""
    service = get_service(account_key)

    # Fetch event details first
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return

    summary = event.get('summary', '(no title)')
    time_str = format_event_time(event)

    if not skip_confirm:
        print(f"\nDelete event?")
        print(f"  Summary: {summary}")
        print(f"  Time:    {time_str}")
        print(f"  ID:      {event_id}")
        confirm = input("\nType 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            return

    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        print(f"API error: {e}")
        return

    print(f"Deleted: {summary} ({time_str})")


# --- Main CLI ---

def main():
    parser = argparse.ArgumentParser(description='PureTensor Google Calendar CLI')
    parser.add_argument('account', choices=['personal', 'ops', 'all'],
                        help='Account to use (all = both accounts)')
    parser.add_argument('command', choices=[
        'auth', 'calendars', 'today', 'week', 'upcoming', 'search',
        'create', 'get', 'delete'
    ], help='Command to run')
    parser.add_argument('--query', '-q', help='Search text')
    parser.add_argument('--id', help='Event ID (for get/delete)')
    parser.add_argument('--limit', '-n', type=int, default=None,
                        help='Max results')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Force re-authentication')
    parser.add_argument('--tz', default=None,
                        help=f'Timezone (default: {DEFAULT_TZ})')
    parser.add_argument('--calendar', default='primary',
                        help='Calendar ID (default: primary)')
    parser.add_argument('--title', help='Event title (for create)')
    parser.add_argument('--date', help='All-day event date: YYYY-MM-DD, today, tomorrow, +Nd')
    parser.add_argument('--start', help='Event start: "YYYY-MM-DD HH:MM"')
    parser.add_argument('--end', help='Event end: "YYYY-MM-DD HH:MM"')
    parser.add_argument('--description', help='Event description')
    parser.add_argument('--location', help='Event location')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='Skip confirmation (for delete)')

    args = parser.parse_args()

    accounts = list(ACCOUNTS.keys()) if args.account == 'all' else [args.account]

    for acc in accounts:
        if len(accounts) > 1:
            print(f"\n{'='*60}")
            print(f"Account: {ACCOUNTS[acc]['name']}")
            print('='*60)

        if args.command == 'auth':
            get_credentials(acc, force=args.force)
            print(f"Authenticated: {ACCOUNTS[acc]['name']}")

        elif args.command == 'calendars':
            cmd_calendars(acc)

        elif args.command == 'today':
            limit = args.limit or 50
            cmd_today(acc, tz_name=args.tz, limit=limit,
                      calendar_id=args.calendar, query=args.query)

        elif args.command == 'week':
            limit = args.limit or 50
            cmd_week(acc, tz_name=args.tz, limit=limit,
                     calendar_id=args.calendar, query=args.query)

        elif args.command == 'upcoming':
            limit = args.limit or 10
            cmd_upcoming(acc, tz_name=args.tz, limit=limit,
                         calendar_id=args.calendar, query=args.query)

        elif args.command == 'search':
            if not args.query:
                print("--query/-q required for search")
                sys.exit(1)
            limit = args.limit or 20
            cmd_search(acc, args.query, tz_name=args.tz, limit=limit,
                       calendar_id=args.calendar)

        elif args.command == 'create':
            if not args.title:
                print("--title required for create")
                sys.exit(1)
            if not args.date and not args.start:
                print("--date or --start required for create")
                sys.exit(1)
            cmd_create(acc, args.title, date_str=args.date,
                       start_str=args.start, end_str=args.end,
                       description=args.description, location=args.location,
                       tz_name=args.tz, calendar_id=args.calendar)

        elif args.command == 'get':
            if not args.id:
                print("--id required for get")
                sys.exit(1)
            cmd_get(acc, args.id, calendar_id=args.calendar)

        elif args.command == 'delete':
            if not args.id:
                print("--id required for delete")
                sys.exit(1)
            cmd_delete(acc, args.id, skip_confirm=args.yes,
                       calendar_id=args.calendar)


if __name__ == '__main__':
    main()
