#!/usr/bin/env python3
"""Write $Photos.html into a preprocessed picture directory.

Reads meta/location.csv and embeds the items so the page can open from
File Explorer (file://) without a local web server.
"""

from __future__ import annotations

import argparse
import base64
import csv
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from extract_locations import CSV_NAME
from PIL import Image, ImageOps

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".webm"}
TEMPLATE_PATH = Path(__file__).resolve().parent / "viewer" / "template.html"
OUTPUT_NAME = "$Photos.html"
OPEN_PY_NAME = "$open_photos.py"
OPEN_CMD_NAME = "$Open-Photos.cmd"
THUMBS_JS_NAME = "$thumbs.js"
LEGACY_NAMES = ("Photos.html", "open_photos.py", "Open-Photos.cmd")
THUMB_DIR = Path("meta") / "thumbs"
THUMB_ZIP = Path("meta") / "thumbs.zip"
THUMB_SIZE = 480

OPEN_PY_SOURCE = r"""
import http.server
import socketserver
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import unquote

PORT = 8765
ROOT = Path(__file__).resolve().parent
THUMB_PREFIX = "/meta/thumbs/"
THUMB_ZIP = ROOT / "meta" / "thumbs.zip"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        if path.startswith(THUMB_PREFIX) and not path.endswith("/"):
            name = path[len(THUMB_PREFIX):]
            if THUMB_ZIP.is_file():
                try:
                    with zipfile.ZipFile(THUMB_ZIP) as archive:
                        data = archive.read(name)
                except KeyError:
                    data = None
                if data:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
        return super().do_GET()


with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
    url = f"http://127.0.0.1:{PORT}/$Photos.html"
    print(f"Serving {ROOT}")
    print(url)
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    httpd.serve_forever()
"""

OPEN_CMD_SOURCE = """\
@echo off
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python "$open_photos.py"
  goto :eof
)
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 "$open_photos.py"
  goto :eof
)
echo Python was not found. Open $Photos.html after running serve_viewer.py from the photo-v2 folder.
pause
"""


def load_rows(root: Path) -> list[dict[str, str]]:
    csv_path = root / CSV_NAME
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Missing {csv_path}. Run extract_locations.py on this directory first."
        )
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def item_from_row(root: Path, row: dict[str, str]) -> dict | None:
    rel = (row.get("path") or row.get("filename") or "").strip().replace("\\", "/")
    if not rel:
        return None
    path = root / rel
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    latitude = (row.get("latitude") or "").strip()
    longitude = (row.get("longitude") or "").strip()
    item = {
        "path": rel,
        "filename": row.get("filename") or path.name,
        "datetime": (row.get("datetime") or "").strip(),
        "kind": "video" if suffix in VIDEO_EXTENSIONS else "image",
        "latitude": None,
        "longitude": None,
    }
    try:
        if latitude and longitude:
            item["latitude"] = float(latitude)
            item["longitude"] = float(longitude)
    except ValueError:
        pass
    return item


def thumb_entry_name(rel: str) -> str:
    return Path(rel).with_suffix(".jpg").as_posix()


def zip_info_mtime(info: zipfile.ZipInfo) -> int:
    return int(datetime(*info.date_time).timestamp())


def load_thumb_zip(zip_path: Path) -> dict[str, tuple[int, bytes]]:
    entries: dict[str, tuple[int, bytes]] = {}
    if not zip_path.is_file():
        return entries
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            entries[info.filename.replace("\\", "/")] = (
                zip_info_mtime(info),
                archive.read(info),
            )
    return entries


def save_thumb_zip(zip_path: Path, entries: dict[str, tuple[int, bytes]]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (mtime, data) in sorted(entries.items()):
            stamp = datetime.fromtimestamp(max(mtime, 0)).timetuple()[:6]
            info = zipfile.ZipInfo(filename=name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    tmp_path.replace(zip_path)


def image_thumb_bytes(source: Path) -> bytes | None:
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((THUMB_SIZE, THUMB_SIZE))
            if image.mode != "RGB":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=80, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def video_thumb_bytes(source: Path) -> bytes | None:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    handle, temp_name = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    dest = Path(temp_name)
    commands = (
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-ss", "0.4", "-frames:v", "1",
            "-vf", f"scale={THUMB_SIZE}:-2", "-q:v", "4", str(dest),
        ],
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0", "-i", str(source), "-frames:v", "1",
            "-vf", f"scale={THUMB_SIZE}:-2", "-q:v", "4", str(dest),
        ],
    )
    try:
        for command in commands:
            dest.unlink(missing_ok=True)
            result = subprocess.run(command, capture_output=True, timeout=60)
            if result.returncode == 0 and dest.is_file() and dest.stat().st_size > 100:
                return dest.read_bytes()
        return None
    except Exception:
        return None
    finally:
        dest.unlink(missing_ok=True)


def make_thumb_bytes(root: Path, item: dict) -> bytes | None:
    source = root / item["path"]
    if item["kind"] == "video":
        return video_thumb_bytes(source)
    return image_thumb_bytes(source)


def build_items(root: Path) -> list[dict]:
    zip_path = root / THUMB_ZIP
    existing = load_thumb_zip(zip_path)
    kept: dict[str, tuple[int, bytes]] = {}
    items = []
    for row in load_rows(root):
        item = item_from_row(root, row)
        if item is None:
            continue
        source = root / item["path"]
        name = thumb_entry_name(item["path"])
        source_mtime = int(source.stat().st_mtime)
        cached = existing.get(name)
        if cached and cached[0] >= source_mtime and len(cached[1]) > 100:
            data = cached[1]
            mtime = cached[0]
        else:
            data = make_thumb_bytes(root, item)
            mtime = source_mtime
        if data:
            kept[name] = (mtime, data)
            item["thumb"] = f"{THUMB_DIR.as_posix()}/{name}"
        items.append(item)
    items.sort(key=lambda entry: (entry["datetime"] or "9999", entry["path"]))
    save_thumb_zip(zip_path, kept)
    write_thumbs_js(root, kept)
    loose = root / THUMB_DIR
    if loose.is_dir():
        shutil.rmtree(loose, ignore_errors=True)
    return items


def write_thumbs_js(root: Path, entries: dict[str, tuple[int, bytes]]) -> None:
    mapping = {
        name: base64.b64encode(data).decode("ascii")
        for name, (_mtime, data) in entries.items()
    }
    payload = json.dumps(mapping, separators=(",", ":"))
    (root / THUMBS_JS_NAME).write_text(
        "window.PHOTO_THUMB_DATA=" + payload + ";\n",
        encoding="ascii",
    )


def make_request_handler(root: Path):
    zip_path = root / THUMB_ZIP
    prefix = "/meta/thumbs/"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            path = unquote(self.path.split("?", 1)[0])
            if path.startswith(prefix) and not path.endswith("/") and zip_path.is_file():
                name = path[len(prefix):]
                try:
                    with zipfile.ZipFile(zip_path) as archive:
                        data = archive.read(name)
                except KeyError:
                    data = b""
                if data:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            return super().do_GET()

    return Handler


def render_html(items: list[dict]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")
    if "%%ITEMS%%" not in template:
        raise RuntimeError("viewer/template.html is missing the %%ITEMS%% placeholder")
    return template.replace("%%ITEMS%%", payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create $Photos.html in a picture directory so it can be opened "
            "from File Explorer. Uses meta/location.csv."
        )
    )
    parser.add_argument("directory", type=Path, help="Preprocessed picture directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.directory.expanduser()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    try:
        items = build_items(root)
        html = render_html(items)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output = root / OUTPUT_NAME
    output.write_text(html, encoding="utf-8")
    (root / OPEN_PY_NAME).write_text(OPEN_PY_SOURCE, encoding="utf-8")
    (root / OPEN_CMD_NAME).write_text(OPEN_CMD_SOURCE, encoding="utf-8")
    for leftover in LEGACY_NAMES:
        old = root / leftover
        if old.is_file():
            old.unlink()
    located = sum(1 for item in items if item["latitude"] is not None)
    thumbs = sum(1 for item in items if item.get("thumb"))
    print(f"Wrote {output}")
    print(f"Wrote {root / OPEN_CMD_NAME}")
    print(f"Wrote {root / THUMB_ZIP}")
    print(f"Wrote {root / THUMBS_JS_NAME}")
    print(f"{len(items)} item(s), {located} with location, {thumbs} thumbnail(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
