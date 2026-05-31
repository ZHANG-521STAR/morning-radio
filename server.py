"""
早安电台一体化服务器
- 静态页面服务
- POST /generate → 接收文案 → 生成视频
- GET /status → 查询生成状态
- GET /video → 获取视频 (支持 Range & ?name=)
- GET /history → 获取生成历史
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)
HISTORY_FILE = OUTPUT / "history.json"

_task_status = {"running": False, "progress": "", "output": "", "error": ""}
_current_proc = None
_cancel_requested = False


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(entry):
    hist = load_history()
    hist.insert(0, entry)
    # Keep last 50
    hist = hist[:50]
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_range(range_header, file_size):
    """Parse Range header, return (start, end) or None."""
    if not range_header:
        return None
    try:
        unit, ranges = range_header.split("=", 1)
        if unit != "bytes":
            return None
        start_str, end_str = ranges.split("-", 1)
        start = int(start_str) if start_str else 0
        if end_str:
            end = int(end_str)
        else:
            end = file_size - 1
        return (start, end)
    except Exception:
        return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        global _current_proc, _cancel_requested

        if self.path == "/cancel":
            _cancel_requested = True
            if _current_proc and _current_proc.poll() is None:
                _current_proc.terminate()
                try:
                    _current_proc.wait(timeout=5)
                except Exception:
                    _current_proc.kill()
            _task_status["running"] = False
            _task_status["progress"] = "已取消"
            _task_status["error"] = ""
            self._json(200, {"status": "cancelled"})

        elif self.path == "/generate":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(body)
                text = data.get("text", "").strip()
                output_name = data.get("name", "morning_radio")
            except Exception:
                text = body.strip()
                output_name = "morning_radio"

            if not text:
                self._json(400, {"error": "文本不能为空"})
                return

            if _task_status["running"]:
                self._json(409, {"error": "正在生成中，请稍后再试"})
                return

            _cancel_requested = False
            _task_status["running"] = True
            _task_status["progress"] = "开始生成..."
            _task_status["error"] = ""
            _task_status["output"] = ""

            script_path = ROOT / "current_script.txt"
            script_path.write_text(text, encoding="utf-8")

            start_time = time.time()

            def run_gen():
                global _current_proc, _cancel_requested
                proc = None
                try:
                    env = os.environ.copy()
                    env["IMAGEIO_FFMPEG_EXE"] = r"C:\Program Files\Python38\lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win64-v4.2.2.exe"
                    env["PYTHONUNBUFFERED"] = "1"
                    cmd = [
                        sys.executable, str(ROOT / "generate_video.py"),
                        "--file", str(script_path),
                        "--output", output_name,
                    ]
                    proc = subprocess.Popen(
                        cmd, cwd=str(ROOT), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                    )
                    _current_proc = proc
                    for line in proc.stdout:
                        if _cancel_requested:
                            proc.terminate()
                            break
                        _task_status["progress"] = line.strip()
                    proc.wait()
                    if _cancel_requested:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            proc.kill()
                        _task_status["progress"] = "已取消"
                        return
                    if proc.returncode == 0:
                        out_file = OUTPUT / f"{output_name}.mp4"
                        out_str = str(out_file) if out_file.exists() else ""
                        _task_status["output"] = out_str
                        _task_status["progress"] = "完成！"
                        save_history({
                            "name": output_name,
                            "output": out_str,
                            "text": text[:300] + ("..." if len(text) > 300 else ""),
                            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "duration": round(time.time() - start_time, 1),
                        })
                    else:
                        _task_status["error"] = f"生成失败 (exit {proc.returncode})"
                except Exception as e:
                    _task_status["error"] = str(e)
                finally:
                    _current_proc = None
                    _task_status["running"] = False

            threading.Thread(target=run_gen, daemon=True).start()
            self._json(200, {"status": "started", "message": "视频生成已启动"})
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/status":
            self._json(200, _task_status)

        elif path == "/history":
            self._json(200, load_history())

        elif path == "/video":
            self._serve_video(params)

        elif path == "/thumbnail":
            self._serve_thumbnail(params)

        else:
            super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _serve_video(self, params):
        # Determine which video to serve
        name = params.get("name", [None])[0]
        if name:
            video_path = OUTPUT / f"{name}.mp4"
            if not video_path.exists():
                video_path = OUTPUT / name
            if not video_path.exists():
                self._json(404, {"error": "video not found"})
                return
        else:
            videos = sorted(OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not videos:
                self._json(404, {"error": "no video yet"})
                return
            video_path = videos[0]

        file_size = video_path.stat().st_size
        range_header = self.headers.get("Range")
        range_parsed = _parse_range(range_header, file_size)

        if range_parsed:
            start, end = range_parsed
            if start >= file_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            end = min(end, file_size - 1)
            content_length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", content_length)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(video_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                chunk = 64 * 1024
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", file_size)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(video_path, "rb") as f:
                chunk = 64 * 1024
                while True:
                    data = f.read(chunk)
                    if not data:
                        break
                    self.wfile.write(data)

    def _serve_thumbnail(self, params):
        """Extract frame at 2s as JPEG thumbnail."""
        name = params.get("name", [None])[0]
        if not name:
            self._json(400, {"error": "name required"})
            return
        video_path = OUTPUT / f"{name}.mp4"
        if not video_path.exists():
            video_path = OUTPUT / name
        if not video_path.exists():
            self._json(404, {"error": "video not found"})
            return

        # Cache thumbnails
        thumb_dir = OUTPUT / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        thumb_path = thumb_dir / f"{video_path.stem}.jpg"

        if not thumb_path.exists():
            try:
                ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE", "ffmpeg")
                subprocess.run(
                    [ffmpeg, "-y", "-ss", "3", "-i", str(video_path),
                     "-vframes", "1", "-q:v", "5", str(thumb_path)],
                    capture_output=True, timeout=15,
                )
            except Exception:
                pass

        if thumb_path.exists():
            data = thumb_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"error": "thumbnail unavailable"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    port = 8080
    print(f"  早安电台服务器: http://localhost:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
