"""gunicorn settings for the API container.

gthread workers: each worker process runs a few threads, so one request that waits on
a slow upstream does not block the whole process. Requests are logged by the app
itself as JSON, so gunicorn's own access log is off.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = 30  # a worker stuck for 30 s is killed and replaced
graceful_timeout = 20  # on SIGTERM, finish in-flight requests for up to 20 s
keepalive = 5
# gunicorn 26 opens a control socket in $HOME by default; the app user has no home
# directory and we do not use the socket, so turn it off.
control_socket_disable = True
accesslog = None
errorlog = "-"
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "api.logging_json.JsonFormatter"}},
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"level": "INFO", "handlers": ["stdout"]},
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["stdout"], "propagate": False},
        "gunicorn.access": {"level": "INFO", "handlers": [], "propagate": False},
    },
}
