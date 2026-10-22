FROM python:3.12-slim

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    DB_PATH=/data/todo.db \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

# Непривилегированный пользователь + каталог для SQLite (подключается как volume)
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
VOLUME /data
EXPOSE 8000

# Docker сам следит за /health (статус виден в `docker ps`)
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"

# 1 процесс + потоки: счётчики prometheus_client живут в памяти процесса
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "main:app"]
