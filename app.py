import os
import re
import io
import sys
import json
import time
import uuid
import queue
import socket
import shutil
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

# When built with PyInstaller --windowed, sys.stdout and sys.stderr are None.
# Uvicorn and yt-dlp check .isatty() or write to them, so provide dummy streams.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

# Path resolution for both normal Python execution and PyInstaller bundle
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MediaDownloaderStudio"
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DATA_DIR = BUNDLE_DIR

os.makedirs(APP_DATA_DIR, exist_ok=True)
STATIC_DIR = BUNDLE_DIR / "static"
HISTORY_FILE = APP_DATA_DIR / "history.json"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
DEFAULT_SAVE_DIR = str(Path.home() / "Downloads" / "MediaDownloader")
os.makedirs(DEFAULT_SAVE_DIR, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = 2
KNOWN_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".aac"}


class DownloadCancelled(Exception):
    pass


# Locate FFmpeg (bundled or imageio_ffmpeg or system PATH)
def resolve_ffmpeg() -> Optional[str]:
    bundled_ffmpeg = BUNDLE_DIR / "ffmpeg.exe"
    if bundled_ffmpeg.exists():
        return str(bundled_ffmpeg)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


FFMPEG_EXE = resolve_ffmpeg()

# Prevent subprocess console windows from flashing on Windows when frozen (without affecting explorer.exe)
if os.name == "nt":
    _orig_popen = subprocess.Popen

    class _NoWindowPopen(_orig_popen):
        def __init__(self, args, *pargs, **kwargs):
            if getattr(sys, "frozen", False) and "creationflags" not in kwargs:
                cmd0 = ""
                if isinstance(args, (list, tuple)) and len(args) > 0:
                    cmd0 = os.path.basename(str(args[0])).lower()
                elif isinstance(args, str):
                    cmd0 = os.path.basename(args.split()[0]).lower() if args.strip() else ""
                if cmd0 not in ("explorer", "explorer.exe"):
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(args, *pargs, **kwargs)

    subprocess.Popen = _NoWindowPopen

app = FastAPI(title="Media Downloader Studio")

# Global references for desktop window & clean server shutdown
webview_window = None
uvicorn_server = None
app_shutting_down = threading.Event()

# Global state for tasks, history, settings, and active subprocesses
tasks_lock = threading.Lock()
tasks: Dict[str, Dict[str, Any]] = {}
cancel_flags: Dict[str, bool] = {}
active_subprocesses: Dict[str, subprocess.Popen] = {}
download_queue: "queue.Queue[tuple[str, DownloadItemRequest]]" = queue.Queue()


def load_settings() -> Dict[str, Any]:
    defaults = {"save_dir": DEFAULT_SAVE_DIR}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    defaults.update(data)
        except Exception:
            pass
    return defaults


def save_settings(new_settings: Dict[str, Any]):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


settings_data: Dict[str, Any] = load_settings()


def load_history() -> List[Dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_history_locked():
    # Caller must hold tasks_lock
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data[:200], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


history_data: List[Dict[str, Any]] = load_history()


def parse_time_to_seconds(time_str: Optional[str]) -> Optional[float]:
    if not time_str or not time_str.strip():
        return None
    s = time_str.strip()
    parts = s.split(":")
    try:
        if len(parts) == 1:
            val = float(parts[0])
        elif len(parts) == 2:
            val = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            val = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            return None
        return val if val >= 0 else None
    except ValueError:
        return None


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def format_bytes(size: Optional[float]) -> str:
    if not size or size <= 0:
        return "-"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def sanitize_custom_filename(raw_name: str) -> str:
    name = raw_name.strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() in KNOWN_MEDIA_EXTS and stem.strip():
        name = stem.strip()
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def is_cancelled(task_id: str) -> bool:
    return app_shutting_down.is_set() or cancel_flags.get(task_id, False)


def terminate_task_subprocess(task_id: str):
    proc = None
    with tasks_lock:
        proc = active_subprocesses.get(task_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass


class InfoRequest(BaseModel):
    url: str


class FolderPickRequest(BaseModel):
    current_dir: Optional[str] = None


class OpenFolderRequest(BaseModel):
    path: str


class SettingsUpdateRequest(BaseModel):
    save_dir: Optional[str] = None


class DownloadItemRequest(BaseModel):
    url: str
    save_dir: str = DEFAULT_SAVE_DIR
    media_type: str = "video"      # "video" or "audio"
    container: str = "mp4"         # mp4, webm, mkv / mp3, m4a, wav, flac, opus
    quality: str = "best"          # "best", "2160", "1440", "1080", "720", "480", "360" or audio bitrate "320", "192", "128"
    start_time: Optional[str] = ""
    end_time: Optional[str] = ""
    custom_filename: Optional[str] = ""
    embed_thumbnail: bool = False
    embed_subs: bool = False
    title_hint: Optional[str] = ""
    thumbnail_hint: Optional[str] = ""


class BatchDownloadRequest(BaseModel):
    items: List[DownloadItemRequest]


@app.get("/api/config")
def get_config():
    with tasks_lock:
        saved_dir = settings_data.get("save_dir") or DEFAULT_SAVE_DIR
    return {
        "default_save_dir": saved_dir,
        "ffmpeg_available": FFMPEG_EXE is not None,
        "ffmpeg_path": FFMPEG_EXE,
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
    }


@app.post("/api/settings")
def update_settings(req: SettingsUpdateRequest):
    with tasks_lock:
        if req.save_dir and req.save_dir.strip():
            settings_data["save_dir"] = os.path.normpath(req.save_dir.strip())
            save_settings(settings_data)
        return dict(settings_data)


@app.post("/api/pick-folder")
def pick_folder(req: FolderPickRequest):
    with tasks_lock:
        fallback_dir = settings_data.get("save_dir") or DEFAULT_SAVE_DIR
    initial = req.current_dir if req.current_dir and os.path.isdir(req.current_dir) else fallback_dir
    if not os.path.isdir(initial):
        initial = DEFAULT_SAVE_DIR

    # 1. Try native pywebview folder dialog if running inside desktop window
    global webview_window
    if webview_window is not None:
        try:
            import webview
            dialog_type = getattr(webview, "FileDialog", None)
            folder_mode = dialog_type.FOLDER if dialog_type else webview.FOLDER_DIALOG
            result = webview_window.create_file_dialog(
                folder_mode,
                directory=initial
            )
            if result and len(result) > 0:
                chosen = os.path.normpath(result[0])
                with tasks_lock:
                    settings_data["save_dir"] = chosen
                    save_settings(settings_data)
                return {"selected": True, "path": chosen}
            return {"selected": False, "path": initial}
        except Exception:
            pass

    # 2. Fallback to Tkinter dialog
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=initial, title="保存先フォルダを選択")
        root.destroy()
        if chosen:
            chosen = os.path.normpath(chosen)
            with tasks_lock:
                settings_data["save_dir"] = chosen
                save_settings(settings_data)
            return {"selected": True, "path": chosen}
        return {"selected": False, "path": initial}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"フォルダ選択ダイアログを開けませんでした: {e}")


def reveal_in_explorer(target: str):
    """Open Windows Explorer and select the file (non-blocking, safe with spaces/brackets/Unicode)."""
    target = os.path.abspath(os.path.normpath(target))
    if os.path.isfile(target):
        if os.name == "nt":
            # Pass a raw command string so quotes wrap ONLY the file path (not /select,)
            # and bypass CREATE_NO_WINDOW explicitly.
            cmd = f'explorer.exe /select,"{target}"'
            _orig_popen(cmd)
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target)])
    else:
        if os.name == "nt":
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", target])


@app.post("/api/open-folder")
def open_folder(req: OpenFolderRequest):
    target = os.path.normpath(req.path)
    if not os.path.exists(target):
        parent = os.path.dirname(target)
        if os.path.exists(parent):
            target = parent
        else:
            raise HTTPException(status_code=404, detail="指定されたパスが見つかりません")

    try:
        reveal_in_explorer(target)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/info")
def fetch_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URLを入力してください")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    if FFMPEG_EXE:
        ydl_opts["ffmpeg_location"] = FFMPEG_EXE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"動画情報の取得に失敗しました: {strip_ansi(str(e))}")

    if not info:
        raise HTTPException(status_code=404, detail="動画情報が見つかりませんでした")

    if "entries" in info and info["entries"]:
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]

    formats = info.get("formats") or []
    resolutions = set()
    for f in formats:
        height = f.get("height")
        vcodec = f.get("vcodec")
        if height and vcodec and vcodec != "none":
            resolutions.add(height)

    sorted_res = sorted(list(resolutions), reverse=True)

    return {
        "title": info.get("title") or "無題のメディア",
        "uploader": info.get("uploader") or info.get("channel") or info.get("extractor_key") or "Unknown",
        "duration": info.get("duration"),
        "duration_str": format_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or "Generic",
        "available_heights": sorted_res,
        "view_count": info.get("view_count"),
    }


def trim_media_with_ffmpeg(
    task_id: str,
    input_path: str,
    start_sec: Optional[float],
    end_sec: Optional[float],
    total_duration: Optional[float],
) -> str:
    """Fast local trim using FFmpeg stream copy with real-time progress and cancel support."""
    if not FFMPEG_EXE or not os.path.isfile(input_path):
        return input_path

    s_val = start_sec if start_sec is not None else 0.0
    clip_duration: Optional[float] = None
    if end_sec is not None:
        clip_duration = max(0.1, end_sec - s_val)
    elif total_duration and total_duration > s_val:
        clip_duration = max(0.1, total_duration - s_val)

    base, ext = os.path.splitext(input_path)
    tmp_output = f"{base}.__trim_tmp__{ext}"

    def run_ffmpeg_cmd(copy_codec: bool) -> int:
        cmd = [FFMPEG_EXE, "-y", "-hide_banner", "-loglevel", "error"]
        if s_val > 0:
            cmd += ["-ss", f"{s_val:.3f}"]
        cmd += ["-i", input_path]
        if end_sec is not None:
            cmd += ["-t", f"{clip_duration:.3f}"]
        if copy_codec:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        cmd += ["-progress", "pipe:1", "-nostats", tmp_output]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with tasks_lock:
            active_subprocesses[task_id] = proc

        try:
            if proc.stdout:
                for line in proc.stdout:
                    if is_cancelled(task_id):
                        proc.terminate()
                        raise DownloadCancelled("ユーザーによりキャンセルされました")
                    line = line.strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        val_str = line.split("=", 1)[1].strip()
                        if val_str.isdigit() and clip_duration and clip_duration > 0:
                            out_sec = int(val_str) / 1_000_000.0
                            ratio = min(1.0, max(0.0, out_sec / clip_duration))
                            pct = round(85.0 + ratio * 14.5, 1)
                            with tasks_lock:
                                if task_id in tasks:
                                    tasks[task_id].update({
                                        "status": "processing",
                                        "progress": max(tasks[task_id]["progress"], pct),
                                        "speed_str": "-",
                                        "eta_str": "切り抜き中",
                                        "message": f"指定範囲を高速切り抜き中... ({int(ratio * 100)}%)",
                                    })
            proc.wait()
            return proc.returncode
        finally:
            with tasks_lock:
                active_subprocesses.pop(task_id, None)

    try:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "processing",
                    "progress": max(tasks[task_id]["progress"], 85.0),
                    "speed_str": "-",
                    "eta_str": "処理中",
                    "message": "指定範囲を高速切り抜き中...",
                })

        rc = run_ffmpeg_cmd(copy_codec=True)
        if is_cancelled(task_id):
            raise DownloadCancelled("ユーザーによりキャンセルされました")

        if rc != 0 or not os.path.isfile(tmp_output) or os.path.getsize(tmp_output) == 0:
            # Fallback to re-encode if stream copy fails for an unusual codec/container pair
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]["message"] = "互換モードで切り抜き中..."
            rc = run_ffmpeg_cmd(copy_codec=False)
            if is_cancelled(task_id):
                raise DownloadCancelled("ユーザーによりキャンセルされました")
            if rc != 0 or not os.path.isfile(tmp_output):
                raise RuntimeError("指定範囲の切り抜きに失敗しました")

        os.replace(tmp_output, input_path)
        return input_path
    finally:
        if os.path.exists(tmp_output):
            try:
                os.remove(tmp_output)
            except OSError:
                pass


def run_download_worker(task_id: str, req: DownloadItemRequest):
    with tasks_lock:
        if task_id not in tasks:
            return
        if is_cancelled(task_id):
            tasks[task_id].update({
                "status": "cancelled",
                "message": "キャンセルされました",
                "speed_str": "-",
                "eta_str": "-",
            })
            return

    save_dir = os.path.normpath(req.save_dir.strip() or DEFAULT_SAVE_DIR)
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception as e:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "error",
                    "message": f"エラー: 保存先フォルダを作成できません ({e})",
                    "speed_str": "-",
                    "eta_str": "-",
                })
        return

    start_sec = parse_time_to_seconds(req.start_time)
    end_sec = parse_time_to_seconds(req.end_time)

    # Validate time range inputs
    if req.start_time and req.start_time.strip() and start_sec is None:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "error",
                    "message": "エラー: 開始時間の形式が正しくありません（例: 01:20）",
                    "speed_str": "-",
                    "eta_str": "-",
                })
        return

    if req.end_time and req.end_time.strip() and end_sec is None:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "error",
                    "message": "エラー: 終了時間の形式が正しくありません（例: 03:45）",
                    "speed_str": "-",
                    "eta_str": "-",
                })
        return

    if start_sec is not None and end_sec is not None and end_sec <= start_sec:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "error",
                    "message": "エラー: 終了時間は開始時間より後に設定してください",
                    "speed_str": "-",
                    "eta_str": "-",
                })
        return

    needs_clip = (start_sec is not None) or (end_sec is not None)
    dl_ceiling = 84.0 if needs_clip else 94.0

    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update({
                "status": "fetching_info",
                "message": "メディア情報を解析中...",
                "speed_str": "解析中",
            })

    stream_state = {
        "finished_count": 0,
        "max_progress": 0.0,
        "final_filepath": None,
    }

    def progress_hook(d: Dict[str, Any]):
        if is_cancelled(task_id):
            raise DownloadCancelled("ユーザーによりキャンセルされました")

        status = d.get("status")
        info_d = d.get("info_dict") or {}
        vcodec = info_d.get("vcodec")
        acodec = info_d.get("acodec")
        is_video_only = bool(vcodec and vcodec != "none" and (not acodec or acodec == "none"))
        is_audio_only = bool(acodec and acodec != "none" and (not vcodec or vcodec == "none"))

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed")
            eta = d.get("eta")

            raw_pct = 0.0
            if total > 0:
                raw_pct = min(100.0, (downloaded / total) * 100.0)
            else:
                p_str = strip_ansi(d.get("_percent_str", "")).replace("%", "").strip()
                try:
                    raw_pct = min(100.0, max(0.0, float(p_str)))
                except ValueError:
                    pass

            # Map multi-stream progress smoothly without jumping backwards
            if req.media_type == "video" and is_video_only:
                overall = (raw_pct / 100.0) * (dl_ceiling * 0.82)
                phase_label = "映像ダウンロード中 (1/2)"
            elif req.media_type == "video" and is_audio_only and stream_state["finished_count"] >= 1:
                base_p = dl_ceiling * 0.82
                overall = base_p + (raw_pct / 100.0) * (dl_ceiling - base_p)
                phase_label = "音声ダウンロード中 (2/2)"
            else:
                overall = (raw_pct / 100.0) * dl_ceiling
                phase_label = "ダウンロード中"

            overall = round(max(stream_state["max_progress"], min(dl_ceiling, overall)), 1)
            stream_state["max_progress"] = overall

            speed_str = f"{format_bytes(speed)}/s" if speed else "計算中..."
            eta_str = format_duration(eta) if eta is not None else "--:--"

            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id].update({
                        "status": "downloading",
                        "progress": overall,
                        "downloaded_str": format_bytes(downloaded),
                        "total_str": format_bytes(total) if total else "不明",
                        "speed_str": speed_str,
                        "eta_str": eta_str,
                        "message": f"{phase_label}... ({raw_pct:.1f}%)",
                    })

        elif status == "finished":
            stream_state["finished_count"] += 1
            if d.get("filename"):
                stream_state["final_filepath"] = d.get("filename")
            with tasks_lock:
                if task_id in tasks:
                    if req.media_type == "video" and is_video_only:
                        msg = "映像の取得完了・音声を取得中..."
                        next_p = max(stream_state["max_progress"], round(dl_ceiling * 0.82, 1))
                    else:
                        msg = "音声・映像の結合および形式変換中..."
                        next_p = max(stream_state["max_progress"], round(dl_ceiling, 1))
                    stream_state["max_progress"] = next_p
                    tasks[task_id].update({
                        "status": "processing",
                        "progress": next_p,
                        "speed_str": "-",
                        "eta_str": "処理中",
                        "message": msg,
                    })

    def postprocessor_hook(d: Dict[str, Any]):
        if is_cancelled(task_id):
            raise DownloadCancelled("ユーザーによりキャンセルされました")
        info_d = d.get("info_dict") or {}
        if d.get("status") == "finished" and info_d.get("filepath"):
            stream_state["final_filepath"] = info_d["filepath"]

    if req.custom_filename and req.custom_filename.strip():
        safe_name = sanitize_custom_filename(req.custom_filename)
        outtmpl = os.path.join(save_dir, f"{safe_name}.%(ext)s")
    else:
        outtmpl = os.path.join(save_dir, "%(title).120B [%(id)s].%(ext)s")

    ydl_opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }

    if FFMPEG_EXE:
        ydl_opts["ffmpeg_location"] = FFMPEG_EXE

    postprocessors = []

    if req.media_type == "audio":
        ydl_opts["format"] = "bestaudio/best"
        audio_codec = req.container if req.container in ["mp3", "m4a", "wav", "flac", "opus"] else "mp3"
        bitrate = req.quality if req.quality in ["320", "256", "192", "128"] else "192"
        pp_audio = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_codec,
        }
        if audio_codec in ["mp3", "m4a", "opus"]:
            pp_audio["preferredquality"] = bitrate
        postprocessors.append(pp_audio)
    else:
        container = req.container if req.container in ["mp4", "mkv", "webm"] else "mp4"
        q = req.quality
        if q and q != "best" and q.isdigit():
            h = int(q)
            if container == "mp4":
                ydl_opts["format"] = f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            else:
                ydl_opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        else:
            if container == "mp4":
                ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            else:
                ydl_opts["format"] = "bestvideo+bestaudio/best"

        ydl_opts["merge_output_format"] = container

    if needs_clip:
        # Always fetch a fresh source file when clipping so existing trimmed files are never double-cut
        ydl_opts["overwrites"] = True

    if req.embed_subs and req.media_type == "video":
        ydl_opts["writesubtitles"] = True
        ydl_opts["subtitleslangs"] = ["ja", "en", "all"]
        postprocessors.append({"key": "FFmpegEmbedSubtitle"})

    can_embed_thumb = not (req.media_type == "audio" and req.container == "wav")
    if req.embed_thumbnail and can_embed_thumb:
        ydl_opts["writethumbnail"] = True
        postprocessors.append({"key": "EmbedThumbnail"})

    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url.strip(), download=True)
            if is_cancelled(task_id):
                raise DownloadCancelled("ユーザーによりキャンセルされました")

            if "entries" in info and info["entries"]:
                entries = [e for e in info["entries"] if e]
                if entries:
                    info = entries[0]

            title = info.get("title") or req.title_hint or "メディアファイル"
            thumbnail = info.get("thumbnail") or req.thumbnail_hint or ""
            extractor = info.get("extractor_key") or "Media"
            total_duration = info.get("duration")

            # Resolve exact saved path accurately from yt-dlp metadata/hooks
            final_path = None
            req_dls = info.get("requested_downloads") or []
            if req_dls and isinstance(req_dls[0], dict) and req_dls[0].get("filepath"):
                if os.path.exists(req_dls[0]["filepath"]):
                    final_path = req_dls[0]["filepath"]

            if not final_path and stream_state["final_filepath"] and os.path.exists(stream_state["final_filepath"]):
                final_path = stream_state["final_filepath"]

            if not final_path:
                prepared_path = ydl.prepare_filename(info)
                base_no_ext = os.path.splitext(prepared_path)[0]
                candidate_path = f"{base_no_ext}.{req.container}"
                if os.path.exists(candidate_path):
                    final_path = candidate_path
                elif os.path.exists(prepared_path):
                    final_path = prepared_path
                else:
                    final_path = save_dir

            # Perform fast local FFmpeg clipping if start_time / end_time was specified
            if needs_clip and os.path.isfile(final_path):
                final_path = trim_media_with_ffmpeg(
                    task_id=task_id,
                    input_path=final_path,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    total_duration=total_duration,
                )

            file_size_str = "-"
            if os.path.isfile(final_path):
                file_size_str = format_bytes(os.path.getsize(final_path))

            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id].update({
                        "title": title,
                        "thumbnail": thumbnail,
                        "extractor": extractor,
                        "status": "completed",
                        "progress": 100.0,
                        "speed_str": "-",
                        "eta_str": "完了",
                        "file_path": final_path,
                        "file_size_str": file_size_str,
                        "message": "保存完了",
                        "completed_at": completed_at,
                    })
                    hist_entry = dict(tasks[task_id])
                    history_data.insert(0, hist_entry)
                    save_history_locked()

    except DownloadCancelled:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "cancelled",
                    "message": "キャンセルされました",
                    "speed_str": "-",
                    "eta_str": "-",
                })
    except Exception as e:
        err_msg = strip_ansi(str(e))
        cancelled = is_cancelled(task_id)
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({
                    "status": "cancelled" if cancelled else "error",
                    "message": "キャンセルされました" if cancelled else f"エラー: {err_msg}",
                    "speed_str": "-",
                    "eta_str": "-",
                })


def queue_worker_loop():
    while not app_shutting_down.is_set():
        try:
            task_id, item = download_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            run_download_worker(task_id, item)
        except Exception as e:
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id].update({
                        "status": "error",
                        "message": f"予期しないエラー: {strip_ansi(str(e))}",
                        "speed_str": "-",
                        "eta_str": "-",
                    })
        finally:
            download_queue.task_done()


# Start bounded daemon worker pool (2 concurrent downloads max)
for _ in range(MAX_CONCURRENT_DOWNLOADS):
    threading.Thread(target=queue_worker_loop, daemon=True).start()


@app.post("/api/download")
def start_downloads(batch: BatchDownloadRequest):
    if not batch.items:
        raise HTTPException(status_code=400, detail="ダウンロード対象がありません")

    created_ids = []
    first_save_dir = None

    for item in batch.items:
        url = item.url.strip()
        if not url:
            continue

        task_id = str(uuid.uuid4())
        save_dir = os.path.normpath(item.save_dir.strip() or DEFAULT_SAVE_DIR)
        if first_save_dir is None:
            first_save_dir = save_dir

        if item.media_type == "audio":
            if item.container in ("wav", "flac"):
                quality_label = "ロスレス"
            else:
                quality_label = f"{item.quality}kbps"
        else:
            quality_label = "最高画質" if item.quality == "best" else f"{item.quality}p"

        clip_label = ""
        if item.start_time or item.end_time:
            clip_label = f" [{item.start_time or '0:00'} ~ {item.end_time or '最後'}]"

        task_obj = {
            "id": task_id,
            "url": url,
            "title": item.title_hint or url,
            "thumbnail": item.thumbnail_hint or "",
            "extractor": "待機中",
            "media_type": item.media_type,
            "container": item.container.upper(),
            "quality_label": f"{item.container.upper()} / {quality_label}{clip_label}",
            "save_dir": save_dir,
            "file_path": save_dir,
            "file_size_str": "-",
            "status": "queued",
            "progress": 0.0,
            "downloaded_str": "0 B",
            "total_str": "-",
            "speed_str": "待機中",
            "eta_str": "--:--",
            "message": "キューで順番待ち中...",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with tasks_lock:
            tasks[task_id] = task_obj
            cancel_flags[task_id] = False

        download_queue.put((task_id, item))
        created_ids.append(task_id)

    if first_save_dir:
        with tasks_lock:
            if settings_data.get("save_dir") != first_save_dir:
                settings_data["save_dir"] = first_save_dir
                save_settings(settings_data)

    return {"started": created_ids}


@app.get("/api/tasks")
def list_tasks():
    with tasks_lock:
        active_list = [dict(t) for t in tasks.values()]
        hist_snapshot = [dict(h) for h in history_data[:50]]
    active_list.reverse()
    return {
        "tasks": active_list,
        "history": hist_snapshot,
    }


@app.post("/api/cancel/{task_id}")
def cancel_task(task_id: str):
    with tasks_lock:
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="タスクが見つかりません")
        cancel_flags[task_id] = True
        if tasks[task_id]["status"] == "queued":
            tasks[task_id]["status"] = "cancelled"
            tasks[task_id]["message"] = "キャンセルされました"
        else:
            tasks[task_id]["message"] = "キャンセル処理中..."
    terminate_task_subprocess(task_id)
    return {"ok": True}


@app.delete("/api/tasks/clear-finished")
def clear_finished_tasks():
    with tasks_lock:
        to_remove = [tid for tid, t in tasks.items() if t["status"] in ("completed", "error", "cancelled")]
        for tid in to_remove:
            tasks.pop(tid, None)
            cancel_flags.pop(tid, None)
    return {"removed": len(to_remove)}


@app.delete("/api/history")
def clear_history():
    with tasks_lock:
        history_data.clear()
        save_history_locked()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_backend_server(port: int):
    global uvicorn_server
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    uvicorn_server = uvicorn.Server(config)
    uvicorn_server.run()


def graceful_shutdown(server_thread: threading.Thread):
    app_shutting_down.set()
    with tasks_lock:
        for tid in list(tasks.keys()):
            cancel_flags[tid] = True
        procs = list(active_subprocesses.values())
    for proc in procs:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    if uvicorn_server is not None:
        uvicorn_server.should_exit = True
    server_thread.join(timeout=3.0)


if __name__ == "__main__":
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"

    server_thread = threading.Thread(target=start_backend_server, args=(port,), daemon=True)
    server_thread.start()

    # Wait until FastAPI is ready (up to 15 seconds)
    for _ in range(150):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    try:
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MediaDownloaderStudio.Desktop.1")
            except Exception:
                pass

        import webview
        icon_file = STATIC_DIR / "icon.ico"
        webview_window = webview.create_window(
            title="Media Downloader Studio",
            url=url,
            width=1280,
            height=860,
            min_size=(900, 620),
            background_color="#0b0f19",
        )
        webview.start(icon=str(icon_file) if icon_file.exists() else None)
        # Graceful shutdown so PyInstaller cleans up %TEMP%/_MEI* and child processes terminate cleanly
        graceful_shutdown(server_thread)
    except Exception:
        # Fallback to browser if webview fails for any reason
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            graceful_shutdown(server_thread)
