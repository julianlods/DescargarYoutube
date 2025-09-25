from flask import Flask, render_template, request, send_file
import yt_dlp
import os
import subprocess
from datetime import datetime
from werkzeug.utils import secure_filename


app = Flask(__name__)

DOWNLOADS = "downloads"
os.makedirs(DOWNLOADS, exist_ok=True)

STEMS_ROOT = os.path.join(DOWNLOADS, "stems")
os.makedirs(STEMS_ROOT, exist_ok=True)

# Si ffmpeg no está en el PATH en Windows, pon aquí la ruta completa:
# EJEMPLO: FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
# Si ya está en PATH, deja FFMPEG_PATH = None
FFMPEG_PATH = None

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url")
        if not url:
            return render_template("index.html", error="Pegá una URL de YouTube")

        # obtenemos info del video para usar el título
        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return render_template("index.html", error=f"No pude extraer info: {e}")

        title = info.get("title", "video").replace("/", "-").replace("\\", "-")

        try:
            output_ext = request.form.getlist("output_ext")

            # MP4: forzar recodificación del audio a AAC durante el merge
            if "mp4" in output_ext:
                filename_mp4 = f"{title}.mp4"
                filepath_mp4 = os.path.join(DOWNLOADS, filename_mp4)

                ydl_opts_mp4 = {
                    "format": "bestvideo+bestaudio/best",
                    "merge_output_format": "mp4",
                    "outtmpl": filepath_mp4,
                    "quiet": True,
                    "prefer_ffmpeg": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegVideoConvertor",
                            "preferedformat": "mp4"
                        }
                    ],
                    # argumentos directos a ffmpeg → recodificamos audio
                    "postprocessor_args": [
                        "-c:v", "copy",   # video intacto
                        "-c:a", "aac",    # recodifica audio
                        "-b:a", "192k"    # bitrate
                    ],
                }

                if FFMPEG_PATH:
                    ydl_opts_mp4["ffmpeg_location"] = FFMPEG_PATH

                with yt_dlp.YoutubeDL(ydl_opts_mp4) as ydl:
                    ydl.download([url])

                if not os.path.exists(filepath_mp4):
                    return render_template("index.html", error=f"Falló la creación del MP4: no se encontró {filepath_mp4}")
                return send_file(filepath_mp4, as_attachment=True, download_name=filename_mp4)

            # WAV: extracción normal a WAV
            if "wav" in output_ext:
                filename_wav = f"{title}.wav"
                filepath_wav = os.path.join(DOWNLOADS, filename_wav)

                ydl_opts_wav = {
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(DOWNLOADS, f"{title}.%(ext)s"),
                    "quiet": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "wav",
                            "preferredquality": "192"
                        }
                    ]
                }
                if FFMPEG_PATH:
                    ydl_opts_wav["ffmpeg_location"] = FFMPEG_PATH

                with yt_dlp.YoutubeDL(ydl_opts_wav) as ydl:
                    ydl.download([url])

                if not os.path.exists(filepath_wav):
                    return render_template("index.html", error=f"Falló la creación del WAV: no se encontró {filepath_wav}")
                return send_file(filepath_wav, as_attachment=True, download_name=filename_wav)

            # si no marcó nada
            return render_template("index.html", error="Seleccioná al menos MP4 o WAV para descargar")

        except yt_dlp.utils.DownloadError as de:
            return render_template("index.html", error=f"Error de descarga: {de}")
        except Exception as e:
            return render_template("index.html", error=f"Error: {e}")

    return render_template("index.html")

@app.route("/separar", methods=["GET", "POST"])
def separar():
    if request.method == "GET":
        return render_template("separar.html")

    try:
        file = request.files.get("file") or request.files.get("audio")  # aceptar ambos nombres
        if not file:
            return render_template("separar.html", error="Subí un archivo WAV.")

        filename = secure_filename(file.filename or "audio.wav")
        if not filename.lower().endswith(".wav"):
            return render_template("separar.html", error="Solo se acepta .wav por ahora.")

        # job id y carpeta de trabajo
        job_id = datetime.now().strftime("job-%Y%m%d-%H%M%S")
        job_dir = os.path.join(DOWNLOADS, "stems", job_id)
        os.makedirs(job_dir, exist_ok=True)

        # guardar input con nombre seguro
        input_path = os.path.join(job_dir, filename)
        file.save(input_path)

        # ejecutar demucs con 5 stems (forzar CPU)
        cmd = [
            "demucs",
            "-n", "htdemucs_6s",
            "-d", "cpu",
            "-o", os.path.abspath(job_dir),
            os.path.abspath(input_path)
        ]
        subprocess.run(cmd, check=True)

        # ruta donde Demucs deja las pistas con este modelo:
        # job_dir/htdemucs_6s/<base_name>/*.wav
        base = os.path.splitext(os.path.basename(input_path))[0]
        demucs_out = os.path.join(job_dir, "htdemucs_6s", base)

        # nombres esperados (los que pediste explicitamente)
        expected = {
            "Voz": "vocals.wav",
            "Guitarra": "guitar.wav",
            "Bajo": "bass.wav",
            "Batería": "drums.wav",
            "Teclado": "piano.wav",
        }

        # agregar 'other' como respaldo si existe
        fallback = {"Otros": "other.wav"}

        stems = []
        # chequear expected
        for label, fname in expected.items():
            path = os.path.join(demucs_out, fname)
            if os.path.exists(path):
                stems.append({"label": label, "file": f"stems/{job_id}/htdemucs_6s/{base}/{fname}"})

        # si hay un "other.wav", lo sumamos
        for label, fname in fallback.items():
            path = os.path.join(demucs_out, fname)
            if os.path.exists(path):
                stems.append({"label": label, "file": f"stems/{job_id}/htdemucs_6s/{base}/{fname}"})

        return render_template(
            "separar.html",
            success=True,
            stems=stems,
            stems_dir=job_id,
            error=None
        )

    except subprocess.CalledProcessError as e:
        return render_template("separar.html", error=f"Falló al ejecutar Demucs: {e}")
    except Exception as e:
        return render_template("separar.html", error=f"Error: {e}")

@app.route("/stems/<path:subpath>")
def download_stem(subpath):
    # sirve archivos desde STEMS_ROOT de forma segura
    fullpath = os.path.join(STEMS_ROOT, subpath)
    if not os.path.abspath(fullpath).startswith(os.path.abspath(STEMS_ROOT)):
        return "Ruta inválida", 400
    if not os.path.exists(fullpath):
        return "Archivo no encontrado", 404
    return send_file(fullpath, as_attachment=True, download_name=os.path.basename(fullpath))


if __name__ == "__main__":
    app.run(debug=True)
