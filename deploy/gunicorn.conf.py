"""Loaded from the release root; Unix only. No access/query/body logging."""
import os

bind = '127.0.0.1:8001'
workers = 2
worker_class = 'sync'
timeout = 60
graceful_timeout = 30
keepalive = 2
forwarded_allow_ips = '127.0.0.1'
accesslog = None
errorlog = '-'
capture_output = False
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190
worker_tmp_dir = os.environ.get('RUNTIME_DIRECTORY', '/tmp')

from config.production_logging import logging_config
logconfig_dict = logging_config()
