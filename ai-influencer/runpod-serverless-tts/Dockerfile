FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY handler.py /app/handler.py

# NOTE:
# - This image assumes GPT-SoVITS API server is available in the same container
#   or sidecar at TTS_LOCAL_API_BASE (default: http://127.0.0.1:9880).
# - Model/weights paths are expected to be mounted from RunPod network volume.

CMD ["python", "-u", "handler.py"]
