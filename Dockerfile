FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/analyzer/.venv/bin:$PATH \
    TZ=Asia/Tokyo

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/analyzer

COPY pyproject.toml uv.lock README.md ./
RUN pip install uv==0.11.26 \
    && uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev

COPY config ./config
COPY prompts ./prompts
COPY scripts/container-entrypoint.sh /usr/local/bin/reolink-analyzer

RUN useradd --create-home --uid 10001 analyzer \
    && mkdir -p /data/input /data/output /data/state \
    && chown -R analyzer:analyzer /data/output /data/state \
    && chmod 700 /data/output /data/state \
    && sed -i 's/\r$//' /usr/local/bin/reolink-analyzer \
    && chmod 755 /usr/local/bin/reolink-analyzer

USER analyzer

ENTRYPOINT ["/usr/local/bin/reolink-analyzer"]
