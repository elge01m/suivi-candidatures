"""Five-minute UTC slots; timezone affects import dates, never interval duration."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import SyncProblem

INTERVAL_MINUTES = 5


def validate_timezone(zone):
    try:
        if not isinstance(zone, str) or len(zone) > 64:
            raise ValueError
        ZoneInfo(zone)
        return zone
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise SyncProblem('invalid_request') from None


def next_slot(after):
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError('An aware datetime is required.')
    value = after.astimezone(timezone.utc)
    return value.replace(second=0, microsecond=0) + timedelta(minutes=INTERVAL_MINUTES - value.minute % INTERVAL_MINUTES)
