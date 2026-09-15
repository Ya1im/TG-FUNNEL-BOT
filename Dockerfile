FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

RUN useradd -m -u 1000 botuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

RUN mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "bot"]
