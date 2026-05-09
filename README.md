# Windows Lost File Recovery Tool

This project is a pure-Python recovery utility for Windows. It has two recovery modes:

- `scan`: searches normal folders or mounted drives for files that still exist and copies selected file types to a restore folder.
- `carve`: scans a raw drive, partition, or disk image for deleted/lost files using file signatures. This can recover files after their folder entries are gone, but original names and paths are usually not recoverable.

Important: recover files to a different physical disk whenever possible. Writing recovered files back to the same disk can overwrite deleted data you are trying to save.

## Requirements

- Python 3.10 or newer.
- Administrator Command Prompt or PowerShell for raw disk access.
- No third-party Python packages are required.

## Examples

Copy visible images and videos from a folder:

```powershell
python recover_files.py scan --paths "D:\Photos" --types images videos --restore-to "E:\Recovered"
```

Search all mounted drives for documents and images:

```powershell
python recover_files.py scan --all-drives --types documents images --restore-to "E:\Recovered"
```

Carve deleted JPEG, PNG, PDF, ZIP/Office, MP4/MOV, AVI/WAV/WEBP, GIF, and BMP files from a USB drive:

```powershell
python recover_files.py carve --source E: --types all --restore-to "D:\Recovered"
```

Carve only photos from a disk image:

```powershell
python recover_files.py carve --source "D:\backup\drive.img" --types images --restore-to "D:\Recovered"
```

Preview without writing files:

```powershell
python recover_files.py carve --source E: --types images --restore-to "D:\Recovered" --dry-run
```

## Supported raw carving formats

Raw carving currently supports:

- Images: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.webp`
- Videos: `.mp4`, `.mov`, `.m4v`, `.avi`
- Audio: `.wav`
- Documents: `.pdf`, `.docx`, `.xlsx`, `.pptx`
- Archives: `.zip`

Normal folder scanning supports a wider list of extensions for images, videos, audio, documents, and archives.

## Build a standalone Windows executable

Install PyInstaller on the Windows machine:

```powershell
py -m pip install pyinstaller
```

Build:

```powershell
py -m PyInstaller --onefile --name recover-files recover_files.py
```

Or run:

```powershell
.\build_windows_exe.bat
```

The executable will be created at:

```text
dist\recover-files.exe
```

Run it as Administrator when using `carve` against raw drives.

## Practical recovery advice

1. Stop using the disk that lost files immediately.
2. Recover to a different disk.
3. Prefer scanning a disk image if you can make one first.
4. Use `scan` if the files may simply be misplaced.
5. Use `carve` if the files were deleted, the filesystem is damaged, or the partition is unreadable.

## Limitations

File carving cannot usually restore original filenames, folder paths, fragmented files, or overwritten data. Large video files are often fragmented and may only recover when their data is still contiguous. For severe NTFS damage or high-value recovery, make a sector-by-sector image and consider a professional recovery workflow.
