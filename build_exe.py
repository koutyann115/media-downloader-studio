import os
import shutil
import subprocess
from pathlib import Path
import imageio_ffmpeg

BASE_DIR = Path(__file__).resolve().parent
FFMPEG_SRC = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_LOCAL = BASE_DIR / "ffmpeg.exe"

print(f"Copying FFmpeg from {FFMPEG_SRC} -> {FFMPEG_LOCAL}")
shutil.copy2(FFMPEG_SRC, FFMPEG_LOCAL)

ICON_FILE = BASE_DIR / "static" / "icon.ico"

cmd = [
    "python", "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "MediaDownloaderStudio",
    "--icon", str(ICON_FILE),
    "--add-data", f"static{os.pathsep}static",
    "--add-binary", f"ffmpeg.exe{os.pathsep}.",
    "--collect-all", "yt_dlp",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols",
    "--hidden-import", "uvicorn.protocols.http",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.websockets",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan",
    "--hidden-import", "uvicorn.lifespan.on",
    "app.py"
]

print("Running PyInstaller...")
subprocess.run(cmd, cwd=str(BASE_DIR), check=True)

dist_exe = BASE_DIR / "dist" / "MediaDownloaderStudio.exe"
root_exe = BASE_DIR / "MediaDownloaderStudio.exe"
if dist_exe.exists():
    shutil.copy2(dist_exe, root_exe)
    print(f"Successfully built standalone executable: {root_exe}")

# Clean up intermediate build files and duplicate dist/ binary
for p in [BASE_DIR / "build", BASE_DIR / "dist", FFMPEG_LOCAL]:
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        try:
            p.unlink()
        except OSError:
            pass
print("Cleaned up temporary build artifacts.")
