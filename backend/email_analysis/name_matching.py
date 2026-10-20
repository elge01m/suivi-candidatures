"""Comparison-only name normalization. Never rewrites model output or records."""
from html import unescape
import unicodedata

NAME_MATCHING_POLICY = 'one HTML-unescape pass, NFC, collapsed whitespace, case-insensitive; accents preserved'


def normalize_matching_name(value):
    if not isinstance(value, str):
        return None
    # One pass only: do not recursively reinterpret nested entity encodings.
    return ' '.join(unicodedata.normalize('NFC', unescape(value)).split()) or None
