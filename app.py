# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, send_file, after_this_request, jsonify, send_from_directory
import yt_dlp
import os
import uuid
import shutil
import subprocess, os
from pathlib import Path
from werkzeug.utils import secure_filename


def _run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stdout)
    return p.stdout

def refine_guitars_3(stems_dir: Path):
    """Toma other.wav y genera 3 guitarras: lead, left, right."""
    other = stems_dir / "other.wav"
    if not other.exists():
        # Si separaste en 5 stems, 'other' ya no tiene piano ni baterías.
        # Si no existe (raro), salgo sin romper.
        return {}

    out = {
        "guitar_lead": stems_dir / "guitar_lead.wav",
        "guitar_left": stems_dir / "guitar_left.wav",
        "guitar_right": stems_dir / "guitar_right.wav",
    }

    # Lead: contenido central + presencia
    _run([
        "ffmpeg","-y","-i",str(other),"-filter_complex",
        "pan=stereo|c0=0.6*FL+0.6*FR|c1=0.6*FL+0.6*FR,"
        "highpass=f=300,bandpass=f=1800:w=3000:t=h,"
        "equalizer=f=2500:t=h:width_type=q:width=0.9:g=5,"
        "equalizer=f=5200:t=h:width_type=q:width=0.9:g=4,"
        "alimiter=limit=0.9",
        str(out["guitar_lead"])
    ])

    # Rítmica Izquierda: side L con cuerpo
    _run([
        "ffmpeg","-y","-i",str(other),"-filter_complex",
        "pan=stereo|c0=0.9*FL-0.9*FR|c1=0*FR,"
        "highpass=f=120,lowpass=f=3500,"
        "equalizer=f=220:t=h:width_type=q:width=1.0:g=5,"
        "equalizer=f=800:t=h:width_type=q:width=1.0:g=3,"
        "alimiter=limit=0.9",
        str(out["guitar_left"])
    ])

    # Rítmica Derecha: side R con cuerpo
    _run([
        "ffmpeg","-y","-i",str(other),"-filter_complex",
        "pan=stereo|c0=0*FL|c1=0.9*FR-0.9*FL,"
        "highpass=f=120,lowpass=f=3500,"
        "equalizer=f=220:t=h:width_type=q:width=1.0:g=5,"
        "equalizer=f=800:t=h:width_type=q:width=1.0:g=3,"
        "alimiter=limit=0.9",
        str(out["guitar_right"])
    ])

    # Devuelvo rutas existentes
    return {k: str(v) for k,v in out.items() if v.exists()}

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # hasta 1 GB por si subís videos grandes

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_FOLDER = BASE_DIR / "downloads"
MEDIA_DIR = BASE_DIR / "media"
STEMS_DIR = MEDIA_DIR / "stems"
AUDIO_DIR = MEDIA_DIR / "audio"
UPLOADS_DIR = MEDIA_DIR / "uploads"  # para reproducir archivos subidos (muted) mientras oís los stems

for d in (DOWNLOAD_FOLDER, MEDIA_DIR, STEMS_DIR, AUDIO_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------- UTILIDADES ----------
YDL_COMMON = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'retries': 10,
    'fragment_retries': 10,
    'concurrent_fragment_downloads': 5,
}

def ytdlp_extract_info(url: str):
    opts = {**YDL_COMMON, 'skip_download': True, 'extract_flat': False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def ytdlp_download_audio(url: str, out_stem: Path):
    """
    Descarga bestaudio a AUDIO_DIR. out_stem no lleva extensión; se detecta luego.
    Devuelve (ruta_audio, info).
    """
    outtmpl = str(out_stem)
    with yt_dlp.YoutubeDL({**YDL_COMMON, 'outtmpl': outtmpl, 'format': 'bestaudio/best'}) as ydl:
        info = ydl.extract_info(url, download=True)

    p = out_stem
    if not p.suffix:
        # intentar encontrar la extensión real
        for ext in ('.m4a', '.webm', '.opus', '.mp3', '.wav'):
            if p.with_suffix(ext).exists():
                p = p.with_suffix(ext)
                break
    return str(p), info

def ffmpeg_extract_audio(input_path: str, out_wav: str):
    """
    Extrae/convierte audio a WAV (mono o estéreo según fuente) usando ffmpeg.
    """
    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-vn',  # sin video
        '-acodec', 'pcm_s16le',
        '-ar', '44100',
        out_wav
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

def run_spleeter(input_audio: str, outdir: Path, stems: int = 4):
    """
    Ejecuta Spleeter por CLI para 4 o 5 stems.
    Genera WAVs: vocals.wav, drums.wav, bass.wav, other.wav (+ piano.wav si 5 stems)
    """
    if stems not in (4, 5):
        stems = 4
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        'spleeter', 'separate',
        '-p', f'spleeter:{stems}stems',
        '-o', str(outdir),
        input_audio
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    # Spleeter crea subcarpeta con el nombre del archivo base; mover a outdir raíz
    produced = outdir / Path(input_audio).stem
    if produced.is_dir():
        for f in produced.iterdir():
            shutil.move(str(f), str(outdir / f.name))
        produced.rmdir()

def stems_map(stem_dir: Path):
    result = {}
    for name in ('vocals', 'drums', 'bass', 'other', 'piano'):
        f = stem_dir / f"{name}.wav"
        if f.exists():
            result[name] = f"/media/stems/{stem_dir.name}/{f.name}"
    return result

# ---------- RUTAS EXISTENTES (DESCARGA) ----------
@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Mantengo tu flujo de descarga:
    - POST con url (sin format_id) => consulta calidades
    - POST con url + format_id => descarga y dispara send_file
    """
    if request.method == 'POST':
        url = request.form.get('url')
        format_id = request.form.get('format_id')

        if url and not format_id:
            try:
                info = ytdlp_extract_info(url)
                formats = [
                    {
                        'format_id': f['format_id'],
                        'ext': f['ext'],
                        'resolution': f.get('format_note') or f.get('height'),
                        'filesize': f.get('filesize')
                    }
                    for f in info.get('formats', [])
                    if f.get('ext') == 'mp4' and f.get('acodec') != 'none' and f.get('vcodec') != 'none'
                ]
                return render_template('index.html', url=url, formats=formats)
            except Exception as e:
                return f"<h3>Error: {e}</h3>"

        elif url and format_id:
            filename = f"{uuid.uuid4()}.mp4"
            filepath = DOWNLOAD_FOLDER / filename

            ydl_opts = {
                'outtmpl': str(filepath),
                'format': format_id,
                **YDL_COMMON
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                @after_this_request
                def remove_file(response):
                    try:
                        if filepath.exists():
                            filepath.unlink()
                    except Exception as e:
                        print(f"Error eliminando archivo: {e}")
                    return response

                return send_file(str(filepath), as_attachment=True)
            except Exception as e:
                return f"<h3>Error al descargar: {e}</h3>"

    return render_template('index.html', formats=None)

# ---------- API NUEVA: INFO DE VIDEO (para previsualización) ----------
@app.route('/api/info', methods=['POST'])
def api_info():
    data = request.get_json(force=True)
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Falta URL'}), 400
    try:
        info = ytdlp_extract_info(url)
        return jsonify({
            'title': info.get('title'),
            'id': info.get('id'),
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------- API NUEVA: SEPARAR DESDE URL DE YOUTUBE ----------
@app.route('/api/separate/upload', methods=['POST'])
def api_separate_upload():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No se envió ningún archivo'}), 400

        f = request.files['file']
        if not f.filename:
            return jsonify({'error': 'Archivo inválido'}), 400

        # Asegurar carpetas
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        STEMS_DIR.mkdir(parents=True, exist_ok=True)

        # Guardar archivo subido
        uid = str(uuid.uuid4())
        up_name = f"{uid}_{secure_filename(f.filename)}"
        save_path = UPLOADS_DIR / up_name
        f.save(save_path)

        # Convertir a WAV para Spleeter
        wav_path = STEMS_DIR / f"{uid}.wav"
        ffmpeg_extract_audio(str(save_path), str(wav_path))

        # Separar en 5 stems (voz, bajo, piano, batería, otros)
        out_dir = STEMS_DIR / f"stems_{uid}"
        run_spleeter(str(wav_path), out_dir, stems=5)

        # Refinar 'other.wav' en 3 guitarras (lead / left / right)
        g3 = refine_guitars_3(out_dir)  # debe devolver dict con rutas o booleanos por cada guitarra

        # Construir mapa final (voz, 3 guitarras, bajo, teclado, batería)
        stems = {}
        if (out_dir / "vocals.wav").exists():
            stems["vocals"] = f"/media/stems/stems_{uid}/vocals.wav"
        if (out_dir / "bass.wav").exists():
            stems["bass"] = f"/media/stems/stems_{uid}/bass.wav"
        if (out_dir / "piano.wav").exists():
            stems["piano"] = f"/media/stems/stems_{uid}/piano.wav"
        if (out_dir / "drums.wav").exists():
            stems["drums"] = f"/media/stems/stems_{uid}/drums.wav"
        if g3.get("guitar_lead"):
            stems["guitar_lead"] = f"/media/stems/stems_{uid}/guitar_lead.wav"
        if g3.get("guitar_left"):
            stems["guitar_left"] = f"/media/stems/stems_{uid}/guitar_left.wav"
        if g3.get("guitar_right"):
            stems["guitar_right"] = f"/media/stems/stems_{uid}/guitar_right.wav"

        return jsonify({
            'ok': True,
            'uid': uid,
            'uploaded_url': f"/media/uploads/{up_name}",
            'stems': stems
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------- SERVIR MEDIOS ----------
@app.route('/media/stems/<path:subpath>')
def serve_stems(subpath):
    return send_from_directory(STEMS_DIR, subpath, as_attachment=False)

@app.route('/media/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOADS_DIR, filename, as_attachment=False)

if __name__ == '__main__':
    app.run(debug=True)
