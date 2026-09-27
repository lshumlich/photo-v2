#!/usr/bin/env python3
"""Extract filename, capture date/time, and GPS location to meta/location.csv.

Walks a directory tree of pictures and videos, then creates or updates
<meta/location.csv> under the directory that was passed in.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

import rename_photos as media

try:
    import exifread
except ImportError:  # pragma: no cover
    exifread = None

try:
    from PIL import Image
    from PIL.ExifTags import IFD
except ImportError:  # pragma: no cover
    Image = None
    IFD = None


CSV_NAME = Path("meta") / "location.csv"
FIELDNAMES = ["path", "filename", "datetime", "latitude", "longitude"]
ISO6709 = re.compile(
    r"([+-]\d{1,3}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?"
)
MUTAGEN_LOCATION_KEYS = (
    "----:com.apple.quicktime:location.ISO6709",
    "----:com.apple.quicktime:location",
    "©xyz",
    "\xa9xyz",
)
MP4_LOCATION_CONTAINERS = {b"moov", b"trak", b"mdia", b"udta", b"meta"}
XYZ_ATOMS = {b"\xa9xyz", b"xyz "}
GPS_LAT_REF = 1
GPS_LATITUDE = 2
GPS_LON_REF = 3
GPS_LONGITUDE = 4


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_coord(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def parse_iso6709(raw: object) -> tuple[float, float] | None:
    text = media._as_datetime_text(raw).replace(" ", "")
    match = ISO6709.search(text)
    if not match:
        return None
    latitude = float(match.group(1))
    longitude = float(match.group(2))
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    return latitude, longitude


def _is_location_key(name: str) -> bool:
    lowered = name.lower().strip()
    return "iso6709" in lowered or lowered in {
        "location",
        "com.apple.quicktime.location",
        "©xyz",
        "\xa9xyz",
        "xyz",
    }


def _ratio_to_float(value: object) -> float:
    if hasattr(value, "num") and hasattr(value, "den"):
        return float(value.num) / float(value.den) if value.den else 0.0
    if isinstance(value, tuple) and len(value) == 2 and not isinstance(value[0], tuple):
        denom = float(value[1])
        return float(value[0]) / denom if denom else 0.0
    return float(value)


def dms_to_decimal(values: object, reference: object) -> float | None:
    if values is None:
        return None
    if hasattr(values, "values"):
        values = values.values
    parts = [_ratio_to_float(part) for part in values]
    if len(parts) < 2:
        return None
    degrees = parts[0]
    minutes = parts[1] if len(parts) > 1 else 0.0
    seconds = parts[2] if len(parts) > 2 else 0.0
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    ref = media._as_datetime_text(reference).upper()
    if ref in {"S", "W"}:
        decimal = -decimal
    return decimal


def location_from_exifread(path: Path) -> tuple[float, float] | None:
    if exifread is None:
        return None
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except OSError:
        return None
    if "GPS GPSLatitude" not in tags or "GPS GPSLongitude" not in tags:
        return None
    latitude = dms_to_decimal(tags["GPS GPSLatitude"], tags.get("GPS GPSLatitudeRef", "N"))
    longitude = dms_to_decimal(tags["GPS GPSLongitude"], tags.get("GPS GPSLongitudeRef", "E"))
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def location_from_pillow(path: Path) -> tuple[float, float] | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            try:
                gps = exif.get_ifd(IFD.GPSInfo) if IFD is not None else {}
            except Exception:
                gps = {}
            if not gps:
                return None
            latitude = dms_to_decimal(
                gps.get(GPS_LATITUDE), gps.get(GPS_LAT_REF, "N")
            )
            longitude = dms_to_decimal(
                gps.get(GPS_LONGITUDE), gps.get(GPS_LON_REF, "E")
            )
    except Exception:
        return None
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def location_from_mutagen(path: Path) -> tuple[float, float] | None:
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return None
    try:
        tags = MP4(path).tags or {}
    except Exception:
        return None
    for key in MUTAGEN_LOCATION_KEYS:
        values = tags.get(key)
        if not values:
            continue
        parsed = parse_iso6709(values[0])
        if parsed is not None:
            return parsed
    return None


def _read_box_header(handle, end: int) -> tuple[int, bytes, int] | None:
    start = handle.tell()
    if start + 8 > end:
        return None
    header = handle.read(8)
    if len(header) < 8:
        return None
    size, typ = struct.unpack(">I4s", header)
    header_size = 8
    if size == 1:
        wide = handle.read(8)
        if len(wide) < 8:
            return None
        size = struct.unpack(">Q", wide)[0]
        header_size = 16
    elif size == 0:
        size = end - start
    if size < header_size:
        return None
    return size, typ, header_size


def _skip_meta_fullbox(handle, box_end: int) -> None:
    if handle.tell() + 8 > box_end:
        return
    peek = handle.read(8)
    handle.seek(-8, os.SEEK_CUR)
    if len(peek) < 8:
        return
    child_type = peek[4:]
    if child_type.isalpha() or child_type in {b"hdlr", b"keys", b"ilst"}:
        return
    handle.read(4)


def _parse_xyz_payload(payload: bytes) -> tuple[float, float] | None:
    for candidate in (payload, payload[4:], payload[2:]):
        parsed = parse_iso6709(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_keys_atom(handle, box_end: int) -> list[str]:
    payload = handle.read(max(0, box_end - handle.tell()))
    if len(payload) < 8:
        return []
    _flags, count = struct.unpack(">II", payload[:8])
    offset = 8
    names: list[str] = []
    for _ in range(count):
        if offset + 8 > len(payload):
            break
        key_size = struct.unpack(">I", payload[offset:offset + 4])[0]
        if key_size < 8 or offset + key_size > len(payload):
            break
        name = payload[offset + 8:offset + key_size].decode("utf-8", errors="replace")
        names.append(name)
        offset += key_size
    return names


def _parse_data_text(handle, end: int) -> str | None:
    while handle.tell() + 8 <= end:
        start = handle.tell()
        header = _read_box_header(handle, end)
        if header is None:
            break
        size, typ, _header_size = header
        box_end = start + size
        if box_end > end:
            break
        if typ == b"data" and box_end - handle.tell() >= 8:
            handle.read(8)
            payload = handle.read(max(0, box_end - handle.tell()))
            handle.seek(box_end)
            return payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        handle.seek(box_end)
    return None


def _parse_ilst_atom(handle, box_end: int) -> dict[int, str]:
    values: dict[int, str] = {}
    while handle.tell() + 8 <= box_end:
        start = handle.tell()
        header = _read_box_header(handle, box_end)
        if header is None:
            break
        size, typ, _header_size = header
        item_end = start + size
        if item_end > box_end:
            break
        index = struct.unpack(">I", typ)[0]
        text = _parse_data_text(handle, item_end)
        if text:
            values[index] = text
        handle.seek(item_end)
    return values


def _location_from_keys_and_ilst(
    keys: list[str], values: dict[int, str]
) -> tuple[float, float] | None:
    for index, name in enumerate(keys, start=1):
        if not _is_location_key(name):
            continue
        text = values.get(index)
        if not text:
            continue
        parsed = parse_iso6709(text)
        if parsed is not None:
            return parsed
    return None


def _walk_mp4_location(handle, end: int) -> tuple[float, float] | None:
    found: tuple[float, float] | None = None
    while handle.tell() + 8 <= end:
        start = handle.tell()
        header = _read_box_header(handle, end)
        if header is None:
            break
        size, typ, _header_size = header
        box_end = start + size
        if box_end > end:
            break
        if typ in XYZ_ATOMS:
            payload = handle.read(max(0, box_end - handle.tell()))
            found = _parse_xyz_payload(payload) or found
        elif typ == b"loci":
            payload = handle.read(max(0, box_end - handle.tell()))
            found = _parse_xyz_payload(payload) or found
        elif typ == b"meta":
            _skip_meta_fullbox(handle, box_end)
            keys: list[str] = []
            values: dict[int, str] = {}
            while handle.tell() + 8 <= box_end:
                child_start = handle.tell()
                child = _read_box_header(handle, box_end)
                if child is None:
                    break
                child_size, child_type, _ = child
                child_end = child_start + child_size
                if child_end > box_end:
                    break
                if child_type == b"keys":
                    keys = _parse_keys_atom(handle, child_end)
                elif child_type == b"ilst":
                    values = _parse_ilst_atom(handle, child_end)
                handle.seek(child_end)
            found = _location_from_keys_and_ilst(keys, values) or found
        elif typ in MP4_LOCATION_CONTAINERS:
            found = _walk_mp4_location(handle, box_end) or found
        handle.seek(box_end)
        if found is not None and typ == b"moov":
            return found
    return found


def location_from_mp4_atoms(path: Path) -> tuple[float, float] | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(0)
            return _walk_mp4_location(handle, end)
    except OSError:
        return None


def get_location(path: Path) -> tuple[float, float] | None:
    if path.suffix.lower() in media.VIDEO_EXTENSIONS:
        return location_from_mutagen(path) or location_from_mp4_atoms(path)
    return location_from_exifread(path) or location_from_pillow(path)


def relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def prefer_existing(new: str, old: str) -> str:
    return new if new else old


def load_existing(csv_path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not csv_path.is_file():
        return list(FIELDNAMES), {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or FIELDNAMES)
        rows = {}
        for row in reader:
            key = (row.get("path") or row.get("filename") or "").strip()
            if key:
                rows[key] = row
    extras = [name for name in fieldnames if name not in FIELDNAMES]
    return FIELDNAMES + extras, rows


def collect_rows(root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in media.iter_media(root):
        taken_at = media.get_capture_datetime(path)
        location = get_location(path)
        latitude, longitude = location if location else (None, None)
        rows.append(
            {
                "path": relative_path(root, path),
                "filename": path.name,
                "datetime": format_datetime(taken_at),
                "latitude": format_coord(latitude),
                "longitude": format_coord(longitude),
            }
        )
    return rows


def merge_rows(
    scanned: list[dict[str, str]],
    existing: dict[str, dict[str, str]],
    fieldnames: list[str],
) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {
        key: {name: row.get(name, "") or "" for name in fieldnames}
        for key, row in existing.items()
    }
    for row in scanned:
        key = row["path"]
        previous = merged.get(key, {})
        combined = {name: previous.get(name, "") for name in fieldnames}
        combined["path"] = row["path"]
        combined["filename"] = row["filename"]
        combined["datetime"] = prefer_existing(row["datetime"], previous.get("datetime", ""))
        combined["latitude"] = prefer_existing(row["latitude"], previous.get("latitude", ""))
        combined["longitude"] = prefer_existing(row["longitude"], previous.get("longitude", ""))
        merged[key] = combined
    return [merged[key] for key in sorted(merged)]


def write_csv(csv_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_plan(csv_path: Path, rows: list[dict[str, str]], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY RUN (CSV will not be written)"
    print(mode)
    print(f"Output: {csv_path}")
    print()
    for row in rows:
        location = (
            f"{row['latitude']},{row['longitude']}"
            if row["latitude"] and row["longitude"]
            else ""
        )
        print(
            f"{row['path']}\t{row['datetime'] or '-'}\t{location or '-'}"
        )
    with_date = sum(1 for row in rows if row["datetime"])
    with_location = sum(1 for row in rows if row["latitude"] and row["longitude"])
    print()
    print(
        f"Summary: {len(rows)} row(s), "
        f"{with_date} with date, "
        f"{with_location} with location"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read pictures and videos under a directory and create or update "
            "meta/location.csv with filename, date/time, and GPS location."
        )
    )
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show extracted rows without writing the CSV",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write or update meta/location.csv (default)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.directory.expanduser()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    csv_path = root / CSV_NAME
    fieldnames, existing = load_existing(csv_path)
    scanned = collect_rows(root)
    rows = merge_rows(scanned, existing, fieldnames)
    apply = not args.dry_run
    print_plan(csv_path, rows, apply=apply)
    if apply:
        write_csv(csv_path, fieldnames, rows)
        print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
