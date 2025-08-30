#!/usr/bin/env python3
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from utils.logging import log

def _shell_quote(s: str) -> str:
    """
    Quote a string for safe use in shell commands.

    Wraps a string in single quotes if it contains whitespace or shell metacharacters.
    Handles embedded single quotes by using the '"\'"' escape sequence.

    Args:
        s (str): The string to be quoted.

    Returns:
        str: The quoted string if necessary, or the original string if quoting is not needed.
    """
    if not s or re.search(r"\s|['\"$`\\]", s):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s

def run_argv(argv: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """
    Run a command with arguments and return its result.

    Executes a command with the given arguments, optionally in a specific working directory.
    The command and its arguments are logged before execution.

    Args:
        argv (List[str]): The command and its arguments as a list of strings.
        cwd (Optional[Path]): The working directory to run the command in. Defaults to None.
        check (bool): If True, raises RuntimeError on non-zero exit codes. Defaults to True.
        timeout (Optional[int]): Maximum time in seconds to wait for command completion.
            None means wait indefinitely. Defaults to None.

    Returns:
        subprocess.CompletedProcess: The completed process information.

    Raises:
        RuntimeError: If check=True and the command returns a non-zero exit code.
        subprocess.TimeoutExpired: If the command exceeds the timeout.
    """
    quoted = " ".join(_shell_quote(x) for x in argv)
    log(f"$ {quoted}")
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {quoted}")
    return proc

def run_capture_argv(argv: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """
    Run a command with arguments and capture its output.

    Similar to run_argv, but captures both stdout and stderr in the returned object.
    The command's output is captured as text and stderr is redirected to stdout.

    Args:
        argv (List[str]): The command and its arguments as a list of strings.
        cwd (Optional[Path]): The working directory to run the command in. Defaults to None.
        check (bool): If True, raises RuntimeError on non-zero exit codes. Defaults to True.
        timeout (Optional[int]): Maximum time in seconds to wait for command completion.
            None means wait indefinitely. Defaults to None.

    Returns:
        subprocess.CompletedProcess: The completed process information with captured output
            in the stdout attribute (as text).

    Raises:
        RuntimeError: If check=True and the command returns a non-zero exit code.
            The error message includes the captured output.
        subprocess.TimeoutExpired: If the command exceeds the timeout.
    """
    quoted = " ".join(_shell_quote(x) for x in argv)
    log(f"$ {quoted}")
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {quoted}\n{proc.stdout}")
    return proc

def have_docker() -> bool:
    """
    Check if Docker is available and accessible on the system.

    Attempts to run 'docker --version' to verify that Docker is installed
    and the current user has permission to use it.

    Returns:
        bool: True if Docker is available and accessible, False otherwise.
            Returns False if Docker is not installed, not running, or the
            current user doesn't have permission to use it.
    """
    try:
        subprocess.run(["docker", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False
