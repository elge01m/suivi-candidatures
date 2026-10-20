"""In-memory MIME extraction. Never fetch attachments, links or remote images."""

import base64
import binascii
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses

from bs4 import BeautifulSoup
from django.views.decorators.debug import sensitive_variables

from .import_errors import ImportProblem


MAX_TEXT = 200_000
MAX_PART_BYTES = 1_000_000
MAX_PARTS = 500
MAX_DEPTH = 20


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


@sensitive_variables()
def parse_message(message, extra_reasons=()):
    reasons = set(extra_reasons)
    if not isinstance(message, dict) or not valid_id(message.get('id')) or not valid_id(message.get('threadId')):
        raise ImportProblem('gmail_response_invalid')
    try:
        date = datetime.fromtimestamp(int(message['internalDate']) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise ImportProblem('gmail_response_invalid') from None
    payload = message.get('payload') or {}
    if not isinstance(payload, dict):
        raise ImportProblem('gmail_response_invalid')

    def decode_bytes(data, charset):
        try:
            return data.decode(charset, errors='strict')
        except (UnicodeError, LookupError):
            reasons.add('encoding_replaced')
            return data.decode('utf-8', errors='replace')

    def decode_header_value(value):
        try:
            decoded = ''.join(decode_bytes(part, encoding or 'utf-8') if isinstance(part, bytes) else part
                              for part, encoding in decode_header(str(value)))
            if len(decoded) > MAX_TEXT:
                reasons.add('header_limit')
            return decoded[:MAX_TEXT]
        except (ValueError, TypeError):
            reasons.add('header_invalid')
            return ''

    def headers(part):
        result = {}
        raw_headers = part.get('headers', [])
        if not isinstance(raw_headers, list):
            reasons.add('header_invalid')
            return result
        for header in raw_headers:
            if isinstance(header, dict) and isinstance(header.get('name'), str):
                name = header['name'].lower()
                result.setdefault(name, []).append(decode_header_value(header.get('value', '')))
        return result

    top_headers = headers(payload)
    visited = 0

    def walk(part, depth=0):
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_PARTS:
            reasons.add('mime_limit')
            return ''
        if not isinstance(part, dict):
            reasons.add('mime_invalid')
            return ''
        part_headers = headers(part)
        disposition = ' '.join(part_headers.get('content-disposition', [])).lower()
        if part.get('filename') or disposition.startswith('attachment'):
            return ''
        mime = str(part.get('mimeType', '')).lower()
        parts = part.get('parts') or []
        if not isinstance(parts, list):
            reasons.add('mime_invalid')
            return ''
        if mime == 'multipart/alternative':
            # Select a single representation; do not duplicate plain and HTML bodies.
            for preferred in ('text/plain', 'text/html'):
                for child in parts:
                    if isinstance(child, dict) and child.get('mimeType', '').lower() == preferred:
                        text = walk(child, depth + 1)
                        if text.strip():
                            return text
            return '\n\n'.join(filter(None, (walk(child, depth + 1) for child in parts
                                             if isinstance(child, dict) and child.get('mimeType', '').startswith('multipart/'))))
        if mime.startswith('multipart/'):
            return '\n\n'.join(filter(None, (walk(child, depth + 1) for child in parts)))[:MAX_TEXT + 1]
        if mime not in ('text/plain', 'text/html'):
            if mime.startswith('text/'):
                reasons.add('unsupported_text')
            return ''
        body = part.get('body') or {}
        if not isinstance(body, dict):
            reasons.add('body_missing')
            return ''
        data = body.get('data')
        if not data:
            if body.get('attachmentId') or body.get('size', 0):
                reasons.add('external_text_unavailable')
            return ''
        if not isinstance(data, str) or len(data) > (MAX_PART_BYTES * 4 // 3 + 8):
            reasons.add('text_limit')
            return ''
        try:
            raw = base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)
        except (ValueError, binascii.Error):
            reasons.add('base64_invalid')
            return ''
        content_type = Message()
        content_type['content-type'] = (part_headers.get('content-type') or [mime])[0]
        text = decode_bytes(raw, content_type.get_content_charset() or 'utf-8')
        if mime == 'text/html':
            soup = BeautifulSoup(text, 'html.parser')
            for tag in soup(['script', 'style', 'head', 'template']):
                tag.decompose()
            text = soup.get_text(separator='\n', strip=True)
        return text

    text = walk(payload)
    if len(text) > MAX_TEXT:
        reasons.add('text_limit')
        text = text[:MAX_TEXT]
    if not payload:
        reasons.add('body_missing')

    def addresses(name):
        return [{'name': name, 'address': address} for name, address in getaddresses(top_headers.get(name, []))]

    content = {
        'from': addresses('from'), 'to': addresses('to'), 'cc': addresses('cc'), 'bcc': addresses('bcc'),
        'subject': '\n'.join(top_headers.get('subject', [])),
        'header_date': '\n'.join(top_headers.get('date', [])),
        'text': text, 'sent': 'SENT' in message.get('labelIds', []),
    }
    return {
        'gmail_id': message['id'], 'thread_id': message['threadId'], 'date_gmail': date,
        'content': content, 'content_incomplete': bool(reasons), 'incomplete_reasons': sorted(reasons),
    }
