#!/usr/bin/env python3
import re
import pandas as pd
from typing import Optional
from utils.logging import log

def to_csv_export_url(google_sheet_url: str) -> str:
    """
    Convert a Google Sheets URL to its CSV export URL format.

    Handles both standard Google Sheets URLs and URLs that are already in export format.
    Can extract both the spreadsheet ID and the specific sheet ID (gid).

    Args:
        google_sheet_url (str): The URL of a Google Sheet.

    Returns:
        str: A URL that can be used to download the sheet as CSV. If the input URL
            is already in CSV export format or cannot be parsed as a Google Sheets URL,
            returns the input URL unchanged.
    """
    if "export?format=csv" in google_sheet_url:
        return google_sheet_url
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)/", google_sheet_url)
    if not m:
        return google_sheet_url
    sheet_id = m.group(1)
    mg = re.search(r"[#&?]gid=([0-9]+)", google_sheet_url)
    gid = mg and mg.group(1)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv" + (f"&gid={gid}" if gid else "")

def read_sheet(sheet_url: Optional[str], csv_path: Optional[str]) -> pd.DataFrame:
    """
    Read data from either a Google Sheet URL or a local CSV file.

    Provides a unified interface for reading tabular data from either source.
    When reading from Google Sheets, automatically converts the URL to CSV export format.

    Args:
        sheet_url (Optional[str]): URL of a Google Sheet. If provided, takes precedence
            over csv_path.
        csv_path (Optional[str]): Path to a local CSV file. Used only if sheet_url
            is None.

    Returns:
        pd.DataFrame: The data from the sheet/CSV as a pandas DataFrame.

    Raises:
        ValueError: If neither sheet_url nor csv_path is provided.
        pd.errors.EmptyDataError: If the CSV file or sheet is empty.
        Exception: If there are network issues accessing the Google Sheet or
            if the local CSV file cannot be read.
    """
    if sheet_url:
        url = to_csv_export_url(sheet_url)
        log(f"Reading sheet: {url}")
        return pd.read_csv(url)
    elif csv_path:
        log(f"Reading CSV: {csv_path}")
        return pd.read_csv(csv_path)
    else:
        raise ValueError("Provide --sheet or --csv")
