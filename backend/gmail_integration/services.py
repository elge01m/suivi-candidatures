"""OAuth only. This module does not call the Gmail messages API or start background work."""

import math
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from config.deployment import oauth_return_url
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .crypto import OAuthConfigurationError, OAuthFailure, cipher, decrypt_tokens, encrypt_tokens
from .models import GmailConnection


SCOPES = ('https://www.googleapis.com/auth/gmail.readonly',)
AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'


class ReauthorizationRequired(OAuthFailure):
    pass


def frontend_destination():
    try:
        return oauth_return_url(settings.OAUTH_SUCCESS_REDIRECT_URL,
            production=getattr(settings, 'PRODUCTION', False), origin=getattr(settings, 'APP_ORIGIN', ''))
    except ImproperlyConfigured:
        raise OAuthConfigurationError from None


@sensitive_variables()
def configuration():
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise OAuthConfigurationError
    uri = settings.GOOGLE_REDIRECT_URI
    try:
        parsed = urlsplit(uri)
        valid_transport = parsed.scheme == 'https' or (
            not getattr(settings, 'PRODUCTION', False) and parsed.scheme == 'http'
            and parsed.hostname == 'localhost' and parsed.port == 8000
        )
        if not valid_transport or not parsed.hostname or parsed.username or parsed.password or \
                parsed.path != '/api/gmail/callback/' or parsed.query or parsed.fragment:
            raise ValueError
    except (ValueError, TypeError):
        raise OAuthConfigurationError from None
    cipher()  # Fail before redirecting to Google if encryption is unavailable.
    return {'web': {
        'client_id': settings.GOOGLE_CLIENT_ID,
        'client_secret': settings.GOOGLE_CLIENT_SECRET,
        'auth_uri': AUTH_URI,
        'token_uri': TOKEN_URI,
        'redirect_uris': [uri],
    }}


@sensitive_variables()
def build_flow(state, verifier):
    flow = Flow.from_client_config(
        configuration(), scopes=list(SCOPES), state=state,
        code_verifier=verifier, autogenerate_code_verifier=False,
    )
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def check_scopes(value):
    if isinstance(value, str):
        scopes = value.split()
    elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        scopes = list(value)
    else:
        raise OAuthFailure
    if set(scopes) != set(SCOPES):
        raise OAuthFailure
    return sorted(set(scopes))


@sensitive_variables()
def exchange_code(user, code, state, verifier):
    flow = build_flow(state, verifier)
    # Pass only the code, not the HTTP callback URL. The token endpoint is HTTPS.
    # No global OAUTHLIB_INSECURE_TRANSPORT override is needed for localhost.
    token = flow.fetch_token(code=code, timeout=10)
    scopes = check_scopes(token.get('scope'))
    if str(token.get('token_type', '')).lower() != 'bearer':
        raise OAuthFailure
    encrypted = encrypt_tokens(token.get('access_token'), token.get('refresh_token'))
    # A new connection/re-consent needs a full token pair. Never combine a new
    # Google account's access token with a previous account's refresh token.
    try:
        if token.get('expires_at') is not None:
            expiry = float(token['expires_at'])
        else:
            expiry = timezone.now().timestamp() + float(token['expires_in'])
        if not math.isfinite(expiry) or expiry <= timezone.now().timestamp():
            raise ValueError
        expires_at = datetime.fromtimestamp(expiry, tz=datetime_timezone.utc)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise OAuthFailure from None
    with transaction.atomic():
        connection, _ = GmailConnection.objects.update_or_create(
            utilisateur=user,
            defaults={'encrypted_tokens': encrypted, 'expires_at': expires_at,
                      'granted_scopes': scopes, 'credential_version': uuid.uuid4()},
        )
        from gmail_sync.services import reset_connection
        reset_connection(connection)
    return connection


class LimitedRequest(Request):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 10
        return super().__call__(*args, **kwargs)


@sensitive_variables()
def refresh_access_token(user):
    """Refresh on demand, including scheduled imports; retain the existing refresh token."""
    configuration()
    with transaction.atomic():
        connection = GmailConnection.objects.select_for_update().get(utilisateur=user)
        stored = decrypt_tokens(connection.encrypted_tokens)
        credentials = Credentials(
            token=stored['access_token'], refresh_token=stored['refresh_token'],
            token_uri=TOKEN_URI, client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET, scopes=connection.granted_scopes,
        )
        try:
            credentials.refresh(LimitedRequest())
        except RefreshError as error:
            response = error.args[1] if len(error.args) > 1 else None
            if isinstance(response, dict) and response.get('error') == 'invalid_grant':
                raise ReauthorizationRequired from None
            raise OAuthFailure from None
        except Exception:
            raise OAuthFailure from None
        scopes = connection.granted_scopes
        if credentials.granted_scopes is not None:
            scopes = check_scopes(credentials.granted_scopes)
        expiry = credentials.expiry
        if not isinstance(expiry, datetime):
            raise OAuthFailure
        if timezone.is_naive(expiry):
            expiry = timezone.make_aware(expiry, datetime_timezone.utc)
        if expiry <= timezone.now():
            raise OAuthFailure
        connection.encrypted_tokens = encrypt_tokens(
            credentials.token, credentials.refresh_token or stored['refresh_token'],
        )
        connection.expires_at = expiry
        connection.granted_scopes = scopes
        connection.save(update_fields=['encrypted_tokens', 'expires_at', 'granted_scopes', 'date_modification'])
    return connection
