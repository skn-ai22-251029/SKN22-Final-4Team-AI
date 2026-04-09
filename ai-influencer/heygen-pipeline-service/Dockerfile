FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY ai-influencer/heygen-pipeline-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY heygen_pipeline /app/heygen_pipeline
COPY ai-influencer/heygen-pipeline-service/main.py /app/main.py

EXPOSE 8100

CMD ["python", "main.py"]
