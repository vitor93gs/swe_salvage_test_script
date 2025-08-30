#!/usr/bin/env python3
import zipfile
from pathlib import Path

def _is_within_directory(base: Path, target: Path) -> bool:
    """
    Check if a target path is contained within a base directory.

    Args:
        base (Path): The base directory path.
        target (Path): The target path to check.

    Returns:
        bool: True if target is within base directory, False otherwise.
    """
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False

def unzip_to(zip_path: Path, dest_dir: Path) -> None:
    """
    Safely extract a zip file to a destination directory with path traversal protection.

    Ensures all extracted files remain within the destination directory by checking each
    path before extraction. This prevents zip slip vulnerabilities where archive members
    might try to write files outside the target directory.

    Args:
        zip_path (Path): Path to the zip file to extract.
        dest_dir (Path): Directory where the contents should be extracted to.

    Returns:
        None

    Raises:
        RuntimeError: If any archive member would be extracted outside the destination
            directory (path traversal attempt).
        zipfile.BadZipFile: If the file is not a valid zip file.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            out_path = dest_dir / member.filename
            if not _is_within_directory(dest_dir, out_path):
                raise RuntimeError(f"Zip member escapes dest dir: {member.filename}")
        zf.extractall(dest_dir)
