"""Small REST adapter. Only profile, message listing and message body reads are allowed."""

import json
import time
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .crypto import OAuthFailure, decrypt_tokens
from .import_errors import ImportProblem
from .message_parser import valid_id
from .services import ReauthorizationRequired, SCOPES, refresh_access_token


BASE_URL = 'https://gmail.googleapis.com/gmail/v1/users/me'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GmailClient:
    @sensitive_variables()
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.transport = requests.Session()
        if set(connection.granted_scopes) != set(SCOPES):
            raise ImportProblem('reauthorization_required')
        if connection.expires_at <= timezone.now() + timedelta(seconds=30):
            self._refresh()

    @sensitive_variables()
    def _refresh(self):
        try:
            self.connection = refresh_access_token(self.connection.utilisateur)
        except ReauthorizationRequired:
            raise ImportProblem('reauthorization_required') from None
        except OAuthFailure:
            raise ImportProblem('gmail_unavailable', retryable=True) from None

    @staticmethod
    def _retry_seconds(value):
        try:
            return max(1, min(86400, int(value)))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                return max(1, min(86400, int((date - timezone.now()).total_seconds())))
            except (TypeError, ValueError, OverflowError):
                return None

    @sensitive_variables()
    def _get(self, path, params=None, refreshed=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ImportProblem('time_budget')
        tokens = decrypt_tokens(self.connection.encrypted_tokens)
        try:
            response = self.transport.get(
                BASE_URL + path, params=params,
                headers={'Authorization': 'Bearer ' + tokens['access_token']},
                timeout=(min(3, remaining), min(5, remaining)), stream=True, allow_redirects=False,
            )
            with response:
                status = response.status_code
                if status == 401:
                    if refreshed:
                        raise ImportProblem('reauthorization_required')
                    self._refresh()
                    return self._get(path, params, refreshed=True)
                if status >= 500:
                    raise ImportProblem('gmail_unavailable', retryable=True)
                if status == 404:
                    raise ImportProblem('message_missing')
                if status == 429:
                    raise ImportProblem('rate_limited', retryable=True,
                                        retry_seconds=self._retry_seconds(response.headers.get('Retry-After')))
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() >= self.deadline:
                        raise ImportProblem('time_budget')
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ImportProblem('response_too_large')
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ImportProblem('gmail_response_invalid') from None
                if status == 403:
                    error = body.get('error', {}) if isinstance(body, dict) else {}
                    details = error.get('errors', []) if isinstance(error, dict) else []
                    reasons = {item.get('reason') for item in details if isinstance(item, dict)}
                    if reasons & {'rateLimitExceeded', 'userRateLimitExceeded', 'dailyLimitExceeded'}:
                        raise ImportProblem('rate_limited', retryable=True,
                                            retry_seconds=86400 if 'dailyLimitExceeded' in reasons else None)
                    raise ImportProblem('gmail_access_denied')
                if status == 400:
                    raise ImportProblem('invalid_page' if params and params.get('pageToken') else 'gmail_response_invalid')
                if status != 200 or not isinstance(body, dict):
                    raise ImportProblem('gmail_response_invalid')
                return body
        except requests.RequestException:
            raise ImportProblem('gmail_unavailable', retryable=True) from None

    def profile(self):
        body = self._get('/profile', {'fields': 'emailAddress'})
        email = body.get('emailAddress')
        if not isinstance(email, str) or '@' not in email or len(email) > 320:
            raise ImportProblem('gmail_response_invalid')
        return email.strip().casefold()

    def list_page(self, query, page_token):
        params = {'q': query, 'includeSpamTrash': 'false', 'maxResults': 100,
                  'fields': 'messages(id,threadId),nextPageToken'}
        if page_token:
            params['pageToken'] = page_token
        body = self._get('/messages', params)
        messages = body.get('messages', [])
        next_token = body.get('nextPageToken')
        if not isinstance(messages, list) or any(not isinstance(item, dict) or not valid_id(item.get('id')) for item in messages):
            raise ImportProblem('gmail_response_invalid')
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise ImportProblem('gmail_response_invalid')
        return [item['id'] for item in messages], next_token

    def message(self, gmail_id):
        if not valid_id(gmail_id):
            raise ImportProblem('gmail_response_invalid')
        try:
            # No RAW format and no /attachments endpoint. Attached MIME parts that
            # Google embeds in FULL responses are discarded by the parser.
            return self._get('/messages/' + gmail_id, {
                'format': 'full', 'fields': 'id,threadId,labelIds,internalDate,payload',
            }), []
        except ImportProblem as error:
            if error.code != 'response_too_large':
                raise
            metadata = self._get('/messages/' + gmail_id, {
                'format': 'metadata',
                'metadataHeaders': ['From', 'To', 'Cc', 'Bcc', 'Subject', 'Date'],
                'fields': 'id,threadId,labelIds,internalDate,payload(headers)',
            })
            return metadata, ['response_too_large']

    def close(self):
        self.transport.close()
