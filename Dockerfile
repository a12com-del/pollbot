FROM docker.io/library/python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --chmod=0644 requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data

COPY --chmod=0644 vote-bot.py config.py ./

USER 10001:10001

CMD ["python", "vote-bot.py"]