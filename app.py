import os
import re
import asyncio
import threading
import subprocess
import textwrap
import requests as http_requests
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
import edge_tts

app = Flask(__name__, static_folder='static')

OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/output')
KOKORO_URL  = os.environ.get('KOKORO_URL', 'http://kokoro:8880')
DEFAULT_SPEED = float(os.environ.get('TTS_SPEED', '1.0'))

os.makedirs(OUTPUT_DIR, exist_ok=True)

conversion_state = {
    'running': False,
    'paused': False,
    'stop': False,
    'progress': 0,
    'total': 0,
    'status': 'idle',
    'current_chunk': '',
    'output_file': ''
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def speed_to_edge_rate(speed: float) -> str:
    """Convert float multiplier to Edge TTS rate string e.g. '+20%'."""
    return f"{int((speed - 1.0) * 100):+d}%"

def extract_text(epub_path: str) -> str:
    txt_path = epub_path.replace('.epub', '.txt').replace('.mobi', '.txt')
    subprocess.run(['ebook-convert', epub_path, txt_path], check=True, capture_output=True)
    with open(txt_path, 'r', encoding='utf-8') as f:
        text = f.read()
    text = re.sub(r'[^\u0000-\u024F\s]', ' ', text)
    text = re.sub(r' +', ' ', text)
    os.remove(txt_path)
    return text

def make_chunks(text: str, size: int = 4000) -> list:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for s in sentences:
        if len(s) > size:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(textwrap.wrap(s, width=size, break_long_words=False))
        elif len(current) + len(s) < size:
            current += s + " "
        else:
            if current.strip():
                chunks.append(current.strip())
            current = s + " "
    if current.strip():
        chunks.append(current.strip())
    return chunks

# ---------------------------------------------------------------------------
# TTS providers
# ---------------------------------------------------------------------------

def convert_google(chunks, voice, api_key, work_dir, speed):
    try:
        from google.cloud import texttospeech
        client = texttospeech.TextToSpeechClient(client_options={"api_key": api_key})
        for i, chunk in enumerate(chunks):
            if conversion_state['stop']:
                break
            while conversion_state['paused']:
                import time; time.sleep(0.5)
                if conversion_state['stop']:
                    break
            out = os.path.join(work_dir, f'chunk_{i:04d}.mp3')
            if os.path.exists(out):
                conversion_state['progress'] = i + 1
                continue
            lang = '-'.join(voice.split('-')[:2])
            synthesis_input = texttospeech.SynthesisInput(text=chunk)
            voice_params = texttospeech.VoiceSelectionParams(language_code=lang, name=voice)
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=speed
            )
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice_params, audio_config=audio_config
            )
            with open(out, 'wb') as f:
                f.write(response.audio_content)
            conversion_state['progress'] = i + 1
            conversion_state['current_chunk'] = f'Chunk {i+1}/{len(chunks)}'
    except Exception as e:
        raise RuntimeError(f"Google TTS Error: {e}")

async def convert_edge_async(chunks, voice, work_dir, speed):
    rate = speed_to_edge_rate(speed)
    for i, chunk in enumerate(chunks):
        if conversion_state['stop']:
            break
        while conversion_state['paused']:
            import time; time.sleep(0.5)
            if conversion_state['stop']:
                break
        out = os.path.join(work_dir, f'chunk_{i:04d}.mp3')
        if os.path.exists(out):
            conversion_state['progress'] = i + 1
            continue
        communicate = edge_tts.Communicate(chunk, voice, rate=rate)
        await communicate.save(out)
        conversion_state['progress'] = i + 1
        conversion_state['current_chunk'] = f'Chunk {i+1}/{len(chunks)}'

def convert_kokoro(chunks, voice, work_dir, speed):
    url = f"{KOKORO_URL}/v1/audio/speech"
    for i, chunk in enumerate(chunks):
        if conversion_state['stop']:
            break
        while conversion_state['paused']:
            import time; time.sleep(0.5)
            if conversion_state['stop']:
                break
        out = os.path.join(work_dir, f'chunk_{i:04d}.mp3')
        if os.path.exists(out):
            conversion_state['progress'] = i + 1
            continue
        resp = http_requests.post(url, json={
            "model": "kokoro",
            "input": chunk,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed
        }, timeout=300)
        resp.raise_for_status()
        with open(out, 'wb') as f:
            f.write(resp.content)
        conversion_state['progress'] = i + 1
        conversion_state['current_chunk'] = f'Chunk {i+1}/{len(chunks)}'

# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_chunks(work_dir: str, num_chunks: int, output_file: str):
    filelist = os.path.join(work_dir, 'filelist.txt')
    with open(filelist, 'w') as f:
        for i in range(num_chunks):
            p = os.path.join(work_dir, f'chunk_{i:04d}.mp3')
            if os.path.exists(p):
                f.write(f"file '{p}'\n")

    ext = Path(output_file).suffix.lower()
    if ext == '.mp3':
        codec_args = ['-c:a', 'libmp3lame', '-q:a', '4']
    else:
        codec_args = ['-c:a', 'aac', '-b:a', '64k']

    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', filelist,
        *codec_args, output_file
    ], check=True, capture_output=True)

# ---------------------------------------------------------------------------
# Conversion thread
# ---------------------------------------------------------------------------

def run_conversion(epub_path, voice, provider, api_key, output_name, speed, fmt):
    try:
        conversion_state['status'] = 'extracting'
        conversion_state['current_chunk'] = 'Extracting text from epub...'
        text = extract_text(epub_path)

        chunk_size = 400 if provider == 'kokoro' else 4000
        chunks = make_chunks(text, size=chunk_size)

        conversion_state['total'] = len(chunks)
        conversion_state['progress'] = 0
        conversion_state['status'] = 'converting'

        work_dir = os.path.join(OUTPUT_DIR, f'work_{output_name}')
        os.makedirs(work_dir, exist_ok=True)

        ext = 'mp3' if fmt == 'mp3' else 'm4b'
        output_file = os.path.join(OUTPUT_DIR, f'{output_name}.{ext}')
        conversion_state['output_file'] = output_file

        if provider == 'google':
            convert_google(chunks, voice, api_key, work_dir, speed)
        elif provider == 'kokoro':
            convert_kokoro(chunks, voice, work_dir, speed)
        else:
            asyncio.run(convert_edge_async(chunks, voice, work_dir, speed))

        if not conversion_state['stop']:
            conversion_state['status'] = 'merging'
            conversion_state['current_chunk'] = 'Merging audio files...'
            merge_chunks(work_dir, len(chunks), output_file)
            conversion_state['status'] = 'done'
            conversion_state['current_chunk'] = 'Complete!'
        else:
            conversion_state['status'] = 'stopped'
    except Exception as e:
        conversion_state['status'] = 'error'
        conversion_state['current_chunk'] = str(e)
    finally:
        conversion_state['running'] = False

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/voices', methods=['POST'])
def get_voices():
    data = request.json
    provider = data.get('provider')

    if provider == 'edge':
        voices = asyncio.run(edge_tts.list_voices())
        result = [{'name': v['Name'], 'lang': v['Locale'], 'gender': v['Gender']} for v in voices]
        return jsonify(result)

    elif provider == 'google':
        try:
            from google.cloud import texttospeech
            api_key = data.get('api_key', '')
            client = texttospeech.TextToSpeechClient(client_options={"api_key": api_key})
            response = client.list_voices()
            # FIX: language_codes[0], not str(language_codes) — the latter breaks lang filter
            result = [
                {'name': v.name, 'lang': v.language_codes[0], 'gender': str(v.ssml_gender)}
                for v in response.voices
            ]
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    elif provider == 'kokoro':
        try:
            resp = http_requests.get(f"{KOKORO_URL}/v1/audio/voices", timeout=10)
            resp.raise_for_status()
            voices = resp.json().get('voices', [])
            result = [{'name': v, 'lang': 'en-US', 'gender': ''} for v in voices]
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': f'Kokoro unreachable: {e}'}), 503

    return jsonify([])

@app.route('/preview', methods=['POST'])
def preview():
    data    = request.json
    provider = data.get('provider')
    voice   = data.get('voice')
    api_key = data.get('api_key', '')
    speed   = float(data.get('speed', DEFAULT_SPEED))
    text    = data.get('text', 'The quick brown fox jumps over the lazy dog.')

    # FIX: rsplit returns a list — take element [0]
    if provider == 'kokoro' and len(text) > 400:
        text = text[:400].rsplit(' ', 1)[0]

    out = os.path.join(OUTPUT_DIR, 'preview.mp3')

    try:
        if provider == 'edge':
            rate = speed_to_edge_rate(speed)
            async def do_preview():
                communicate = edge_tts.Communicate(text, voice, rate=rate)
                await communicate.save(out)
            asyncio.run(do_preview())

        elif provider == 'kokoro':
            resp = http_requests.post(f"{KOKORO_URL}/v1/audio/speech", json={
                "model": "kokoro",
                "input": text,
                "voice": voice,
                "response_format": "mp3",
                "speed": speed
            }, timeout=60)
            resp.raise_for_status()
            with open(out, 'wb') as f:
                f.write(resp.content)

        else:  # google
            from google.cloud import texttospeech
            client = texttospeech.TextToSpeechClient(client_options={"api_key": api_key})
            lang = '-'.join(voice.split('-')[:2])
            synthesis_input = texttospeech.SynthesisInput(text=text)
            voice_params = texttospeech.VoiceSelectionParams(language_code=lang, name=voice)
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=speed
            )
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice_params, audio_config=audio_config
            )
            with open(out, 'wb') as f:
                f.write(response.audio_content)

    except Exception as e:
        return jsonify({'error': str(e)}), 400

    return send_file(out, mimetype='audio/mpeg')

@app.route('/convert', methods=['POST'])
def convert():
    if conversion_state['running']:
        return jsonify({'error': 'Conversion already running'}), 400
    if 'epub' not in request.files:
        return jsonify({'error': 'No epub file'}), 400

    epub     = request.files['epub']
    provider = request.form.get('provider')
    voice    = request.form.get('voice')
    api_key  = request.form.get('api_key', '')
    speed    = float(request.form.get('speed', DEFAULT_SPEED))
    fmt      = request.form.get('format', 'm4b')   # 'm4b' or 'mp3'

    epub_path = os.path.join(OUTPUT_DIR, epub.filename)
    epub.save(epub_path)
    output_name = Path(epub.filename).stem

    conversion_state.update({
        'running': True, 'paused': False, 'stop': False,
        'progress': 0, 'total': 0, 'status': 'starting',
        'current_chunk': 'Starting...', 'output_file': ''
    })

    t = threading.Thread(
        target=run_conversion,
        args=(epub_path, voice, provider, api_key, output_name, speed, fmt)
    )
    t.daemon = True
    t.start()
    return jsonify({'ok': True})

@app.route('/status')
def status():
    return jsonify(conversion_state)

@app.route('/pause', methods=['POST'])
def pause():
    conversion_state['paused'] = not conversion_state['paused']
    return jsonify({'paused': conversion_state['paused']})

@app.route('/stop', methods=['POST'])
def stop():
    conversion_state['stop'] = True
    conversion_state['running'] = False
    return jsonify({'ok': True})

@app.route('/download')
def download():
    f = conversion_state.get('output_file', '')
    if f and os.path.exists(f):
        return send_file(f, as_attachment=True)
    return jsonify({'error': 'No file'}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)