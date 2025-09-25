from flask import Flask, render_template, request, send_file
import yt_dlp
import os

app = Flask(__name__)

DOWNLOADS = "downloads"
os.makedirs(DOWNLOADS, exist_ok=True)

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


if __name__ == "__main__":
    app.run(debug=True)
