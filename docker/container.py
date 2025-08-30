#!/usr/bin/env python3
from pathlib import Path
import tempfile

from config import SWE_IMAGE
from utils.command import run_argv, run_capture_argv
from utils.logging import log
from docker.dockerfile_templates import SWE_DOCKERFILE

def ensure_swe_image(_swe_branch=None) -> None:
    """
    Ensure the SWE-agent Docker image exists, building it if necessary.
    
    This function checks if the SWE-agent Docker image is already available in the local
    Docker registry. If not, it creates a new Dockerfile in a temporary directory and
    builds the image using the template defined in dockerfile_templates.py.
    
    Args:
        _swe_branch (str, optional): The branch of SWE-agent to clone. Currently unused
            but maintained for compatibility. Defaults to None.
    
    Returns:
        None
    
    Raises:
        RuntimeError: If the Docker build process fails.
    """
    got = run_capture_argv(
        ["docker", "image", "inspect", SWE_IMAGE],
        check=False
    )
    if got.returncode == 0:
        log(f"SWE image present: {SWE_IMAGE}")
        return

    log(f"Building SWE image: {SWE_IMAGE}")
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "Dockerfile").write_text(SWE_DOCKERFILE)
        run_capture_argv(["docker", "build", "-t", SWE_IMAGE, "."], cwd=Path(tmpdir), check=True)

def _force_remove_containers_using_volume(vol: str) -> None:
    """
    Force remove any containers (in any state) that still reference a specific Docker volume.
    
    This function searches for all containers that have the specified volume mounted and
    forcefully removes them using 'docker rm -f'. This is useful for cleanup operations
    when you need to ensure a volume can be safely removed.
    
    Args:
        vol (str): The name of the Docker volume to check for container references.
    
    Returns:
        None
    
    Note:
        - This is an internal helper function (marked by the leading underscore).
        - Any exceptions during the container removal process are silently caught and ignored
          to ensure the cleanup process continues even if individual removals fail.
        - The function uses 'docker rm -f' which forcefully stops and removes containers
          without confirmation.
    """
    try:
        out = run_capture_argv(
            ["docker", "ps", "-aq", "--filter", f"volume={vol}"],
            check=False
        ).stdout.strip()
        ids = [x for x in out.splitlines() if x.strip()]
        for cid in ids:
            run_argv(["docker", "rm", "-f", cid], check=False)
    except Exception:
        pass
