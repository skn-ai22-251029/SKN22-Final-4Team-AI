FROM python:3.11-slim

WORKDIR /app

# 의존성을 먼저 복사해 레이어 캐싱 활용
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 비루트 유저 실행
RUN useradd -m appuser
USER appuser

EXPOSE 8080

CMD ["python", "main.py"]
