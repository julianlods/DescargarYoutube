# -*- coding: utf-8 -*-
import os, io
import sys
import uuid
import shutil
import time
import subprocess
from pathlib import Path
from typing import Dict, List
from tempfile import TemporaryDirectory
from threading import Lock
from flask import (
    Flask, render_template, request, redirect, url_for, send_file,
    send_from_directory, abort, after_this_request, jsonify,
    Response, stream_with_context, session
)
from werkzeug.utils import secure_filename
import yt_dlp
import numpy as np, soundfile as sf, os

# --- Split simple de guitarra en Lead vs Rítmica (energía + suavizado) ---
def split_guitar_lead_rhythm(in_wav: str, out_lead: str, out_rhythm: str):
    """
    Genera dos archivos a partir de guitar.wav:
      - out_lead    : solo (lead)
      - out_rhythm  : acompañamiento (complemento)
    Estrategia: RMS por ventana + percentil, une gaps cortos y aplica fades para evitar clics.
    """
    if not os.path.isfile(in_wav):
        return False

    try:
        y, sr = sf.read(in_wav, always_2d=True)     # (N, C)
        mono = y.mean(axis=1)

        # Ventaneo
        win = max(1024, int(sr * 0.046))  # ~46 ms
        hop = max(256,  int(sr * 0.010))  # ~10 ms
        n   = len(mono)
        nF  = 1 + max(0, (n - win) // hop)

        # RMS por frame
        rms = np.empty(nF, dtype=np.float32)
        for i in range(nF):
            s = mono[i*hop:i*hop+win]
            if s.size == 0: s = np.zeros(1, dtype=np.float32)
            rms[i] = np.sqrt((s*s).mean() + 1e-12)

        # Suavizado (media móvil corta)
        w = 12
        rms_s = np.copy(rms)
        acc = 0.0; q = []
        for i,x in enumerate(rms):
            q.append(x); acc += x
            if len(q) > w: acc -= q.pop(0)
            rms_s[i] = acc / len(q)

        # Umbral por percentil
        th = float(np.quantile(rms_s, 0.80) * 0.9)
        is_lead = rms_s > th

        # Postproceso: unir gaps < 0.25s y descartar tramos < 0.5s
        min_frames  = int(0.50 / (hop/sr))
        join_frames = int(0.25 / (hop/sr))

        # a intervalos
        intervals = []
        on = False; s = 0
        for i,flag in enumerate(is_lead):
            if flag and not on:
                on = True; s = i
            elif not flag and on:
                on = False; intervals.append([s, i-1])
        if on: intervals.append([s, len(is_lead)-1])

        # unir cercanos
        merged = []
        for seg in intervals:
            if not merged:
                merged.append(seg); continue
            last = merged[-1]
            if seg[0] - last[1] <= join_frames:
                last[1] = max(last[1], seg[1])
            else:
                merged.append(seg)

        # filtrar por duración mínima
        merged = [seg for seg in merged if (seg[1]-seg[0]+1) >= min_frames]

        # máscaras por muestra
        lead_mask = np.zeros(n, dtype=np.float32)
        for s_f,e_f in merged:
            s_smp = s_f*hop
            e_smp = min(n-1, e_f*hop + win)
            lead_mask[s_smp:e_smp] = 1.0

        # fades en bordes (10 ms)
        fade = max(1, int(sr*0.010))
        # ramp up
        i = 1
        while i < n:
            if lead_mask[i-1]==0 and lead_mask[i]==1:
                end = min(i+fade, n)
                ramp = np.linspace(0,1,end-i, dtype=np.float32)
                lead_mask[i:end] = np.maximum(lead_mask[i:end], ramp)
            if lead_mask[i-1]==1 and lead_mask[i]==0:
                end = min(i+fade, n)
                ramp = np.linspace(1,0,end-i, dtype=np.float32)
                lead_mask[i:end] = np.minimum(lead_mask[i:end], ramp)
            i += 1

        rhythm_mask = 1.0 - lead_mask

        # aplica máscaras a todas las columnas (estéreo)
        lead   = (y.T * lead_mask).T
        rhythm = (y.T * rhythm_mask).T

        sf.write(out_lead,   lead,   sr)
        sf.write(out_rhythm, rhythm, sr)
        return True
    except Exception:
        # fallback: si falla, deja todo en rítmica y lead vacío
        try:
            sf.write(out_lead,   np.zeros_like(y), sr)
            sf.write(out_rhythm, y,                sr)
        except Exception:
            return False
        return True

# ──────────────────────────────────────────────────────────────────────────────
# Config básica
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev')
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # hasta 1GB

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_FOLDER = BASE_DIR / "downloads"
MEDIA_DIR = BASE_DIR / "media"
AUDIO_DIR = MEDIA_DIR / "audio"
UPLOADS_DIR = MEDIA_DIR / "uploads"
STEMS_DIR = MEDIA_DIR / "stems"

for d in (DOWNLOAD_FOLDER, MEDIA_DIR, AUDIO_DIR, UPLOADS_DIR, STEMS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# yt-dlp (para /descargar)
# ──────────────────────────────────────────────────────────────────────────────
YDL_COMMON = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'retries': 10,
    'fragment_retries': 10,
    'concurrent_fragment_downloads': 5,
}

def ytdlp_extract_info(url: str):
    """Obtiene info del video (formatos)."""
    opts = {**YDL_COMMON, 'skip_download': True, 'extract_flat': False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def list_mp4_formats(info: dict):
    """Filtra MP4 con audio+video."""
    fmts = []
    for f in info.get('formats', []):
        if f.get('ext') == 'mp4' and f.get('acodec') != 'none' and f.get('vcodec') != 'none':
            fmts.append({
                'format_id': f['format_id'],
                'ext': f['ext'],
                'resolution': f.get('format_note') or f.get('height'),
                'filesize': f.get('filesize')
            })
    return fmts

def download_by_format(url: str, format_id: str, out_path: Path) -> Path:
    """Descarga el formato elegido a out_path (MP4)."""
    ydl_opts = {'outtmpl': str(out_path), 'format': format_id, **YDL_COMMON}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return out_path

# ──────────────────────────────────────────────────────────────────────────────
# FFmpeg helpers
# ──────────────────────────────────────────────────────────────────────────────

# Cache en memoria de stems (ttl ~ 1 hora)
MEM_STEMS = {}  # sid -> {"ts": epoch, "stems": {"vocals": bytes, ...}}
MEM_LOCK = Lock()

def _purge_mem(ttl=3600):
    now = time.time()
    with MEM_LOCK:
        to_del = [sid for sid, v in MEM_STEMS.items() if now - v.get("ts", now) > ttl]
        for sid in to_del:
            MEM_STEMS.pop(sid, None)


def ffmpeg_extract_audio(input_path: str, out_wav: str):
    """
    Convierte a WAV 44.1kHz s16le (estándar).
    """
    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-vn',
        '-acodec', 'pcm_s16le',
        '-ar', '44100',
        out_wav
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def safe_unlink(p: Path):
    try:
        if p and p.exists():
            p.unlink()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Demucs (6 stems: vocals, drums, bass, other, guitar, piano)
# ──────────────────────────────────────────────────────────────────────────────
def run_demucs6(input_audio: str, outdir: Path):
    """
    Usa Demucs v4 (htdemucs_6s) para separar:
    vocals, drums, bass, other, guitar, piano
    Fuerza CPU en Windows (evita errores de CUDA si no hay GPU).
    Loguea stderr para ver el motivo real si falla.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "demucs.separate",
        "-n", "htdemucs_6s",
        "-d", "cpu",                 # <── fuerza CPU
        "-o", str(outdir),
        input_audio
    ]
    # Ejecutamos capturando stderr para reportar el motivo real
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Demucs falló (htdemucs_6s). Detalle:\n{p.stderr}")

    # Aplana la salida: outdir/htdemucs_6s/<basename>/*.wav  → outdir/*.wav
    base = Path(input_audio).stem
    model_folder = outdir / "htdemucs_6s" / base
    if model_folder.is_dir():
        for f in model_folder.iterdir():
            if f.suffix.lower() == ".wav":
                shutil.move(str(f), str(outdir / f.name))
        # limpiar carpetas si quedaron vacías (ignorar errores)
        try:
            model_folder.rmdir()
            (outdir / "htdemucs_6s").rmdir()
        except Exception:
            pass


def separate_to_memory(input_wav_path: Path):
    """
    Ejecuta Demucs en un directorio TEMPORAL, carga los WAV en memoria (bytes)
    y borra todo del disco al terminar. Devuelve (sid, stems_present).
    """
    _purge_mem()
    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        run_demucs6(str(input_wav_path), tmpdir)  # genera .wav en tmpdir y los aplana

        stems_bytes = {}
        for name in STEM_ORDER:
            f = tmpdir / f"{name}.wav"
            if f.exists():
                stems_bytes[name] = f.read_bytes()

        # ── 1.b) Experimental: dividir guitarra en Lead / Rítmica si existe ─────────
        gtr_wav = tmpdir / "guitar.wav"
        if gtr_wav.exists():
            out_lead   = tmpdir / "gtr_lead.wav"
            out_rhythm = tmpdir / "gtr_rhythm.wav"
            try:                
                ok = split_guitar_lead_rhythm(str(gtr_wav), str(out_lead), str(out_rhythm))
                if ok and out_lead.exists() and out_rhythm.exists():
                    # sumá gtr_lead / gtr_rhythm pero conservá 'guitar' para separar.html
                    stems_bytes['gtr_lead']   = out_lead.read_bytes()
                    stems_bytes['gtr_rhythm'] = out_rhythm.read_bytes()
                    # NO removemos 'guitar'
            except Exception:
                # si falla el análisis, dejamos la guitarra original sin cambios
                pass

    # Guardamos en memoria con un id de sesión
    sid = uuid.uuid4().hex[:10]
    with MEM_LOCK:
        MEM_STEMS[sid] = {"ts": time.time(), "stems": stems_bytes}

    return sid, list(stems_bytes.keys())


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de mix
# ──────────────────────────────────────────────────────────────────────────────
STEM_ORDER = ['vocals', 'guitar', 'bass', 'drums', 'piano', 'other']

def sanitize_gain(raw) -> float:
    """
    Acepta 0..1 ó 0..100. Devuelve 0..1.
    """
    if raw is None:
        return 1.0
    try:
        v = float(raw)
    except Exception:
        return 1.0
    if v < 0:
        return 0.0
    # Si es porcentaje
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))

def build_mix_filter_and_inputs(stem_dir: Path, gains: Dict[str, float]):
    """
    Construye inputs y filter_complex (volume + amix) para FFmpeg.
    Retorna (inputs_list, filter_complex_str, num_inputs)
    """
    inputs: List[str] = []
    filters: List[str] = []
    amix_ins: List[str] = []

    idx = 0
    for name in STEM_ORDER:
        stem_path = stem_dir / f"{name}.wav"
        if not stem_path.exists():
            continue
        g = float(gains.get(name, 1.0))
        if g <= 0:
            continue  # mute

        inputs += ['-i', str(stem_path)]
        filters.append(f'[{idx}:a]volume={g}[a{idx}]')
        amix_ins.append(f'[a{idx}]')
        idx += 1

    if not amix_ins:
        # Si todo está muteado, metemos un "anullsrc" (silencio)
        return [], "anullsrc=r=44100:cl=stereo[mix]", 0

    filter_complex = ';'.join(filters) + ';' + ''.join(amix_ins) + f'amix=inputs={len(amix_ins)}:normalize=0[mix]'
    return inputs, filter_complex, len(amix_ins)

# ──────────────────────────────────────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def home():
    # Si alguien hace POST a "/", preservamos método al ir a /descargar (evita 405)
    return redirect(url_for("descargar"), code=307)

# ─── 1) DESCARGAR MP4 DE YOUTUBE ──────────────────────────────────────────────
@app.route('/descargar', methods=['GET', 'POST'])
def descargar():
    """
    Flujo:
    - POST con url (sin format_id): lista calidades
    - POST con url + format_id: descarga y envía el archivo
    """
    if request.method == 'POST':
        url = (request.form.get('url') or "").strip()
        format_id = request.form.get('format_id')

        if url and not format_id:
            try:
                info = ytdlp_extract_info(url)
                formats = list_mp4_formats(info)
                return render_template('descargar.html', url=url, formats=formats)
            except Exception as e:
                return render_template('descargar.html', url=url, formats=None, error=str(e))

        elif url and format_id:
            filename = f"{uuid.uuid4()}.mp4"
            filepath = DOWNLOAD_FOLDER / filename
            try:
                download_by_format(url, format_id, filepath)

                @after_this_request
                def _cleanup(response):
                    try:
                        if filepath.exists():
                            filepath.unlink()
                    except Exception as ex:
                        print(f"Error limpiando archivo: {ex}")
                    return response

                return send_file(str(filepath), as_attachment=True, download_name="video.mp4")
            except Exception as e:
                return render_template('descargar.html', url=url, formats=None, error=f"Error al descargar: {e}")

    # GET
    return render_template('descargar.html', formats=None)

# ─── 2) SEPARAR / “MODO MOISES” (solo archivo subido) ────────────────────────
@app.route("/separar", methods=["GET", "POST"])
def separar():
    if request.method == "GET":
        return render_template("separar.html")

    # SOLO archivo subido (sin URL)
    if "file" not in request.files or not request.files["file"].filename:
        return render_template("separar.html", error="Subí un archivo de audio o video.")

    try:
        f = request.files["file"]
        fname = secure_filename(f.filename)
        up_path = UPLOADS_DIR / f"{uuid.uuid4()}_{fname}"
        f.save(up_path)

        # Convertimos a WAV 44.1kHz
        wav_path = AUDIO_DIR / f"{uuid.uuid4()}.wav"
        ffmpeg_extract_audio(str(up_path), str(wav_path))

        # Separar → CARGAR EN MEMORIA (NO persistimos en disco)
        mem_sid, present = separate_to_memory(wav_path)
        session['mem_session'] = mem_sid
        session['stems_present'] = present
        safe_unlink(up_path)
        safe_unlink(wav_path)

        return render_template(
            "separar.html",
            src_name=Path(wav_path).name,
            mem_session=mem_sid,
            stems_present=present
        )

    except Exception as e:
        return render_template("separar.html", error=str(e))

# ─── Ruta experimental ───────────────────────────────────────
@app.get("/experimental")
def experimental():
    mem_session   = session.get("mem_session")
    stems_present = session.get("stems_present") or []
    return render_template("experimental.html",
                           mem_session=mem_session,
                           stems_present=stems_present,
                           error=None)

# ─── Servir archivos estáticos de stems ───────────────────────────────────────
@app.route('/media/stems/<path:subpath>')
def serve_stems(subpath):
    return send_from_directory(STEMS_DIR, subpath, as_attachment=False)

@app.route('/media/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOADS_DIR, filename, as_attachment=False)

@app.route('/mem_stem/<sid>/<name>.wav')
def mem_stem(sid, name):
    _purge_mem()
    name = name.lower()
    with MEM_LOCK:
        pack = MEM_STEMS.get(sid)
        if not pack:
            abort(404)
        data = pack["stems"].get(name)
        if not data:
            abort(404)
    return send_file(io.BytesIO(data), mimetype='audio/wav', as_attachment=False, download_name=f"{name}.wav")


# ─── 3) STREAM del MIX “estilo Moises” ───────────────────────────────────────
@app.route("/stream_mix")
def stream_mix():
    """
    Stream del TEMA COMPLETO mezclado en vivo (un solo audio), con volúmenes/mutes por instrumento.
    Query ej:
      /stream_mix?folder=stems_ab12cd34&vocals=100&guitar=0&bass=80&drums=100&piano=0&other=100&t=123
    Acepta 0..1 ó 0..100 (porcentaje). 't' = segundos para mantener la posición al mover sliders.
    """
    folder = (request.args.get("folder") or "").strip()
    if not folder:
        return "Falta 'folder'", 400

    stem_dir = STEMS_DIR / folder
    if not stem_dir.exists():
        return "Carpeta inexistente", 404

    # segundos desde inicio (para no volver al comienzo al refrescar mezcla)
    try:
        start_t = float(request.args.get("t") or 0.0)
        if start_t < 0:
            start_t = 0.0
    except:
        start_t = 0.0

    # gains saneados por instrumento
    gains = {name: sanitize_gain(request.args.get(name)) for name in STEM_ORDER}
    inputs, filter_complex, n = build_mix_filter_and_inputs(stem_dir, gains)

    if n == 0:
        # stream de 2s de silencio (sin cache)
        def silence_stream():
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                   "-t", "2", "-f", "wav", "-"]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            try:
                for chunk in iter(lambda: p.stdout.read(4096), b""):
                    yield chunk
            finally:
                p.kill()
        return Response(stream_with_context(silence_stream()),
                        mimetype="audio/wav")

    def generate():
        # Insertamos -ss <t> ANTES de cada -i (seeking por input) para mantener posición
        seeked_inputs = []
        it = iter(inputs)  # inputs = ['-i', path, '-i', path, ...]
        for flag, val in zip(it, it):
            seeked_inputs.extend(['-ss', str(start_t), flag, val])

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *seeked_inputs,
            "-filter_complex", filter_complex,
            "-map", "[mix]",
            "-f", "wav", "-"  # stream WAV
        ]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        try:
            for chunk in iter(lambda: p.stdout.read(4096), b""):
                yield chunk
        finally:
            p.kill()

    headers = {"Cache-Control": "no-store, max-age=0"}
    return Response(stream_with_context(generate()),
                    mimetype="audio/wav",
                    headers=headers)


# ─── 4) EXPORTAR mezcla a archivo (descargable) ──────────────────────────────
@app.route('/mix', methods=['POST'])
def mix_stems():
    """
    Recibe JSON:
      { "folder": "stems_xxxx", "gains": { "vocals":0..1, "guitar":0..1, ... } }
    Devuelve JSON con url y filename.
    """
    data = request.get_json(silent=True) or {}
    folder = (data.get('folder') or "").strip()
    gains = data.get('gains') or {}

    if not folder or not isinstance(gains, dict):
        return jsonify({"error": "Parámetros inválidos"}), 400

    stem_dir = STEMS_DIR / folder
    if not stem_dir.exists():
        return jsonify({"error": "Carpeta no encontrada"}), 404

    # Sanitizamos
    gains = {k: sanitize_gain(v) for k, v in gains.items()}

    inputs, filter_complex, n = build_mix_filter_and_inputs(stem_dir, gains)
    if n == 0:
        return jsonify({"error": "Todos los instrumentos muteados"}), 400

    mix_out = stem_dir / f"mix_{uuid.uuid4().hex[:6]}.wav"
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        *inputs,
        '-filter_complex', filter_complex,
        '-map', '[mix]',
        '-c:a', 'pcm_s16le',
        str(mix_out)
    ]

    try:
        subprocess.check_call(cmd)
    except Exception as e:
        return jsonify({"error": f"ffmpeg: {e}"}), 500

    return jsonify({
        "url": url_for('serve_stems', subpath=f"{folder}/{mix_out.name}"),
        "filename": mix_out.name
    })

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # host="0.0.0.0" si lo corrés en un server público
    app.run(debug=True)
