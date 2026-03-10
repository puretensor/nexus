#!/usr/bin/env python3
"""
PureTensor Gmail CLI
Supports multiple accounts with separate token files.
Mirrors gdrive.py patterns with Gmail-specific functionality.
"""

import os
import sys
import json
import argparse
import base64
import email
import webbrowser
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Gmail scopes - full access + filter management
SCOPES = [
    'https://mail.google.com/',                             # Full access (needed for permanent delete)
    'https://www.googleapis.com/auth/gmail.settings.basic'  # Create/manage filters
]

CONFIG_DIR = Path.home() / '.config' / 'puretensor'
TOKENS_DIR = CONFIG_DIR / 'gdrive_tokens'
CLIENT_SECRET = CONFIG_DIR / 'client_secret.json'

ACCOUNTS = {
    'personal': {
        'name': os.environ.get('GMAIL_PERSONAL', 'personal@example.com'),
        'token_file': 'gmail_token_personal.json',
        'description': 'Personal Gmail'
    },
    'ops': {
        'name': os.environ.get('GMAIL_OPS', 'ops@example.com'),
        'token_file': 'gmail_token_ops.json',
        'description': 'Ops Workspace'
    },
    'galactic': {
        'name': os.environ.get('GMAIL_GALACTIC', 'galactic@example.com'),
        'token_file': 'gmail_token_galactic.json',
        'description': 'Trading Corp Gmail'
    }
}

# Shorthand aliases → resolved to (account, from_alias, from_name)
# Use: python3 gmail.py hal send --to ... --subject ... --body ...
# account='mail_provider' routes through SMTP instead of Gmail API
IDENTITY_ALIASES = {
    'hal': ('mail_provider', os.environ.get('HAL_EMAIL', 'hal@example.com'), 'PureClaw'),
    'hal-org': ('ops', os.environ.get('HAL_ORG_EMAIL', 'hal-org@example.com'), 'PureClaw'),
}

# Send-as identities (account -> alias -> display name)
# These must be configured as "Send As" aliases in Google Workspace
SEND_IDENTITIES = {
    'ops': {
        'ops@puretensor.ai': 'PureTensorAI',
        'hal@example.com': 'PureClaw',            # Send As on ops account
    },
}

# mail provider SMTP — non-Gmail identities routed via direct SMTP
EXTERNAL_SMTP = {
    'hal@example.com': {
        'host': 'mail.example.com',
        'port': 587,
        'username': 'hal@example.com',
        'password': os.environ.get('HAL_SMTP_PASSWORD', ''),
    },
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
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                print(f"ERROR: Client secret not found at {CLIENT_SECRET}")
                print("Download from GCP Console and save as client_secret.json")
                sys.exit(1)

            print(f"\n=== OAuth for {account['name']} ({account['description']}) ===")
            print("A browser will open. Sign in with the correct account!\n")

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES)
            try:
                creds = flow.run_local_server(
                    port=0,
                    login_hint=account['name'],
                    prompt='select_account'
                )
            except webbrowser.Error:
                # Headless: print URL for manual auth
                flow.run_local_server(
                    port=8085,
                    open_browser=False,
                    login_hint=account['name'],
                    prompt='select_account'
                )
                creds = flow.credentials

        # Save token
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w') as f:
            f.write(creds.to_json())
        print(f"Token saved to {token_path}")

    return creds


def get_service(account_key: str):
    """Build Gmail API service."""
    creds = get_credentials(account_key)
    return build('gmail', 'v1', credentials=creds)


def parse_message_headers(headers: list) -> dict:
    """Extract common headers from message header list."""
    result = {}
    wanted = {'From', 'To', 'Subject', 'Date', 'Cc', 'Reply-To'}
    for h in headers:
        if h['name'] in wanted:
            result[h['name']] = h['value']
    return result


def get_message_body(payload: dict) -> str:
    """Extract plain text body from message payload."""
    # Simple message
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')

    # Multipart message - find text/plain part
    parts = payload.get('parts', [])
    for part in parts:
        if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
            return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
        # Nested multipart
        if part.get('mimeType', '').startswith('multipart/'):
            result = get_message_body(part)
            if result:
                return result

    # Fallback to text/html
    for part in parts:
        if part.get('mimeType') == 'text/html' and part.get('body', {}).get('data'):
            return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')

    return '[No text body found]'


def format_date(date_str: str) -> str:
    """Format email date to shorter form."""
    try:
        # Parse various email date formats
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.strftime('%Y-%m-%d %H:%M')
    except Exception:
        return date_str[:16] if date_str else ''


def list_messages(account_key: str, query: str = '', limit: int = 20, label: str = 'INBOX'):
    """List messages matching query."""
    service = get_service(account_key)

    params = {
        'userId': 'me',
        'maxResults': limit,
    }
    if query:
        params['q'] = query
    if label:
        params['labelIds'] = [label]

    results = service.users().messages().list(**params).execute()
    messages = results.get('messages', [])

    if not messages:
        print('No messages found.')
        return []

    # Fetch headers for each message
    detailed = []
    for msg in messages:
        detail = service.users().messages().get(
            userId='me', id=msg['id'], format='metadata',
            metadataHeaders=['From', 'Subject', 'Date']
        ).execute()
        detailed.append(detail)

    print(f"\n{'ID':<18} {'Date':<18} {'From':<30} {'Subject'}")
    print('-' * 110)

    for msg in detailed:
        headers = parse_message_headers(msg.get('payload', {}).get('headers', []))
        msg_id = msg['id']
        date = format_date(headers.get('Date', ''))
        sender = headers.get('From', '')[:28]
        subject = headers.get('Subject', '(no subject)')[:50]
        unread = 'UNREAD' in msg.get('labelIds', [])
        marker = '*' if unread else ' '
        print(f"{marker}{msg_id:<17} {date:<18} {sender:<30} {subject}")

    print(f"\n  (* = unread)  Showing {len(detailed)} messages")
    return detailed


def read_message(account_key: str, msg_id: str):
    """Read a specific message by ID."""
    service = get_service(account_key)

    msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    headers = parse_message_headers(msg.get('payload', {}).get('headers', []))
    body = get_message_body(msg.get('payload', {}))

    print(f"\n{'='*80}")
    print(f"  From:    {headers.get('From', '')}")
    print(f"  To:      {headers.get('To', '')}")
    if headers.get('Cc'):
        print(f"  Cc:      {headers.get('Cc', '')}")
    print(f"  Date:    {headers.get('Date', '')}")
    print(f"  Subject: {headers.get('Subject', '')}")
    print(f"  ID:      {msg_id}")
    labels = msg.get('labelIds', [])
    print(f"  Labels:  {', '.join(labels)}")
    print(f"{'='*80}\n")
    print(body)

    return msg


def trash_message(account_key: str, msg_id: str):
    """Move a message to trash."""
    service = get_service(account_key)
    service.users().messages().trash(userId='me', id=msg_id).execute()
    print(f"Trashed message: {msg_id}")


def batch_trash(account_key: str, query: str, limit: int = 500):
    """Batch trash messages matching a query (Gmail API batchModify supports up to 1000 IDs)."""
    service = get_service(account_key)

    # Search for messages
    results = service.users().messages().list(userId='me', q=query, maxResults=limit).execute()
    messages = results.get('messages', [])

    if not messages:
        print('No messages found matching query.')
        return 0

    msg_ids = [msg['id'] for msg in messages]
    count = len(msg_ids)

    print(f"Found {count} messages matching: {query}")
    print(f"Trashing {count} messages...")

    # Use batchModify to add TRASH label (more efficient than individual calls)
    service.users().messages().batchModify(
        userId='me',
        body={'ids': msg_ids, 'addLabelIds': ['TRASH']}
    ).execute()

    print(f"✓ Trashed {count} messages")
    return count


def spam_message(account_key: str, msg_id: str):
    """Mark a message as spam."""
    service = get_service(account_key)
    service.users().messages().modify(
        userId='me', id=msg_id,
        body={'addLabelIds': ['SPAM'], 'removeLabelIds': ['INBOX']}
    ).execute()
    print(f"Marked as spam: {msg_id}")


def delete_message(account_key: str, msg_id: str):
    """Permanently delete a message (no undo)."""
    service = get_service(account_key)
    service.users().messages().delete(userId='me', id=msg_id).execute()
    print(f"Permanently deleted: {msg_id}")


def list_labels(account_key: str):
    """List all labels."""
    service = get_service(account_key)
    results = service.users().labels().list(userId='me').execute()
    labels = results.get('labels', [])

    print(f"\n{'Name':<40} {'Type':<15} {'ID'}")
    print('-' * 80)
    for label in sorted(labels, key=lambda x: x['name']):
        print(f"{label['name']:<40} {label.get('type', ''):<15} {label['id']}")

    return labels


def create_filter(account_key: str, from_addr: str, action: str = 'trash'):
    """Create an email filter."""
    service = get_service(account_key)

    filter_body = {
        'criteria': {
            'from': from_addr
        },
        'action': {}
    }

    if action == 'trash':
        filter_body['action']['removeLabelIds'] = ['INBOX']
        filter_body['action']['addLabelIds'] = ['TRASH']
    elif action == 'delete':
        filter_body['action']['removeLabelIds'] = ['INBOX']
        filter_body['action']['addLabelIds'] = ['TRASH']
    elif action == 'archive':
        filter_body['action']['removeLabelIds'] = ['INBOX']
    elif action == 'read':
        filter_body['action']['removeLabelIds'] = ['UNREAD']
    elif action == 'spam':
        filter_body['action']['removeLabelIds'] = ['INBOX']
        filter_body['action']['addLabelIds'] = ['SPAM']

    result = service.users().settings().filters().create(
        userId='me', body=filter_body
    ).execute()

    print(f"Created filter: {result.get('id')}")
    print(f"  From: {from_addr}")
    print(f"  Action: {action}")
    return result


def list_filters(account_key: str):
    """List all filters."""
    service = get_service(account_key)
    results = service.users().settings().filters().list(userId='me').execute()
    filters = results.get('filter', [])

    if not filters:
        print('No filters found.')
        return []

    print(f"\n{'ID':<20} {'Criteria':<40} {'Action'}")
    print('-' * 90)

    for f in filters:
        criteria = f.get('criteria', {})
        action = f.get('action', {})

        # Build criteria string
        crit_parts = []
        if criteria.get('from'):
            crit_parts.append(f"from:{criteria['from']}")
        if criteria.get('to'):
            crit_parts.append(f"to:{criteria['to']}")
        if criteria.get('subject'):
            crit_parts.append(f"subject:{criteria['subject']}")
        if criteria.get('query'):
            crit_parts.append(f"query:{criteria['query']}")
        crit_str = ', '.join(crit_parts) if crit_parts else '(empty)'

        # Build action string
        act_parts = []
        if action.get('addLabelIds'):
            act_parts.append(f"+{','.join(action['addLabelIds'])}")
        if action.get('removeLabelIds'):
            act_parts.append(f"-{','.join(action['removeLabelIds'])}")
        if action.get('forward'):
            act_parts.append(f"fwd:{action['forward']}")
        act_str = ' '.join(act_parts) if act_parts else '(none)'

        print(f"{f['id']:<20} {crit_str:<40} {act_str}")

    return filters


def delete_filter(account_key: str, filter_id: str):
    """Delete a filter by ID."""
    service = get_service(account_key)
    service.users().settings().filters().delete(userId='me', id=filter_id).execute()
    print(f"Deleted filter: {filter_id}")


def send_message(account_key, to, subject, body, cc=None, bcc=None,
                 from_name=None, from_alias=None, html=False,
                 reply_to_id=None, attachments=None):
    """Send an email. Supports Send As aliases for cross-domain sending."""
    service = get_service(account_key)
    account = ACCOUNTS[account_key]

    # Determine from address and display name
    from_addr = from_alias or account['name']
    if from_name:
        display = from_name
    elif account_key in SEND_IDENTITIES and from_addr in SEND_IDENTITIES[account_key]:
        display = SEND_IDENTITIES[account_key][from_addr]
    else:
        display = None

    from_header = f'{display} <{from_addr}>' if display else from_addr

    # Build MIME message
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, 'html' if html else 'plain'))
        for filepath in attachments:
            part = MIMEBase('application', 'octet-stream')
            with open(filepath, 'rb') as f:
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition',
                            f'attachment; filename="{Path(filepath).name}"')
            msg.attach(part)
    else:
        msg = MIMEText(body, 'html' if html else 'plain')

    msg['To'] = to
    msg['From'] = from_header
    msg['Subject'] = subject
    if cc:
        msg['Cc'] = cc

    send_body = {'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode()}

    # Threading support for replies
    if reply_to_id:
        orig = service.users().messages().get(
            userId='me', id=reply_to_id, format='metadata',
            metadataHeaders=['Message-ID', 'Subject', 'From']
        ).execute()
        for h in orig.get('payload', {}).get('headers', []):
            if h['name'] == 'Message-ID':
                msg.add_header('In-Reply-To', h['value'])
                msg.add_header('References', h['value'])
        send_body['threadId'] = orig.get('threadId')
        # Re-encode after adding headers
        send_body['raw'] = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    result = service.users().messages().send(userId='me', body=send_body).execute()

    print(f"\nSent: {result['id']}")
    print(f"  From:    {from_header}")
    print(f"  To:      {to}")
    if cc:
        print(f"  Cc:      {cc}")
    if bcc:
        print(f"  Bcc:     {bcc}")
    print(f"  Subject: {subject}")
    print(f"  Thread:  {result.get('threadId', 'new')}")

    return result


def send_via_mail_provider(from_addr, to, subject, body, cc=None, bcc=None,
                       from_name=None, html=False, attachments=None,
                       in_reply_to=None):
    """Send email via mail provider SMTP (for non-Gmail identities like hal@example.com)."""
    import smtplib

    if from_addr not in EXTERNAL_SMTP:
        print(f"ERROR: No mail provider config for {from_addr}")
        sys.exit(1)

    cfg = EXTERNAL_SMTP[from_addr]
    display = from_name or from_addr
    from_header = f'{display} <{from_addr}>'

    # Build MIME message
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, 'html' if html else 'plain'))
        for filepath in attachments:
            part = MIMEBase('application', 'octet-stream')
            with open(filepath, 'rb') as f:
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition',
                            f'attachment; filename="{Path(filepath).name}"')
            msg.attach(part)
    else:
        msg = MIMEText(body, 'html' if html else 'plain')

    msg['To'] = to
    msg['From'] = from_header
    msg['Subject'] = subject
    if cc:
        msg['Cc'] = cc
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    # Send via SMTP STARTTLS
    try:
        with smtplib.SMTP(cfg['host'], cfg['port'], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg['username'], cfg['password'])
            recipients = [to]
            if cc:
                recipients.extend(a.strip() for a in cc.split(','))
            if bcc:
                recipients.extend(a.strip() for a in bcc.split(','))
            smtp.sendmail(from_addr, recipients, msg.as_string())
    except Exception as e:
        print(f"\nERROR: SMTP send failed: {e}")
        sys.exit(1)

    print(f"\nSent (mail provider SMTP):")
    print(f"  From:    {from_header}")
    print(f"  To:      {to}")
    if cc:
        print(f"  Cc:      {cc}")
    print(f"  Subject: {subject}")

    return {'id': 'mail_provider-smtp', 'status': 'sent'}


def list_send_aliases(account_key):
    """List configured Send As aliases for an account."""
    service = get_service(account_key)
    result = service.users().settings().sendAs().list(userId='me').execute()
    aliases = result.get('sendAs', [])

    print(f"\n{'Address':<35} {'Display Name':<25} {'Default':<10} {'Verified'}")
    print('-' * 90)
    for a in aliases:
        default = 'Yes' if a.get('isDefault') else ''
        verified = 'Yes' if a.get('verificationStatus') == 'accepted' else a.get('verificationStatus', '')
        print(f"{a['sendAsEmail']:<35} {a.get('displayName', ''):<25} {default:<10} {verified}")

    return aliases


def main():
    parser = argparse.ArgumentParser(description='PureTensor Gmail CLI')
    all_choices = list(ACCOUNTS.keys()) + list(IDENTITY_ALIASES.keys()) + ['all']
    parser.add_argument('account', choices=all_choices,
                        help='Account or identity alias (hal, hal-org, personal)')
    parser.add_argument('command', choices=[
        'auth', 'inbox', 'unread', 'search', 'read', 'delete', 'trash', 'batch-trash', 'spam',
        'labels', 'filter-create', 'filter-list', 'filter-delete',
        'send', 'reply', 'aliases'
    ], help='Command to run')
    parser.add_argument('--query', '-q', help='Search query (Gmail syntax)')
    parser.add_argument('--id', help='Message or filter ID (also reply-to msg ID)')
    parser.add_argument('--limit', '-n', type=int, default=20, help='Max results')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Force re-authentication')
    parser.add_argument('--from', dest='from_addr', help='From address for filter')
    parser.add_argument('--action', default='trash',
                        choices=['trash', 'delete', 'archive', 'read', 'spam'],
                        help='Filter action (default: trash)')
    # Send options
    parser.add_argument('--to', help='Recipient email address')
    parser.add_argument('--cc', help='CC recipients (comma-separated)')
    parser.add_argument('--bcc', help='BCC recipients (comma-separated)')
    parser.add_argument('--subject', '-s', help='Email subject')
    parser.add_argument('--body', '-b', help='Email body text (inline)')
    parser.add_argument('--body-file', help='Read email body from file')
    parser.add_argument('--html', action='store_true', help='Body is HTML')
    parser.add_argument('--from-name', help='Display name override (e.g. "John Smith")')
    parser.add_argument('--from-alias', help='Send As address (e.g. hal@example.com)')
    parser.add_argument('--attachment', action='append', help='File to attach (repeatable)')

    args = parser.parse_args()

    # Resolve identity aliases for send/reply commands
    identity_from_alias = None
    identity_from_name = None
    if args.account in IDENTITY_ALIASES:
        real_account, identity_from_alias, identity_from_name = IDENTITY_ALIASES[args.account]
        accounts = [real_account]
    elif args.account == 'all':
        accounts = list(ACCOUNTS.keys())
    else:
        accounts = [args.account]

    # mail provider identity — route send/reply through SMTP, reject other commands
    if accounts == ['mail_provider']:
        if args.command == 'send':
            if not args.to:
                print("--to required for send")
                sys.exit(1)
            if not args.subject:
                print("--subject required for send")
                sys.exit(1)
            body = args.body or ''
            if args.body_file:
                with open(args.body_file, 'r') as f:
                    body = f.read()
            if not body:
                print("--body or --body-file required for send")
                sys.exit(1)
            send_via_mail_provider(
                identity_from_alias, args.to, args.subject, body,
                cc=args.cc, bcc=args.bcc,
                from_name=args.from_name or identity_from_name,
                html=args.html, attachments=args.attachment,
            )
        elif args.command == 'reply':
            if not args.id:
                print("--id required for reply (Message-ID to reply to)")
                sys.exit(1)
            body = args.body or ''
            if args.body_file:
                with open(args.body_file, 'r') as f:
                    body = f.read()
            if not body:
                print("--body or --body-file required for reply")
                sys.exit(1)
            # For mail provider replies, use the provided ID as In-Reply-To
            # and default the subject to Re: original subject
            subject = args.subject or f"Re: {args.id}"
            send_via_mail_provider(
                identity_from_alias, args.to or '', subject, body,
                cc=args.cc, bcc=args.bcc,
                from_name=args.from_name or identity_from_name,
                html=args.html, attachments=args.attachment,
                in_reply_to=args.id,
            )
        else:
            print(f"Command '{args.command}' not supported for mail provider identity '{args.account}'")
            print("Use imap.py or IMAP tools for inbox/search operations")
            sys.exit(1)
        return

    for acc in accounts:
        if len(accounts) > 1:
            print(f"\n{'='*60}")
            print(f"Account: {ACCOUNTS[acc]['name']}")
            print('='*60)

        if args.command == 'auth':
            get_credentials(acc, force=args.force)
            print(f"Authenticated: {ACCOUNTS[acc]['name']}")

        elif args.command == 'inbox':
            list_messages(acc, query=args.query or '', limit=args.limit)

        elif args.command == 'unread':
            list_messages(acc, query='is:unread', limit=args.limit)

        elif args.command == 'search':
            if not args.query:
                print("--query required for search")
                sys.exit(1)
            list_messages(acc, query=args.query, limit=args.limit, label=None)

        elif args.command == 'read':
            if not args.id:
                print("--id required for read")
                sys.exit(1)
            read_message(acc, args.id)

        elif args.command == 'delete':
            if not args.id:
                print("--id required for delete")
                sys.exit(1)
            delete_message(acc, args.id)

        elif args.command == 'trash':
            if not args.id:
                print("--id required for trash")
                sys.exit(1)
            trash_message(acc, args.id)

        elif args.command == 'batch-trash':
            if not args.query:
                print("--query required for batch-trash")
                sys.exit(1)
            batch_trash(acc, args.query, limit=args.limit)

        elif args.command == 'spam':
            if not args.id:
                print("--id required for spam")
                sys.exit(1)
            spam_message(acc, args.id)

        elif args.command == 'labels':
            list_labels(acc)

        elif args.command == 'filter-create':
            if not args.from_addr:
                print("--from required for filter-create")
                sys.exit(1)
            create_filter(acc, args.from_addr, args.action)

        elif args.command == 'filter-list':
            list_filters(acc)

        elif args.command == 'filter-delete':
            if not args.id:
                print("--id required for filter-delete")
                sys.exit(1)
            delete_filter(acc, args.id)

        elif args.command == 'send':
            if not args.to:
                print("--to required for send")
                sys.exit(1)
            if not args.subject:
                print("--subject required for send")
                sys.exit(1)
            body = args.body or ''
            if args.body_file:
                with open(args.body_file, 'r') as f:
                    body = f.read()
            if not body:
                print("--body or --body-file required for send")
                sys.exit(1)
            send_message(
                acc, args.to, args.subject, body,
                cc=args.cc, bcc=args.bcc,
                from_name=args.from_name or identity_from_name,
                from_alias=args.from_alias or identity_from_alias,
                html=args.html, attachments=args.attachment
            )

        elif args.command == 'reply':
            if not args.id:
                print("--id required for reply (message ID to reply to)")
                sys.exit(1)
            body = args.body or ''
            if args.body_file:
                with open(args.body_file, 'r') as f:
                    body = f.read()
            if not body:
                print("--body or --body-file required for reply")
                sys.exit(1)
            # Get original message to auto-fill To and Subject
            service = get_service(acc)
            orig = service.users().messages().get(
                userId='me', id=args.id, format='metadata',
                metadataHeaders=['From', 'Subject', 'Reply-To']
            ).execute()
            orig_headers = parse_message_headers(
                orig.get('payload', {}).get('headers', []))
            reply_to = args.to or orig_headers.get('Reply-To') or orig_headers.get('From', '')
            subject = args.subject or f"Re: {orig_headers.get('Subject', '')}"
            send_message(
                acc, reply_to, subject, body,
                cc=args.cc, bcc=args.bcc,
                from_name=args.from_name or identity_from_name,
                from_alias=args.from_alias or identity_from_alias,
                html=args.html, reply_to_id=args.id,
                attachments=args.attachment
            )

        elif args.command == 'aliases':
            list_send_aliases(acc)


if __name__ == '__main__':
    main()
