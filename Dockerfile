FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    calibre \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    edge-tts \
    google-cloud-texttospeech \
    requests

COPY app.py .
COPY static/ static/

RUN mkdir -p /output

ENV OUTPUT_DIR=/output
ENV KOKORO_URL=http://kokoro:8880

EXPOSE 5000

CMD ["python", "app.py"]