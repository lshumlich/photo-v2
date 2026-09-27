#!/usr/bin/env python3
"""Rename photos from their capture date/time.

Walks a directory tree, reads the photo's taken-at timestamp, and renames
matching files to yyyymmdd_hhmm_nnn (plus the original extension). Files
without a capture date are left alone.

Dry-run is the default. Pass --apply to rename files.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

try:
    from pillow_heif import register_heif_opener
except ImportError:
    register_heif_opener = None
else:
    register_heif_opener()


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".orf",
    ".rw2",
    ".raf",
}

VIDEO_EXTENSIONS = {".mov", ".mp4"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"udta", b"meta"}
MAC_EPOCH = datetime(1904, 1, 1)
MUTAGEN_DATE_KEYS = (
    "----:com.apple.quicktime:creationdate",
    "©day",
    "\xa9day",
)

# DateTimeOriginal, DateTimeDigitized, DateTime
EXIFREAD_TAGS = (
    "EXIF DateTimeOriginal",
    "EXIF DateTimeDigitized",
    "Image DateTime",
)
EXIFREAD_SUBSEC_TAGS = (
    "EXIF SubSecTimeOriginal",
    "EXIF SubSecTimeDigitized",
    "EXIF SubSecTime",
)
PIL_EXIF_TAGS = (36867, 36868, 306)
PIL_SUBSEC_TAGS = (37521, 37522, 37520)

ALREADY_NAMED = re.compile(r"^(\d{8}_\d{4})_(\d+)$")


@dataclass(frozen=True)
class Photo:
    path: Path
    taken_at: datetime
    prefix: str


def _as_datetime_text(raw: object) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip().strip("\x00").strip()
    return str(raw).strip().strip("\x00").strip()


def parse_exif_datetime(raw: object) -> datetime | None:
    text = _as_datetime_text(raw)
    if not text or text.startswith("0000"):
        return None
    core = text[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(core, fmt)
        except ValueError:
            continue
    return None


def parse_media_datetime(raw: object) -> datetime | None:
    parsed = parse_exif_datetime(raw)
    if parsed is not None:
        return parsed
    text = _as_datetime_text(raw)
    if not text or text.startswith("0000"):
        return None
    iso = text.replace("Z", "+00:00")
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


def apply_subseconds(dt: datetime, raw: object) -> datetime:
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return dt
    microseconds = int((digits + "000000")[:6])
    return dt.replace(microsecond=microseconds)


def datetime_from_exifread(path: Path) -> datetime | None:
    if exifread is None:
        return None
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except OSError:
        return None
    for tag, subsec_tag in zip(EXIFREAD_TAGS, EXIFREAD_SUBSEC_TAGS):
        if tag not in tags:
            continue
        parsed = parse_exif_datetime(tags[tag])
        if parsed is None:
            continue
        if subsec_tag in tags:
            parsed = apply_subseconds(parsed, tags[subsec_tag])
        return parsed
    return None


def _pil_exif_value(exif, ifd: dict, tag: int):
    if ifd and tag in ifd:
        return ifd[tag]
    return exif.get(tag)


def datetime_from_pillow(path: Path) -> datetime | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            try:
                ifd = exif.get_ifd(IFD.Exif) if IFD is not None else {}
            except Exception:
                ifd = {}
            for tag, subsec_tag in zip(PIL_EXIF_TAGS, PIL_SUBSEC_TAGS):
                value = _pil_exif_value(exif, ifd, tag)
                if not value:
                    continue
                parsed = parse_exif_datetime(value)
                if parsed is None:
                    continue
                subsec = _pil_exif_value(exif, ifd, subsec_tag)
                if subsec:
                    parsed = apply_subseconds(parsed, subsec)
                return parsed
    except Exception:
        return None
    return None


def datetime_from_mutagen(path: Path) -> datetime | None:
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return None
    try:
        media = MP4(path)
    except Exception:
        return None
    tags = media.tags or {}
    for key in MUTAGEN_DATE_KEYS:
        values = tags.get(key)
        if not values:
            continue
        parsed = parse_media_datetime(values[0])
        if parsed is not None:
            return parsed
    return None


def _read_mp4_box_header(handle) -> tuple[int, bytes, int] | None:
    start = handle.tell()
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
        handle.seek(0, os.SEEK_END)
        size = handle.tell() - start
        handle.seek(start + header_size)
    return size, typ, header_size


def _parse_mvhd_datetime(handle) -> datetime | None:
    version_flags = handle.read(4)
    if len(version_flags) < 4:
        return None
    if version_flags[0] == 1:
        data = handle.read(16)
        if len(data) < 16:
            return None
        creation_time = struct.unpack(">Q", data[:8])[0]
    else:
        data = handle.read(8)
        if len(data) < 8:
            return None
        creation_time = struct.unpack(">I", data[:4])[0]
    if not creation_time:
        return None
    try:
        created = MAC_EPOCH + timedelta(seconds=creation_time)
    except OverflowError:
        return None
    if created.year < 1990:
        return None
    return created.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _walk_mp4_for_mvhd(handle, end: int) -> datetime | None:
    while handle.tell() + 8 <= end:
        start = handle.tell()
        header = _read_mp4_box_header(handle)
        if header is None:
            break
        size, typ, header_size = header
        if size < header_size:
            break
        box_end = start + size
        if box_end > end:
            break
        if typ == b"mvhd":
            parsed = _parse_mvhd_datetime(handle)
            if parsed is not None:
                return parsed
        elif typ in MP4_CONTAINERS:
            if typ == b"meta" and handle.tell() + 4 <= box_end:
                handle.read(4)
            parsed = _walk_mp4_for_mvhd(handle, box_end)
            if parsed is not None:
                return parsed
        handle.seek(box_end)
    return None


def datetime_from_mp4_mvhd(path: Path) -> datetime | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(0)
            return _walk_mp4_for_mvhd(handle, end)
    except OSError:
        return None


def get_capture_datetime(path: Path) -> datetime | None:
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return datetime_from_mutagen(path) or datetime_from_mp4_mvhd(path)
    return datetime_from_exifread(path) or datetime_from_pillow(path)


def iter_media(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and name.lower() != "meta"
        ]
        for name in filenames:
            if name.startswith(".") or name.startswith("._"):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                files.append(path)
    files.sort(key=lambda item: (str(item.parent).lower(), item.name.lower()))
    return files


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def next_free_sequence(used: set[int]) -> int:
    sequence = 1
    while sequence in used:
        sequence += 1
    return sequence


def assign_destinations(photos: list[Photo]) -> list[tuple[Path, Path]]:
    groups: dict[tuple[Path, str], list[Photo]] = defaultdict(list)
    for photo in photos:
        groups[(photo.path.parent, photo.prefix)].append(photo)

    renames: list[tuple[Path, Path]] = []
    for (directory, prefix), items in groups.items():
        items.sort(key=lambda photo: (photo.taken_at, photo.path.name.lower()))
        grouped_paths = {photo.path for photo in items}
        used: set[int] = set()

        for entry in directory.iterdir():
            if not entry.is_file() or entry in grouped_paths:
                continue
            match = ALREADY_NAMED.fullmatch(entry.stem)
            if match and match.group(1) == prefix:
                used.add(int(match.group(2)))

        keep: dict[Photo, int] = {}
        pending: list[Photo] = []
        for photo in items:
            match = ALREADY_NAMED.fullmatch(photo.path.stem)
            if match and match.group(1) == prefix:
                sequence = int(match.group(2))
                if sequence not in used:
                    used.add(sequence)
                    keep[photo] = sequence
                    continue
            pending.append(photo)

        for photo in pending:
            sequence = next_free_sequence(used)
            used.add(sequence)
            dest = directory / f"{prefix}_{sequence:03d}{photo.path.suffix}"
            if not same_path(photo.path, dest):
                renames.append((photo.path, dest))

        for photo, sequence in keep.items():
            dest = directory / f"{prefix}_{sequence:03d}{photo.path.suffix}"
            if not same_path(photo.path, dest):
                renames.append((photo.path, dest))

    return renames


def plan_renames(
    root: Path,
) -> tuple[list[tuple[Path, Path]], list[Path], list[Path], list[Path]]:
    skipped: list[Path] = []
    errors: list[Path] = []
    dated: list[Photo] = []

    for path in iter_media(root):
        try:
            taken_at = get_capture_datetime(path)
        except Exception:
            errors.append(path)
            continue
        if taken_at is None:
            skipped.append(path)
            continue
        dated.append(Photo(path, taken_at, taken_at.strftime("%Y%m%d_%H%M")))

    renames = assign_destinations(dated)
    rename_sources = {source for source, _dest in renames}
    already = [photo.path for photo in dated if photo.path not in rename_sources]
    return renames, already, skipped, errors


def apply_renames(renames: list[tuple[Path, Path]]) -> None:
    token = uuid.uuid4().hex[:8]
    staged: list[tuple[Path, Path]] = []
    try:
        for source, dest in renames:
            if dest.exists() and not same_path(source, dest):
                raise FileExistsError(f"Refusing to overwrite existing file: {dest}")
            temp = source.with_name(f"{source.name}.__renaming_{token}")
            source.rename(temp)
            staged.append((temp, dest))
        for temp, dest in staged:
            if dest.exists() and not same_path(temp, dest):
                raise FileExistsError(f"Refusing to overwrite existing file: {dest}")
            temp.rename(dest)
    except Exception:
        for temp, dest in staged:
            if temp.exists() and not dest.exists():
                original = Path(str(temp).rsplit(f".__renaming_{token}", 1)[0])
                if not original.exists():
                    temp.rename(original)
        raise


def print_plan(
    root: Path,
    renames: list[tuple[Path, Path]],
    already: list[Path],
    skipped: list[Path],
    errors: list[Path],
    apply: bool,
) -> None:
    mode = "APPLY" if apply else "DRY RUN (no files will be renamed)"
    print(f"{mode}")
    print(f"Root: {root}")
    print()

    for source, dest in renames:
        print(f"RENAME  {source}  ->  {dest.name}")
    for path in already:
        print(f"KEEP    {path}  (already named)")
    for path in skipped:
        print(f"KEEP    {path}  (no capture date)")
    for path in errors:
        print(f"ERROR   {path}  (could not read metadata)")

    print()
    print(
        f"Summary: {len(renames)} rename(s), "
        f"{len(already) + len(skipped)} left unchanged, "
        f"{len(errors)} error(s)"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename pictures and videos under a directory to yyyymmdd_hhmm_nnn "
            "using the capture date/time. Files without a date are left alone."
        )
    )
    parser.add_argument("directory", type=Path, help="Root directory to scan")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be renamed without changing files (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename the files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.directory.expanduser()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    if exifread is None and Image is None:
        print(
            "Install Pillow or exifread to read photo dates "
            "(see requirements.txt).",
            file=sys.stderr,
        )
        return 2

    renames, already, skipped, errors = plan_renames(root)
    print_plan(root, renames, already, skipped, errors, apply=args.apply)

    if args.apply and renames:
        apply_renames(renames)
        print("Renames completed.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
