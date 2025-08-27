#!/usr/bin/env python3
"""
Fixed version that addresses:
- SWE-agent environment setup hang (skip Python standalone)
- Correct SWE-agent CLI flag (--config instead of --config_file)
- Skip rows without a real task_id
- Robust volume cleanup (force-remove containers using a volume)
- Avoid permission errors when populating named volumes (run BusyBox as root; use tar copy)
- Quieter copies (no "preserve ownership" noise)
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional, List
import threading

import pandas as pd

# ------------- config you can tweak -------------
BUILD_ARGS: List[str] = []
BASE_OUT = Path("tasks_runs").absolute()
IMAGE_PREFIX = "task-"
CONTAINER_PREFIX = "container_"
SWE_IMAGE = "swe-agent-runner:latest"
# -----------------------------------------------


def log(msg: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def have_docker() -> bool:
    try:
        subprocess.run(["docker", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False


def run_argv(argv: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    quoted = " ".join(_shell_quote(x) for x in argv)
    log(f"$ {quoted}")
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {quoted}")
    return proc


def run_capture_argv(argv: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
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


def _shell_quote(s: str) -> str:
    if not s or re.search(r"\s|['\"$`\\]", s):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


def drive_id_from_url(url: str) -> str:
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


def download_from_drive(url: str, out_path: Path, attempts: int = 4):
    import gdown
    file_id = drive_id_from_url(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Optional[Exception] = None
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


def unzip_to(zip_path: Path, dest_dir: Path):
    def _is_within_directory(base: Path, target: Path) -> bool:
        try:
            target.resolve().relative_to(base.resolve())
            return True
        except Exception:
            return False

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            out_path = dest_dir / member.filename
            if not _is_within_directory(dest_dir, out_path):
                raise RuntimeError(f"Zip member escapes dest dir: {member.filename}")
        zf.extractall(dest_dir)


def to_csv_export_url(google_sheet_url: str) -> str:
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
    if sheet_url:
        url = to_csv_export_url(sheet_url)
        log(f"Reading sheet: {url}")
        return pd.read_csv(url)
    elif csv_path:
        log(f"Reading CSV: {csv_path}")
        return pd.read_csv(csv_path)
    else:
        raise ValueError("Provide --sheet or --csv")


def ensure_swe_image(_swe_branch=None) -> None:
    got = subprocess.run(
        ["docker", "image", "inspect", SWE_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if got.returncode == 0:
        log(f"SWE image present: {SWE_IMAGE}")
        return

    log(f"Building SWE image: {SWE_IMAGE}")
    dockerfile_swe = r"""
    FROM python:3.11-slim

    ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONUNBUFFERED=1

    # OS deps + docker CLI
    RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg procps \
        && mkdir -p /etc/apt/keyrings \
        && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
        && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list \
        && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
        && rm -rf /var/lib/apt/lists/*

    # Install dependencies
    RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir --timeout 300 swe-rex

    # Install SWE-agent from source
    ARG SA_REF=main
    RUN git clone --depth 1 --branch "$SA_REF" https://github.com/SWE-agent/SWE-agent.git /opt/swe-agent \
    && cd /opt/swe-agent \
    && pip install --no-cache-dir --timeout 300 -e .

    # Verify installation
    RUN python -c "import sweagent; print('sweagent import OK')" \
    && python -m sweagent --help >/dev/null

    WORKDIR /workspace
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "Dockerfile").write_text(dockerfile_swe)
        run_capture_argv(["docker", "build", "-t", SWE_IMAGE, "."], cwd=Path(tmpdir), check=True)


def create_swe_config(config_path: Path) -> None:
    # Valid for SWE-agent 1.1.0 "run" schema
    config_content = """
agent:
  tools:
    enable_bash_tool: true

env:
  repo:
    path: /repo

problem_statement:
  path: /repo/issue.txt
"""
    config_path.write_text(config_content.strip())


def run_swe_agent_in_dedicated_container(
    volume_name: str,
    issue_desc: str,
    env_keys: Dict[str, str],
    log_path: Path,
    env_file: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout_seconds: int = 1800,
) -> None:
    import tempfile
    import os

    log(f"Starting SWE-agent for volume: {volume_name}")
    log(f"Issue description length: {len(issue_desc)} chars")
    log(f"Timeout set to: {timeout_seconds}s")

    # 1) Write the problem statement and config to the volume
    with tempfile.TemporaryDirectory() as tmpdir:
        # Issue description
        prompt_path_host = Path(tmpdir) / "issue.json"
        prompt_path_host.write_text(json.dumps({"issue_description": issue_desc}, ensure_ascii=False, indent=2))

        # Custom SWE-agent config to fix the hanging issue
        config_path_host = Path(tmpdir) / "swe_config.yaml"
        create_swe_config(config_path_host)

        # Copy both files to volume (as root to avoid permission issues)
        run_argv([
            "docker", "run", "--rm",
            "-v", f"{volume_name}:/repo",
            "-v", f"{tmpdir}:/tmp_host",
            "busybox", "sh", "-lc",
            "cp /tmp_host/issue.json /repo/issue.json && cp /tmp_host/swe_config.yaml /repo/swe_config.yaml"
        ])
        log("Issue description and config written to volume")

    # 2) Build env flags
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

    # 3) Docker socket setup
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

    # 4) Fixed SWE-agent launcher script
    swe_cmd = textwrap.dedent(f"""
        set -euo pipefail
        export PATH="$PATH:/usr/local/bin:~/.local/bin"

        progress() {{
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
        }}

        progress "Starting SWE-agent with fixed configuration"
        cd /repo

        # Git setup
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

        # Convert issue to text
        python - <<'PY'
import json
from pathlib import Path
try:
    j = json.loads(Path("issue.json").read_text(encoding="utf-8"))
    txt = j.get("issue_description") or j.get("description") or ""
    Path("issue.txt").write_text(txt, encoding="utf-8")
    print(f"Converted issue to text ({{len(txt)}} chars)")
except Exception as e:
    print(f"Error processing issue: {{e}}")
    raise
PY

        # Model selection
        if [ -n "${{MODEL_NAME-}}" ]; then
            MODEL_NAME="$MODEL_NAME"
        elif [ -n "${{GOOGLE_API_KEY-}}" ] || [ -n "${{GOOGLE_AI_API_KEY-}}" ] || [ -n "${{GEMINI_API_KEY-}}" ]; then
            MODEL_NAME="gemini-1.5-pro-latest"
        elif [ -n "${{OPENAI_API_KEY-}}" ]; then
            MODEL_NAME="gpt-4o"
        elif [ -n "${{ANTHROPIC_API_KEY-}}" ]; then
            MODEL_NAME="claude-3-5-sonnet-20241022"
        else
            MODEL_NAME="gemini-1.5-pro-latest"
        fi

        progress "Using model: $MODEL_NAME"
        progress "Starting SWE-agent run..."

        # Run with custom config that fixes the hanging issue (use --config)
       "$SA_CMD" run \
        --config="/repo/swe_config.yaml" \
        --agent.model.name="$MODEL_NAME" || {{
        echo "SWE-agent failed with exit code: $?"
        exit 1
        }}


        progress "SWE-agent completed successfully"
    """).strip()

    # 5) Run the container with timeout
    mount_args = sum([["-v", m] for m in socket_mounts], [])

    cmd = (
        ["docker", "run", "--rm", "-t"] + user_opt +
        ["-v", f"{volume_name}:/repo"] +
        mount_args +
        env_flags +
        ["-e", "PYTHONUNBUFFERED=1"] +
        [SWE_IMAGE, "bash", "-lc", swe_cmd]
    )

    log(f"Starting SWE-agent runner container...")

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
                current_time = time.time()
                elapsed = current_time - start_time
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


def _force_remove_containers_using_volume(vol: str):
    """Force remove any containers (any state) that still reference a volume."""
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


def main():
    parser = argparse.ArgumentParser(description="Run TX tasks with Docker + SWE-agent (Fixed Version).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="Google Sheet URL")
    src.add_argument("--csv", help="Local CSV path")
    parser.add_argument("--swe-branch", help="SWE-agent branch to clone", default=None)
    parser.add_argument("--repo-path-in-container", default="/opt/transifex-client")
    parser.add_argument("--no-cache", action="store_true", help="Build without Docker cache")
    parser.add_argument("--env-file", help="Environment file for SWE-agent", default=None)
    parser.add_argument("--test-timeout", type=int, default=1800, help="Test timeout in seconds")
    parser.add_argument("--build-timeout", type=int, default=3600, help="Build timeout in seconds")
    parser.add_argument("--keep", action="store_true", help="Keep containers/volumes for debugging")
    parser.add_argument("--model-name", default=None, help="Explicit model name")
    parser.add_argument("--swe-timeout", type=int, default=1800, help="SWE-agent timeout in seconds")
    args = parser.parse_args()

    if not have_docker():
        log("Docker not available")
        sys.exit(1)

    df = read_sheet(args.sheet, args.csv)

    required_cols = ["task_id", ".git.zip", "updated_issue_description", "dockerfile", "test_command"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log(f"Missing columns: {missing}")
        sys.exit(1)

    # Only keep rows with a real task_id
    df = df[df["task_id"].notna()]
    df = df[df["task_id"].astype(str).str.strip() != ""]

    BASE_OUT.mkdir(exist_ok=True)
    ensure_swe_image(args.swe_branch)

    _volatile = {"containers": [], "volumes": []}

    def _cleanup():
        if args.keep:
            return
        for c in list(_volatile["containers"]):
            try:
                run_argv(["docker", "rm", "-f", c], check=False)
            except Exception:
                pass
            finally:
                _volatile["containers"].remove(c)
        for v in list(_volatile["volumes"]):
            try:
                _force_remove_containers_using_volume(v)
                run_argv(["docker", "volume", "rm", v], check=False)
            except Exception:
                pass
            finally:
                _volatile["volumes"].remove(v)

    def _sig_handler(signum, frame):
        log(f"Received signal {signum}, cleaning up...")
        _cleanup()
        sys.exit(130)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    all_results: List[Dict[str, Any]] = []

    for i, row in df.iterrows():
        task_id = str(row["task_id"]).strip()
        if not task_id or task_id.lower() == "nan":
            continue

        git_zip_url = str(row[".git.zip"]).strip()
        issue_desc = str(row["updated_issue_description"]).strip()
        dockerfile_url = str(row["dockerfile"]).strip()
        test_cmd = str(row["test_command"]).strip()

        log("=" * 80)
        log(f"Starting task {task_id}")
        log("=" * 80)

        task_dir = BASE_OUT / f"task_{task_id}"
        if task_dir.exists():
            shutil.rmtree(task_dir)
        task_dir.mkdir(parents=True)

        volume_name = f"task_vol_{task_id}"
        # make sure no one holds the volume, then remove if exists
        _force_remove_containers_using_volume(volume_name)
        run_argv(["docker", "volume", "rm", volume_name], check=False)
        run_argv(["docker", "volume", "create", volume_name])
        _volatile["volumes"].append(volume_name)

        # Download and setup
        dockerfile_path = task_dir / "Dockerfile"
        git_zip_path = task_dir / ".git.zip"
        try:
            download_from_drive(dockerfile_url, dockerfile_path)
            download_from_drive(git_zip_url, git_zip_path)
        except Exception as e:
            result = {"task_id": task_id, "status": "download_error", "error": str(e)}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
            continue

        try:
            unzip_to(git_zip_path, task_dir)
            if not (task_dir / ".git").exists():
                raise RuntimeError("No .git folder found after unzip")
        except Exception as e:
            result = {"task_id": task_id, "status": "unzip_error", "error": str(e)}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
            continue

        # Copy workspace into named volume (as root to avoid perms)
        run_argv(
            ["docker", "run", "--rm",
             "-v", f"{volume_name}:/mnt/vol",
             "-v", f"{task_dir}:/mnt/host",
             "busybox", "sh", "-lc",
             # tar round-trip avoids ownership warnings and preserves tree
             "cd /mnt/host && tar cf - . | (cd /mnt/vol && tar xf -)"]
        )

        # Build image: copy volume contents into a temp build context (as root)
        image_tag = f"{IMAGE_PREFIX}{task_id}".lower()
        build_log = task_dir / "build.log"
        try:
            with tempfile.TemporaryDirectory() as tmpbuild:
                run_argv(
                    ["docker", "run", "--rm",
                     "-v", f"{volume_name}:/mnt/vol",
                     "-v", f"{tmpbuild}:/mnt/build",
                     "busybox", "sh", "-lc",
                     "cd /mnt/vol && tar cf - . | (cd /mnt/build && tar xf -) && chmod -R u+rwX /mnt/build"]
                )
                cmd = ["docker", "build", "--progress=plain"]
                if args.no_cache:
                    cmd.append("--no-cache")
                cmd += ["-t", image_tag] + BUILD_ARGS + ["."]
                proc = run_capture_argv(cmd, cwd=Path(tmpbuild), check=True, timeout=args.build_timeout)
                build_log.write_text(proc.stdout)
        except Exception as e:
            result = {"task_id": task_id, "status": "build_failed", "error": str(e)}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
            if not args.keep:
                _force_remove_containers_using_volume(volume_name)
                run_argv(["docker", "volume", "rm", volume_name], check=False)
                if volume_name in _volatile["volumes"]:
                    _volatile["volumes"].remove(volume_name)
            continue

        # Start container for tests
        container = f"{CONTAINER_PREFIX}{task_id}".lower()
        run_argv(["docker", "rm", "-f", container], check=False)
        rc = run_argv([
            "docker", "run", "-d",
            "--name", container,
            "-v", f"{volume_name}:{args.repo_path_in_container}",
            image_tag, "bash", "-lc", "sleep infinity"
        ], check=False).returncode
        if rc != 0:
            result = {"task_id": task_id, "status": "run_failed"}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
            if not args.keep:
                _force_remove_containers_using_volume(volume_name)
                run_argv(["docker", "volume", "rm", volume_name], check=False)
                if volume_name in _volatile["volumes"]:
                    _volatile["volumes"].remove(volume_name)
            continue
        _volatile["containers"].append(container)

        # Run SWE-agent with the fix
        swe_log = task_dir / "swe_agent.log"
        try:
            env_keys = {
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
                "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
                "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY", ""),
                "GOOGLE_AI_API_KEY": os.environ.get("GOOGLE_AI_API_KEY", ""),
                "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
            }

            run_swe_agent_in_dedicated_container(
                volume_name=volume_name,
                issue_desc=issue_desc,
                env_keys=env_keys,
                log_path=swe_log,
                env_file=args.env_file,
                model_name=args.model_name,
                timeout_seconds=args.swe_timeout,
            )
        except Exception as e:
            swe_log.write_text(str(e))
            log(f"SWE-agent error: {e}")

        # Run tests
        tests_log = task_dir / "tests.log"
        try:
            proc = run_capture_argv(
                ["docker", "exec", "-i", container, "bash", "-lc", test_cmd],
                check=False,
                timeout=args.test_timeout
            )
            tests_log.write_text(proc.stdout)
            status = "tests_passed" if proc.returncode == 0 else "tests_failed"
            result = {
                "task_id": task_id,
                "status": status,
                "test_exit_code": proc.returncode
            }
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
        except subprocess.TimeoutExpired:
            result = {"task_id": task_id, "status": "tests_timeout"}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))
        except Exception as e:
            result = {"task_id": task_id, "status": "tests_error", "error": str(e)}
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))

        # Cleanup
        if not args.keep:
            run_argv(["docker", "rm", "-f", container], check=False)
            _force_remove_containers_using_volume(volume_name)
            run_argv(["docker", "volume", "rm", volume_name], check=False)
            if container in _volatile["containers"]:
                _volatile["containers"].remove(container)
            if volume_name in _volatile["volumes"]:
                _volatile["volumes"].remove(volume_name)

    _cleanup()

    # Write summary
    summary = BASE_OUT / "summary.json"
    summary.write_text(json.dumps(all_results, indent=2))

    summary_csv = BASE_OUT / "summary.csv"
    with summary_csv.open("w", newline="") as f:
        if all_results:
            writer = csv.DictWriter(f, fieldnames=sorted({k for r in all_results for k in r.keys()}))
            writer.writeheader()
            writer.writerows(all_results)

    log("All tasks complete.")
    log("Summary:")
    for r in all_results:
        log(f"- {r.get('task_id')}: {r.get('status')}")
    log(f"Logs directory: {BASE_OUT}")


if __name__ == "__main__":
    main()
