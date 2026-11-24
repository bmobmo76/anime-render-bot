FROM python:3.11-slim

# Install FFmpeg, which is required for merging video streams
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render uses the PORT environment variable
CMD ["python", "main.py"]
