FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY video_bot ./video_bot
CMD ["python", "-m", "video_bot"]
