FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY runpod-seedlab-eval-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts /app/scripts
COPY runpod-seedlab-eval-service /app/runpod-seedlab-eval-service

EXPOSE 8400

CMD ["python", "runpod-seedlab-eval-service/main.py"]
