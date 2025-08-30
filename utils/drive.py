#!/usr/bin/env python3
import re
import random
import time
from pathlib import Path
from utils.logging import log

def drive_id_from_url(url: str) -> str:
    """
    Extract the file ID from a Google Drive URL.

    Supports various Google Drive URL formats including:
    - /file/d/<id>
    - ?id=<id>
    - /uc?id=<id>

    Args:
        url (str): The Google Drive URL to parse.

    Returns:
        str: The extracted file ID.

    Raises:
        ValueError: If the URL is not a valid Google Drive URL or if the file ID
            cannot be extracted from the URL.
    """
    if "drive.google.com" not in url:
        raise ValueError(f"Not a Google Drive URL: {url}")
    patterns = [
        r"/file/d/([A-Za-z0-9_-]{10,})",
        r"[?&]id=([A-Za-z0-9_-]{10,})",
        r"/uc\?id=([A-Za-z0-9_-]{10,})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    raise ValueError(f"Could not parse Drive file id from: {url}")

def download_from_drive(url: str, out_path: Path, attempts: int = 4) -> None:
    """
    Download a file from Google Drive with retry logic.

    Downloads a file using gdown library with exponential backoff retry logic.
    Creates parent directories if they don't exist.

    Args:
        url (str): The Google Drive URL of the file to download.
        out_path (Path): The path where the downloaded file should be saved.
        attempts (int, optional): Maximum number of download attempts. Defaults to 4.

    Returns:
        None

    Raises:
        RuntimeError: If all download attempts fail or if the downloaded file is empty.
            The error message includes the last exception encountered.
        ValueError: If the provided URL is not a valid Google Drive URL.
    
    Note:
        Uses exponential backoff with random jitter between retry attempts.
        The wait time between attempts follows the pattern: 2^attempt + random(0,1)
    """
    import gdown
    file_id = drive_id_from_url(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception = None
    for i in range(1, attempts + 1):
        try:
            log(f"Downloading from Drive -> {out_path.name} (attempt {i}/{attempts})")
            gdown.download(id=file_id, output=str(out_path), quiet=False)
            if out_path.exists() and out_path.stat().st_size > 0:
                return
            raise RuntimeError("Zero-sized download")
        except Exception as e:
            last_exc = e
            if i == attempts:
                break
            sleep = 2 ** i + random.random()
            log(f"Download failed ({e}); retrying in {sleep:.1f}s...")
            time.sleep(sleep)
    raise RuntimeError(f"Failed to download after {attempts} attempts: {last_exc}")
