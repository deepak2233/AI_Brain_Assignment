FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    BETTERBARK_STATE=/data/state/state.db

RUN groupadd --gid 10001 betterbark \
    && useradd --uid 10001 --gid betterbark --no-create-home betterbark \
    && mkdir -p /app /data/state \
    && chown -R betterbark:betterbark /app /data

WORKDIR /app
COPY --chown=betterbark:betterbark solution /app/solution
COPY --chown=betterbark:betterbark data /app/data
COPY --chown=betterbark:betterbark transcripts /app/transcripts
COPY --chown=betterbark:betterbark stubs /app/stubs

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-m", "solution", "--state", "/data/state/state.db", "health"]

ENTRYPOINT ["python", "-m", "solution"]
CMD ["--state", "/data/state/state.db", "status"]
