"""Download the Kokoro-82M voice model for ChessAI's read-aloud (about 90 MB, one time).

    .\.venv\Scripts\pip install -r requirements-tts.txt
    .\.venv\Scripts\python scripts\setup_tts.py            # int8 model, 88 MB (recommended)
    .\.venv\Scripts\python scripts\setup_tts.py --quality  # full model, 310 MB

Files go to DATA_DIR/tts (default: data/tts), where the backend looks for them.
Model: Kokoro-82M (Apache-2.0), ONNX export from github.com/thewh1teagle/kokoro-onnx.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {"voices": "voices-v1.0.bin", "int8": "kokoro-v1.0.int8.onnx", "full": "kokoro-v1.0.onnx"}


def download(name: str, target: Path) -> None:
    if target.is_file() and target.stat().st_size > 1_000_000:
        print(f"  {name}: already there")
        return
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"  {name}: downloading…", flush=True)

    def progress(blocks, size, total):
        if total > 0:
            done = min(100, blocks * size * 100 // total)
            print(f"\r  {name}: {done}%", end="", flush=True)
    urllib.request.urlretrieve(RELEASE + name, tmp, progress)
    tmp.replace(target)
    print(f"\r  {name}: done      ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quality", action="store_true", help="the full 310 MB model instead of int8")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.config import get_settings
    folder = Path(get_settings().data_dir) / "tts"
    folder.mkdir(parents=True, exist_ok=True)
    model = FILES["full" if args.quality else "int8"]
    print(f"Kokoro voice files → {folder}")
    download(FILES["voices"], folder / FILES["voices"])
    download(model, folder / model)
    if args.quality:
        print("Using the full model: set TTS_KOKORO_MODEL to", folder / model)
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        print("\nNow install the runtime:  pip install -r requirements-tts.txt")
    print("\nRestart the server; Settings → Read aloud → Voice shows the Kokoro voices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
