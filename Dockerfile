# BTC 5m runner image (#32). Builds a working strategy container:
# Python deps from requirements.txt, skill mounted at /skill.
#
# Build: docker build -t btc5m .
# Dry-run session (default, no orders): docker compose up
# Live session: scripts/btc5m_docker.sh run -- --profile conservative --execute
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /skill

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "scripts/test_btc_5m_session_exit_sl.py"]
# Default = conservative dry-run (no --execute flag -> no orders possible).
CMD ["--profile", "conservative", "--entry-timeout-min", "60"]
