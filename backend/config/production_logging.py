"""No request body, URL, exception value or local variables in production logs."""
import logging


class MetadataOnly(logging.Filter):
    def filter(self, record):
        record.msg = 'Application event; sensitive details omitted.'
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        if hasattr(record, 'request'):
            record.request = None
        return True


def logging_config():
    return {
        'version': 1, 'disable_existing_loggers': False,
        'filters': {'metadata': {'()': 'config.production_logging.MetadataOnly'}},
        'formatters': {'safe': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}},
        'handlers': {'safe': {'class': 'logging.StreamHandler','filters':['metadata'],'formatter':'safe'}},
        'root': {'handlers':['safe'],'level':'WARNING'},
        'loggers': {name:{'handlers':['safe'],'level':'WARNING','propagate':False} for name in (
            'django','django.server','django.request','django.security','gunicorn.error','gunicorn.access',
            'google','google.genai','google.auth','oauthlib','requests_oauthlib','urllib3','requests','httpx','httpcore',
        )},
    }
