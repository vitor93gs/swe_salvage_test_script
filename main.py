#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any, List

from config import (
    BASE_OUT,
    TEST_LOGS_DIR,
    IMAGE_PREFIX,
    CONTAINER_PREFIX,
    BUILD_ARGS,
)
from docker.container import ensure_swe_image, _force_remove_containers_using_volume
from swe.agent import run_swe_agent_in_dedicated_container
from utils.command import have_docker, run_argv, run_capture_argv
from utils.drive import download_from_drive
from utils.file import unzip_to
from utils.logging import log
from utils.sheet import read_sheet

def main() -> None:
    """
    Main entry point for the testing script.

    Processes a list of tasks from a Google Sheet or CSV file, executing each task in
    a Docker container with SWE-agent. For each task:
    1. Downloads required files from Google Drive
    2. Sets up Docker volumes and containers
    3. Runs SWE-agent to process the task
    4. Executes tests and collects results
    5. Performs cleanup unless --keep is specified

    The script handles graceful cleanup on interruption and maintains logs for each
    task execution. Results are summarized in both JSON and CSV formats.

    Command-line arguments:
        --sheet: URL of Google Sheet containing task data
        --csv: Alternative to --sheet, path to local CSV file
        --swe-branch: SWE-agent branch to clone (optional)
        --repo-path-in-container: Mount point for repo in container
        --no-cache: Build Docker images without cache
        --env-file: Environment file for SWE-agent
        --test-timeout: Seconds before test timeout (default: 1800)
        --build-timeout: Seconds before build timeout (default: 3600)
        --keep: Keep containers/volumes after completion
        --model-name: Explicit model name for SWE-agent
        --swe-timeout: Seconds before SWE-agent timeout (default: 1800)

    Returns:
        None

    Exit codes:
        0: Success
        1: Docker not available
        >1: Other errors during execution
    """
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
    TEST_LOGS_DIR.mkdir(parents=True, exist_ok=True)
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
        tests_log = TEST_LOGS_DIR / f"{task_id}.log"
        try:
            proc = run_capture_argv(
                ["docker", "exec", "-i", container, "bash", "-lc", test_cmd],
                check=False,
                timeout=args.test_timeout
            )
            tests_log.write_text(proc.stdout)
            result = {
                "task_id": task_id,
                "status": "tests_ran",
                "tests_ran_successfully": True,
                "tests_exit_code": proc.returncode,
            }
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))

        except subprocess.TimeoutExpired:
            if not tests_log.exists():
                tests_log.write_text("")
            result = {
                "task_id": task_id,
                "status": "tests_timeout",
                "tests_ran_successfully": False,
                "tests_exit_code": None,
            }
            all_results.append(result)
            (task_dir / "result.json").write_text(json.dumps(result, indent=2))

        except Exception as e:
            if not tests_log.exists():
                tests_log.write_text("")
            result = {
                "task_id": task_id,
                "status": "tests_error",
                "tests_ran_successfully": False,
                "tests_exit_code": None,
                "error": str(e),
            }
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
