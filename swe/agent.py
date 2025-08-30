#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Dict, Optional, List

from config import SWE_IMAGE
from utils.command import run_argv
from utils.logging import log
from swe.config import create_swe_config

def run_swe_agent_in_dedicated_container(
    volume_name: str,
    issue_desc: str,
    env_keys: Dict[str, str],
    log_path: Path,
    env_file: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout_seconds: int = 1800,
) -> None:
    """
    Run SWE-agent in a dedicated Docker container with specified configuration.

    Sets up and runs the SWE-agent in a Docker container with proper configuration,
    environment variables, and volume mounts. The function handles:
    - Creating and managing configuration files
    - Setting up Git configuration in the container
    - Managing Docker socket access
    - Setting up model selection and API keys
    - Handling container lifecycle and timeout
    - Logging output and errors

    Args:
        volume_name (str): Name of the Docker volume to mount as /repo.
        issue_desc (str): Description of the issue for the SWE-agent to process.
        env_keys (Dict[str, str]): Dictionary of environment variables to pass to the container,
            typically containing API keys for different LLM providers.
        log_path (Path): Path where the SWE-agent's output should be logged.
        env_file (Optional[str]): Path to an environment file to pass to Docker. Defaults to None.
        model_name (Optional[str]): Specific model to use for the SWE-agent. If not provided,
            will be selected based on available API keys. Defaults to None.
        timeout_seconds (int): Maximum time in seconds to allow the SWE-agent to run.
            Defaults to 1800 (30 minutes).

    Returns:
        None

    Raises:
        RuntimeError: If the SWE-agent process fails or returns a non-zero exit code.
        OSError: If there are issues creating or writing to the log file.
        Exception: For other errors during execution (logged but not re-raised).

    Note:
        The function sets up various Docker configurations including:
        - Host network access for the container
        - Docker socket mounting for nested Docker access
        - User permissions matching the host user when possible
        - Environment variables for API keys and configuration
    """
    log(f"Starting SWE-agent for volume: {volume_name}")
    log(f"Issue description length: {len(issue_desc)} chars")
    log(f"Timeout set to: {timeout_seconds}s")

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_dir = Path(tmpdir)
        (cfg_dir / "issue.json").write_text(
            json.dumps({"issue_description": issue_desc}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        create_swe_config(cfg_dir / "swe_config.yaml")
        log("Prepared /cfg files (issue.json, swe_config.yaml)")

        # Pre-clean the repo in the named volume (and mark safe)
        run_argv([
            "docker", "run", "--rm",
            "-v", f"{volume_name}:/repo",
            SWE_IMAGE, "bash", "-lc",
            (
                "set -euo pipefail; cd /repo; "
                "git config --global --add safe.directory /repo || true; "
                "if [ -d .git ]; then "
                "  git config core.filemode false || true; "
                "  git reset --hard HEAD >/dev/null 2>&1 || true; "
                "  git clean -fd >/dev/null 2>&1 || true; "
                "  git status --porcelain || true; "
                "else "
                "  echo 'No .git directory in /repo; skipping git clean.'; "
                "fi"
            ),
        ], check=False)

        # Build env flags
        env_flags: List[str] = []
        api_keys_found = []
        for k, v in env_keys.items():
            if v:
                env_flags += ["-e", k]
                api_keys_found.append(k)
        log(f"API keys detected: {api_keys_found}")

        if env_file:
            env_flags += ["--env-file", env_file]

        if model_name:
            env_flags += ["-e", f"MODEL_NAME={model_name}"]
            log(f"Using explicit model: {model_name}")

        # Docker socket setup
        docker_host = os.environ.get("DOCKER_HOST", "")
        if docker_host:
            env_flags += ["-e", "DOCKER_HOST"]

        socket_mounts: List[str] = []
        if docker_host.startswith("unix://"):
            host_sock = docker_host.replace("unix://", "")
            if os.path.exists(host_sock):
                socket_mounts.append(f"{host_sock}:{host_sock}")
        else:
            if os.path.exists("/var/run/docker.sock"):
                socket_mounts.append("/var/run/docker.sock:/var/run/docker.sock")

        user_opt: List[str] = []
        try:
            uid = os.getuid()
            gid = os.getgid()
            if docker_host.startswith("unix://") and f"/run/user/{uid}/docker.sock" in docker_host:
                user_opt = ["--user", f"{uid}:{gid}"]
                if os.environ.get("XDG_RUNTIME_DIR"):
                    env_flags += ["-e", "XDG_RUNTIME_DIR"]
        except Exception:
            pass

        # Launcher script
        swe_cmd = textwrap.dedent("""
            set -euo pipefail
            export PATH="$PATH:/usr/local/bin:~/.local/bin"

            progress() {
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
            }

            progress "Starting SWE-agent with fixed configuration"
            cd /repo

            # Git: avoid 'dubious ownership'
            git config --global --add safe.directory /repo || true
            git config --global user.email "sweagent@example.com" || true
            git config --global user.name "SWE Agent" || true

            # Detect CLI
            if command -v sweagent >/dev/null 2>&1; then
                SA_CMD="sweagent"
            elif python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('sweagent') else 1)" >/dev/null 2>&1; then
                SA_CMD="python -m sweagent"
            else
                echo "ERROR: sweagent not found" >&2
                exit 127
            fi
            progress "Found SWE-agent: $SA_CMD"

            # Docker check
            if ! timeout 30 docker version >/dev/null 2>&1; then
                echo "ERROR: Docker not accessible" >&2
                exit 127
            fi
            progress "Docker verified"

            # Convert issue JSON -> text file
            python - <<'PY'
import json
from pathlib import Path
j = json.loads(Path("/cfg/issue.json").read_text(encoding="utf-8"))
txt = j.get("issue_description") or j.get("description") or ""
Path("/cfg/issue.txt").write_text(txt, encoding="utf-8")
print(f"Converted issue to text ({len(txt)} chars) -> /cfg/issue.txt")
PY

            # Model selection
            if [ -n "${MODEL_NAME-}" ]; then
                MODEL_NAME="$MODEL_NAME"
            elif [ -n "${GOOGLE_API_KEY-}" ] || [ -n "${GOOGLE_AI_API_KEY-}" ] || [ -n "${GEMINI_API_KEY-}" ]; then
                MODEL_NAME="gemini-1.5-pro-latest"
            elif [ -n "${OPENAI_API_KEY-}" ]; then
                MODEL_NAME="gpt-4o"
            elif [ -n "${ANTHROPIC_API_KEY-}" ]; then
                MODEL_NAME="claude-3-5-sonnet-20241022"
            else
                MODEL_NAME="gemini-1.5-pro-latest"
            fi
            progress "Using model: $MODEL_NAME"
            progress "Starting SWE-agent run..."

            # Run with config & force parser type (Gemini lacks function calling)
            "$SA_CMD" run \
              --config="/cfg/swe_config.yaml" \
              --agent.model.name="$MODEL_NAME" \
              --agent.tools.parse_function.type=thought_action || {
                echo "SWE-agent failed with exit code: $?"
                exit 1
              }

            progress "SWE-agent completed successfully"
        """).strip()

        # Launch runner container
        mount_args = sum([["-v", m] for m in socket_mounts], [])
        network_args = ["--network", "host"]

        cmd = (
            ["docker", "run", "--rm", "-t"]
            + user_opt
            + network_args
            + ["-v", f"{volume_name}:/repo"]
            + ["-v", f"{cfg_dir}:/cfg"]
            + mount_args
            + env_flags
            + ["-e", "PYTHONUNBUFFERED=1"]
            + [SWE_IMAGE, "bash", "-lc", swe_cmd]
        )

        log("Starting SWE-agent runner container...")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        with log_path.open("w", encoding="utf-8") as lf:
            try:
                start_time = time.time()
                for line in iter(proc.stdout.readline, ""):
                    elapsed = time.time() - start_time
                    print(line, end="")
                    lf.write(line)
                    lf.flush()
                    if timeout_seconds > 0 and elapsed > timeout_seconds:
                        log(f"Timeout reached ({timeout_seconds}s), terminating...")
                        proc.terminate()
                        time.sleep(5)
                        if proc.poll() is None:
                            proc.kill()
                        break
            except Exception as e:
                log(f"Error during execution: {e}")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

        if proc.returncode != 0:
            log(f"SWE-agent exited with code {proc.returncode}. See {log_path.name}.")
        else:
            log(f"SWE-agent completed successfully. See {log_path.name}.")
