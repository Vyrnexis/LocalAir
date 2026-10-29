FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LOCALAIR_DATA=/data
ENV LOCALAIR_HOST=0.0.0.0
ENV LOCALAIR_PORT=8787
ENV TZ=Etc/UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"LOCALAIR_PORT\", \"8787\")}/api/status', timeout=4)"

CMD ["python", "-m", "src.web_main"]
