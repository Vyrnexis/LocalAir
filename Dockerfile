FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LOCALAIR_DATA=/data
ENV LOCALAIR_HOST=0.0.0.0
ENV LOCALAIR_PORT=8787

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts
COPY config.example.toml ./config.example.toml

VOLUME ["/data"]
EXPOSE 8787

CMD ["python", "-m", "src.web_main"]
