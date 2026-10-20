import logging


class AnalysisLogFilter(logging.Filter):
    def filter(self, record):
        request = getattr(record, 'request', None)
        paths = ('/api/analyses/', '/api/propositions/', '/api/sync/')
        if getattr(request, 'path', '').startswith(paths) or any(path in record.getMessage() for path in paths):
            record.msg = 'Email analysis HTTP request (sensitive details omitted).'
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
            if hasattr(record, 'request'):
                record.request = None
        return True
