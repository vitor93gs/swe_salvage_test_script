#!/usr/bin/env python3
import time

def log(msg: str) -> None:
    """
    Log a message with a timestamp prefix.

    Prints a message to stdout with a timestamp prefix in the format [YYYY-MM-DD HH:MM:SS].
    Output is flushed immediately to ensure logs are written in real-time.

    Args:
        msg (str): The message to log.

    Returns:
        None
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)
