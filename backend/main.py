import os
import uuid
import glob
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from yt_dlp import YoutubeDL

app = FastAPI(title="Media Downloader API")

# Allow browser requests from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Common yt-dlp options applied to every request
BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    },
}


def _find_downloaded_file(file_id: str) -> str:
    """Find the actual downloaded file by scanning the downloads directory."""
    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*"))
    if not matches:
        raise FileNotFoundError(f"No downloaded file found for id {file_id}")
    # Prefer mp4 > webm > mkv, otherwise take the first match
    priority = [".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus"]
    matches.sort(key=lambda p: next(
        (i for i, ext in enumerate(priority) if p.endswith(ext)), 999
    ))
    return matches[0]


@app.get("/info")
def get_info(url: str):
    """Return basic video info (title, thumbnail, duration) without listing all formats."""
    try:
        opts = {**BASE_OPTS, "extract_flat": False}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/formats")
def get_formats(url: str):
    """Return available video formats for a URL."""
    try:
        with YoutubeDL(BASE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = []
            seen = set()
            for f in info.get("formats", []):
                resolution = f.get("resolution") or (
                    f"{f['width']}x{f['height']}" if f.get("width") and f.get("height") else None
                )
                if not resolution or resolution == "audio only":
                    continue
                key = (resolution, f.get("ext"))
                if key in seen:
                    continue
                seen.add(key)
                formats.append({
                    "format_id": f["format_id"],
                    "ext": f.get("ext"),
                    "resolution": resolution,
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                })
            # Sort by resolution (higher = better)
            def res_sort(fmt):
                try:
                    w, h = fmt["resolution"].split("x")
                    return int(w) * int(h)
                except Exception:
                    return 0
            formats.sort(key=res_sort, reverse=True)
            return {
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "formats": formats,
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/download")
def download(url: str, format_id: str = None, audio_only: bool = False):
    """Download a video and return it as a file response."""
    try:
        file_id = str(uuid.uuid4())
        output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

        ydl_opts = {
            **BASE_OPTS,
            "quiet": False,   # allow progress output in container logs
            "outtmpl": output_template,
        }

        if audio_only:
            # Try to extract audio with ffmpeg; if unavailable fall back to best audio-only stream
            ydl_opts.update({
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "ignoreerrors": False,
            })
        elif format_id:
            # Try to merge selected format with best audio; fall back to the format alone if ffmpeg missing
            ydl_opts.update({
                "format": f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}",
                "merge_output_format": "mp4",
            })
        else:
            # Prefer pre-muxed mp4 first so ffmpeg is not required, then allow merge
            ydl_opts.update({
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
                "merge_output_format": "mp4",
            })

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_title = info.get("title", "video")

        # Find the actual file (post-processors may change extension)
        filepath = _find_downloaded_file(file_id)
        ext = Path(filepath).suffix
        safe_title = "".join(c for c in video_title if c.isalnum() or c in " -_()").strip()
        download_name = f"{safe_title[:80]}{ext}" if safe_title else f"download{ext}"

        return FileResponse(
            filepath,
            media_type="application/octet-stream",
            filename=download_name,
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# Serve the frontend LAST so API routes take priority.
# Resolve paths from __file__ so they work regardless of the CWD uvicorn is started from.
_here = Path(__file__).resolve().parent          # .../backend/
_frontend_candidates = [
    _here / "frontend",        # Docker: /app/frontend/
    _here.parent / "frontend", # Local dev: <repo>/frontend/
]
frontend_path = next((p for p in _frontend_candidates if p.is_dir()), None)

if frontend_path:
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return {"status": "API running", "hint": "frontend not found — place it at frontend/ next to backend/"}