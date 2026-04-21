FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY seed-lab-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts ./scripts
COPY seed-lab-service ./seed-lab-service

RUN groupadd -g 10001 appuser \
    && useradd -m -u 10001 -g 10001 appuser
USER appuser

EXPOSE 8300

CMD ["python", "seed-lab-service/main.py"]
