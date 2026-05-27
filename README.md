# epub to audiobook TTS converter

# epub_to_audiobook_tts

Convert epub/mobi ebooks to audiobooks (`.m4b` or `.mp3`) using a web UI and your choice of TTS provider.

## Features

- **Three TTS providers:**
  - **Google Chirp 3 HD** — high quality, supports Croatian and other languages (requires API key)
  - **Edge TTS** — free, Microsoft voices, good Croatian support
  - **Kokoro** — local AI TTS, English voices, runs fully offline via sidecar container
- Upload epub or mobi, get an audiobook back
- Adjustable speed and output format (m4b / mp3)
- Pause, resume, and stop mid-conversion
- Voice preview before converting
- Progress tracking per chunk
- Picks up where it left off if interrupted (chunk caching)

## Stack

- Flask (Python) — backend + API
- Edge TTS, Google Cloud TTS, Kokoro-FastAPI — TTS engines
- FFmpeg — audio merging and encoding
- Calibre (`ebook-convert`) — epub/mobi text extraction
- Vanilla JS frontend — no framework

## Quick Start

```bash
git clone https://github.com/fiksr/epub_to_audiobook_tts.git
cd epub_to_audiobook_tts
docker compose up --build
```

Open http://localhost:5001

## Docker Compose

Two containers:

| Container | Port | Description |
|-----------|------|-------------|
| `tts` | 5001 | Flask web app |
| `kokoro` | 8880 | Kokoro-FastAPI CPU sidecar |

```yaml
version: '3.8'
services:
  tts:
    build: .
    ports:
      - "5001:5000"
    volumes:
      - ./output:/output
    environment:
      - KOKORO_URL=http://kokoro:8880
    depends_on:
      - kokoro
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: "4"
          memory: 2048M

  kokoro:
    image: ghcr.io/remsky/kokoro-fastapi-cpu:latest
    ports:
      - "8880:8880"
    environment:
      - OMP_NUM_THREADS=4
      - MKL_NUM_THREADS=4
      - TORCH_NUM_THREADS=4
      - MAX_WORKERS=1
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: "4"
          memory: 4G
```

CPU thread limits are set via environment variables — adjust `cpus` and `*_NUM_THREADS` to match your hardware.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `/output` | Where converted files are saved |
| `KOKORO_URL` | `http://kokoro:8880` | Kokoro sidecar URL |
| `TTS_SPEED` | `1.0` | Default speed (can be overridden from UI) |

## Google API Key

To use Google Chirp 3 HD voices, you need a Google Cloud API key with the **Cloud Text-to-Speech API** enabled. Enter it in the UI — it is saved in browser localStorage and never sent anywhere except directly to Google's API.

## Output

Converted files land in `./output/`. Work chunks are cached in `./output/work_<bookname>/` so interrupted conversions can resume without re-synthesizing completed chunks.

## License

MIT
