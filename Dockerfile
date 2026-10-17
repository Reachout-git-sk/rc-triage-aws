# One image, two uses: the API (default command) and the fake GPU-metrics upstream.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Amazon's CA bundle for RDS, so the API can verify the database's TLS certificate
# (sslmode=verify-full) instead of trusting any server that answers.
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
RUN chmod 644 /etc/ssl/certs/rds-global-bundle.pem

COPY gunicorn.conf.py .
COPY api ./api
COPY migrations ./migrations
COPY fake_gpu_metrics ./fake_gpu_metrics

# The git SHA of this build; /health returns it so a deploy can tell old from new.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Do not run as root inside the container.
RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "api.app:create_app()"]
