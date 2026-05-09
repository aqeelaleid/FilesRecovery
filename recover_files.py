#!/usr/bin/env python3
"""
Windows-friendly file recovery helper.

This tool has two modes:
  - scan: find still-existing files by type and copy them to a restore folder.
  - carve: read a disk, partition, or image as raw bytes and recover files by
    known file signatures. This can recover deleted files after directory
    entries are gone, but filenames and folder paths are usually lost.

Use a restore folder on a different physical disk whenever possible.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence


ONE_MIB = 1024 * 1024

CATEGORIES: dict[str, set[str]] = {
    "images": {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
    },
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"},
    "documents": {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".rtf",
        ".csv",
    },
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
}


@dataclass(frozen=True)
class Pattern:
    magic: bytes
    start_adjust: int = 0


@dataclass(frozen=True)
class CarveHit:
    length: int
    extension: str
    confidence: str = "medium"


@dataclass(frozen=True)
class CarveFormat:
    name: str
    category: str
    extensions: frozenset[str]
    patterns: tuple[Pattern, ...]
    max_size: int
    carve: Callable[["SeekableReader", int, int], CarveHit | None]


class SeekableReader:
    def read(self, size: int = -1) -> bytes:
        raise NotImplementedError

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        raise NotImplementedError

    def tell(self) -> int:
        raise NotImplementedError


def normalize_extension(value: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError("empty extension")
    return value if value.startswith(".") else f".{value}"


def selected_extensions(types: Sequence[str], extensions: Sequence[str]) -> set[str]:
    chosen: set[str] = set()
    if not types and not extensions:
        types = ["all"]
    for file_type in types:
        file_type = file_type.lower()
        if file_type == "all":
            for values in CATEGORIES.values():
                chosen.update(values)
        elif file_type in CATEGORIES:
            chosen.update(CATEGORIES[file_type])
        else:
            raise ValueError(f"unknown file type: {file_type}")
    for extension in extensions:
        chosen.add(normalize_extension(extension))
    return chosen


def category_for_extension(extension: str) -> str:
    extension = normalize_extension(extension)
    for category, extensions in CATEGORIES.items():
        if extension in extensions:
            return category
    return "other"


def ensure_restore_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(value: str, max_length: int = 120) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not value:
        value = "file"
    if len(value) <= max_length:
        return value
    digest = hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"{value[: max_length - 11]}_{digest}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter:03d}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def get_windows_drives() -> list[str]:
    if os.name != "nt":
        return [str(Path.cwd().anchor or Path.cwd())]
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    drives: list[str] = []
    for index in range(26):
        if bitmask & (1 << index):
            drives.append(f"{chr(65 + index)}:\\")
    return drives


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def iter_existing_files(paths: Sequence[Path], restore_root: Path) -> Iterator[Path]:
    restore_root = restore_root.resolve()
    for root in paths:
        root = root.expanduser()
        if not root.exists():
            print(f"[warn] path does not exist: {root}", file=sys.stderr)
            continue
        if root.is_file():
            if root.resolve() != restore_root:
                yield root
            continue
        for current_root, dirnames, filenames in os.walk(root):
            current_path = Path(current_root)
            kept_dirs = []
            for dirname in dirnames:
                candidate = (current_path / dirname).resolve()
                if is_relative_to(candidate, restore_root):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in filenames:
                yield current_path / filename


def copy_existing_matches(
    paths: Sequence[Path],
    restore_root: Path,
    extensions: set[str],
    preserve_paths: bool,
    dry_run: bool,
    max_files: int | None,
) -> int:
    count = 0
    restore_root = ensure_restore_root(restore_root)
    roots = [path.expanduser().resolve() for path in paths]

    for source in iter_existing_files(roots, restore_root):
        extension = source.suffix.lower()
        if extension not in extensions:
            continue
        if max_files is not None and count >= max_files:
            break

        if preserve_paths:
            source_resolved = source.resolve()
            relative = None
            for root in roots:
                try:
                    relative = source_resolved.relative_to(root)
                    break
                except ValueError:
                    continue
            if relative is None:
                relative = Path(safe_name(str(source_resolved)))
            destination = restore_root / category_for_extension(extension) / relative
        else:
            digest = hashlib.sha1(str(source.resolve()).encode("utf-8", "ignore")).hexdigest()[:8]
            destination = (
                restore_root
                / category_for_extension(extension)
                / f"{safe_name(source.stem)}_{digest}{extension}"
            )

        destination = unique_path(destination)
        print(f"[copy] {source} -> {destination}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        count += 1
    return count


def find_marker_length(
    reader: SeekableReader,
    offset: int,
    marker: bytes,
    max_size: int,
    include_marker: bool = True,
    min_size: int = 0,
) -> int | None:
    reader.seek(offset)
    read_total = 0
    carry = b""
    marker_len = len(marker)
    while read_total < max_size:
        chunk = reader.read(min(ONE_MIB, max_size - read_total))
        if not chunk:
            return None
        data = carry + chunk
        search_start = max(0, min_size - (read_total - len(carry)))
        found = data.find(marker, search_start)
        if found != -1:
            absolute_found = offset + read_total - len(carry) + found
            end = absolute_found + (marker_len if include_marker else 0)
            return end - offset
        read_total += len(chunk)
        carry = data[-(marker_len - 1) :] if marker_len > 1 else b""
    return None


def carve_marker(
    marker: bytes,
    extension: str,
    min_size: int = 0,
    confidence: str = "medium",
) -> Callable[[SeekableReader, int, int], CarveHit | None]:
    def _carve(reader: SeekableReader, offset: int, max_size: int) -> CarveHit | None:
        length = find_marker_length(reader, offset, marker, max_size, True, min_size)
        if length is None:
            return None
        return CarveHit(length=length, extension=extension, confidence=confidence)

    return _carve


def carve_bmp(reader: SeekableReader, offset: int, max_size: int) -> CarveHit | None:
    reader.seek(offset)
    header = reader.read(14)
    if len(header) < 14 or not header.startswith(b"BM"):
        return None
    length = struct.unpack_from("<I", header, 2)[0]
    if 14 <= length <= max_size:
        return CarveHit(length=length, extension=".bmp", confidence="high")
    return None


def carve_riff(reader: SeekableReader, offset: int, max_size: int) -> CarveHit | None:
    reader.seek(offset)
    header = reader.read(12)
    if len(header) < 12 or not header.startswith(b"RIFF"):
        return None
    declared = struct.unpack_from("<I", header, 4)[0] + 8
    if declared < 12 or declared > max_size:
        return None
    kind = header[8:12]
    extension = {b"AVI ": ".avi", b"WAVE": ".wav", b"WEBP": ".webp"}.get(kind)
    if extension is None:
        return None
    return CarveHit(length=declared, extension=extension, confidence="high")


def carve_zip(reader: SeekableReader, offset: int, max_size: int) -> CarveHit | None:
    reader.seek(offset)
    if reader.read(4) != b"PK\x03\x04":
        return None
    reader.seek(offset)
    read_total = 0
    carry = b""
    eocd = b"PK\x05\x06"
    while read_total < max_size:
        chunk = reader.read(min(ONE_MIB, max_size - read_total))
        if not chunk:
            return None
        data = carry + chunk
        cursor = 0
        while True:
            found = data.find(eocd, cursor)
            if found == -1:
                break
            absolute_found = offset + read_total - len(carry) + found
            reader.seek(absolute_found)
            trailer = reader.read(22)
            if len(trailer) == 22:
                comment_len = struct.unpack_from("<H", trailer, 20)[0]
                length = absolute_found - offset + 22 + comment_len
                if 22 <= length <= max_size:
                    return CarveHit(length=length, extension=".zip", confidence="medium")
            cursor = found + 4
        read_total += len(chunk)
        carry = data[-3:]
    return None


def carve_mp4(reader: SeekableReader, offset: int, max_size: int) -> CarveHit | None:
    reader.seek(offset)
    first = reader.read(16)
    if len(first) < 16:
        return None
    first_size = struct.unpack_from(">I", first, 0)[0]
    if first[4:8] != b"ftyp" or first_size < 8 or first_size > 1024 * 1024:
        return None
    major_brand = first[8:12]
    extension = ".mov" if major_brand in {b"qt  "} else ".mp4"

    position = offset
    total = 0
    seen_media = False
    while total < max_size:
        reader.seek(position)
        header = reader.read(8)
        if len(header) < 8:
            break
        box_size = struct.unpack_from(">I", header, 0)[0]
        box_type = header[4:8]
        header_size = 8
        if box_size == 1:
            large_header = reader.read(8)
            if len(large_header) < 8:
                break
            box_size = struct.unpack(">Q", large_header)[0]
            header_size = 16
        elif box_size == 0:
            break

        if box_size < header_size or box_size > max_size - total:
            break
        if total == 0 and box_type != b"ftyp":
            return None
        if box_type in {b"mdat", b"moov"}:
            seen_media = True
        position += box_size
        total += box_size
        if seen_media and total >= first_size and box_type in {b"moov", b"mfra"}:
            return CarveHit(length=total, extension=extension, confidence="high")

    if seen_media and total > first_size:
        return CarveHit(length=total, extension=extension, confidence="medium")
    return None


def sniff_recovered_zip(path: Path) -> Path:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return path

    extension = ".zip"
    if "[Content_Types].xml" in names:
        if "word/document.xml" in names:
            extension = ".docx"
        elif "xl/workbook.xml" in names:
            extension = ".xlsx"
        elif "ppt/presentation.xml" in names:
            extension = ".pptx"
    if path.suffix.lower() == extension:
        return path
    renamed = unique_path(path.with_suffix(extension))
    path.rename(renamed)
    return renamed


CARVE_FORMATS: tuple[CarveFormat, ...] = (
    CarveFormat(
        "jpeg",
        "images",
        frozenset({".jpg", ".jpeg"}),
        (Pattern(b"\xff\xd8\xff"),),
        256 * ONE_MIB,
        carve_marker(b"\xff\xd9", ".jpg", min_size=128, confidence="medium"),
    ),
    CarveFormat(
        "png",
        "images",
        frozenset({".png"}),
        (Pattern(b"\x89PNG\r\n\x1a\n"),),
        256 * ONE_MIB,
        carve_marker(b"\x00\x00\x00\x00IEND\xaeB`\x82", ".png", min_size=32, confidence="high"),
    ),
    CarveFormat(
        "gif",
        "images",
        frozenset({".gif"}),
        (Pattern(b"GIF87a"), Pattern(b"GIF89a")),
        128 * ONE_MIB,
        carve_marker(b"\x3b", ".gif", min_size=32, confidence="low"),
    ),
    CarveFormat(
        "bmp",
        "images",
        frozenset({".bmp"}),
        (Pattern(b"BM"),),
        512 * ONE_MIB,
        carve_bmp,
    ),
    CarveFormat(
        "riff",
        "videos",
        frozenset({".avi", ".wav", ".webp"}),
        (Pattern(b"RIFF"),),
        4 * 1024 * ONE_MIB,
        carve_riff,
    ),
    CarveFormat(
        "pdf",
        "documents",
        frozenset({".pdf"}),
        (Pattern(b"%PDF-"),),
        512 * ONE_MIB,
        carve_marker(b"%%EOF", ".pdf", min_size=128, confidence="medium"),
    ),
    CarveFormat(
        "zip-office",
        "archives",
        frozenset({".zip", ".docx", ".xlsx", ".pptx"}),
        (Pattern(b"PK\x03\x04"),),
        2 * 1024 * ONE_MIB,
        carve_zip,
    ),
    CarveFormat(
        "mp4-mov",
        "videos",
        frozenset({".mp4", ".mov", ".m4v"}),
        (Pattern(b"ftyp", start_adjust=-4),),
        8 * 1024 * ONE_MIB,
        carve_mp4,
    ),
)


def selected_carve_formats(types: Sequence[str], extensions: Sequence[str]) -> list[CarveFormat]:
    extensions_set = selected_extensions(types, extensions)
    formats = [
        carve_format
        for carve_format in CARVE_FORMATS
        if carve_format.extensions & extensions_set or carve_format.category in types or "all" in types
    ]
    if not formats and extensions:
        supported = ", ".join(sorted({ext for fmt in CARVE_FORMATS for ext in fmt.extensions}))
        raise ValueError(f"none of those extensions can be carved. Supported raw formats: {supported}")
    return formats


def normalize_raw_source(source: str) -> str:
    source = source.strip()
    if os.name == "nt":
        match = re.fullmatch(r"([A-Za-z]):\\?", source)
        if match:
            return f"\\\\.\\{match.group(1).upper()}:"
        match = re.fullmatch(r"([A-Za-z])", source)
        if match:
            return f"\\\\.\\{match.group(1).upper()}:"
    return source


def copy_range(reader: SeekableReader, offset: int, length: int, destination: Path) -> None:
    reader.seek(offset)
    remaining = length
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while remaining > 0:
            chunk = reader.read(min(ONE_MIB, remaining))
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)


def carve_source(
    source: str,
    restore_root: Path,
    formats: Sequence[CarveFormat],
    allowed_extensions: set[str],
    chunk_size: int,
    dry_run: bool,
    max_files: int | None,
) -> int:
    restore_root = ensure_restore_root(restore_root)
    patterns: list[tuple[CarveFormat, Pattern]] = []
    for carve_format in formats:
        patterns.extend((carve_format, pattern) for pattern in carve_format.patterns)
    overlap = max(max(len(pattern.magic) + abs(pattern.start_adjust) for _, pattern in patterns), 32)

    recovered = 0
    seen_offsets: set[int] = set()
    source = normalize_raw_source(source)
    started = time.monotonic()

    print(f"[open] {source}")
    with open(source, "rb", buffering=0) as reader:
        offset = 0
        carry = b""
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            data = carry + chunk
            data_base = offset - len(carry)

            for carve_format, pattern in patterns:
                cursor = 0
                while True:
                    found = data.find(pattern.magic, cursor)
                    if found == -1:
                        break
                    absolute_start = data_base + found + pattern.start_adjust
                    cursor = found + 1
                    if absolute_start < 0 or absolute_start in seen_offsets:
                        continue
                    seen_offsets.add(absolute_start)

                    resume_at = reader.tell()
                    hit = carve_format.carve(reader, absolute_start, carve_format.max_size)
                    reader.seek(resume_at)
                    if hit is None:
                        continue
                    if hit.extension != ".zip" and hit.extension not in allowed_extensions:
                        continue

                    name = f"recovered_{absolute_start:012x}_{hit.confidence}{hit.extension}"
                    destination = unique_path(restore_root / category_for_extension(hit.extension) / name)
                    print(f"[carve] offset=0x{absolute_start:x} length={hit.length} -> {destination}")
                    if not dry_run:
                        copy_range(reader, absolute_start, hit.length, destination)
                        reader.seek(resume_at)
                        if hit.extension == ".zip":
                            destination = sniff_recovered_zip(destination)
                            if destination.suffix.lower() not in allowed_extensions:
                                destination.unlink(missing_ok=True)
                                continue
                            final_parent = restore_root / category_for_extension(destination.suffix)
                            if destination.parent != final_parent:
                                final_parent.mkdir(parents=True, exist_ok=True)
                                final_destination = unique_path(final_parent / destination.name)
                                destination.rename(final_destination)
                                destination = final_destination
                    recovered += 1
                    if max_files is not None and recovered >= max_files:
                        return recovered

            offset += len(chunk)
            carry = data[-overlap:]
            if offset and offset % (512 * ONE_MIB) < chunk_size:
                elapsed = max(time.monotonic() - started, 0.1)
                mib = offset / ONE_MIB
                print(f"[progress] scanned {mib:,.0f} MiB at {mib / elapsed:,.1f} MiB/s")
    return recovered


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover visible files or carve deleted/lost files from raw Windows disks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="Copy still-existing files matching selected types from folders or drives.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    scan.add_argument("--paths", nargs="+", type=Path, help="Folders/files to search.")
    scan.add_argument("--all-drives", action="store_true", help="Search every mounted drive.")
    scan.add_argument("--types", nargs="+", default=[], choices=[*CATEGORIES.keys(), "all"])
    scan.add_argument("--extensions", nargs="+", default=[], help="Extra extensions such as jpg pdf docx.")
    scan.add_argument("--restore-to", required=True, type=Path, help="Folder where recovered files are copied.")
    scan.add_argument("--flat", action="store_true", help="Do not preserve original folder paths.")
    scan.add_argument("--dry-run", action="store_true", help="Show what would be copied without writing files.")
    scan.add_argument("--max-files", type=int, help="Stop after recovering this many files.")

    carve = subparsers.add_parser(
        "carve",
        help="Recover deleted/lost files by scanning raw bytes from a drive, partition, or disk image.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    carve.add_argument(
        "--source",
        required=True,
        help=r"Raw source. Examples: E:, \\.\E:, \\.\PhysicalDrive1, or C:\path\disk.img",
    )
    carve.add_argument("--types", nargs="+", default=[], choices=[*CATEGORIES.keys(), "all"])
    carve.add_argument("--extensions", nargs="+", default=[], help="Limit to extensions such as jpg pdf mp4.")
    carve.add_argument("--restore-to", required=True, type=Path, help="Folder where carved files are written.")
    carve.add_argument("--chunk-size-mb", type=int, default=16, help="Raw read chunk size in MiB.")
    carve.add_argument("--dry-run", action="store_true", help="Show recoverable hits without writing files.")
    carve.add_argument("--max-files", type=int, help="Stop after recovering this many files.")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "scan":
            paths: list[Path] = []
            if args.all_drives:
                paths.extend(Path(drive) for drive in get_windows_drives())
            if args.paths:
                paths.extend(args.paths)
            if not paths:
                raise ValueError("provide --paths or --all-drives")
            extensions = selected_extensions(args.types, args.extensions)
            count = copy_existing_matches(
                paths=paths,
                restore_root=args.restore_to,
                extensions=extensions,
                preserve_paths=not args.flat,
                dry_run=args.dry_run,
                max_files=args.max_files,
            )
            print(f"[done] copied {count} file(s)")
            return 0

        if args.command == "carve":
            extensions = selected_extensions(args.types, args.extensions)
            formats = selected_carve_formats(args.types, args.extensions)
            count = carve_source(
                source=args.source,
                restore_root=args.restore_to,
                formats=formats,
                allowed_extensions=extensions,
                chunk_size=max(1, args.chunk_size_mb) * ONE_MIB,
                dry_run=args.dry_run,
                max_files=args.max_files,
            )
            print(f"[done] carved {count} file(s)")
            return 0
    except KeyboardInterrupt:
        print("\n[stopped] interrupted by user", file=sys.stderr)
        return 130
    except PermissionError as exc:
        print(f"[error] permission denied: {exc}", file=sys.stderr)
        print("Run Command Prompt or PowerShell as Administrator for raw disk access.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
