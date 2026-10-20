import logging


class OAuthLogFilter(logging.Filter):
    """Suppress callback query strings AND exception/request details, even in DEBUG."""

    def filter(self, record):
        request = getattr(record, 'request', None)
        if '/api/gmail/' in record.getMessage() or getattr(request, 'path', '').startswith('/api/gmail/'):
            record.msg = 'Gmail OAuth HTTP request (sensitive details omitted).'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            if hasattr(record, 'request'):
                record.request = None
        return True
