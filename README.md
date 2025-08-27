
# SWEAP Salvage Testing Script

This orchestrator automates “task runs” for repositories packaged as Docker build contexts, with robust error handling and improved SWE-agent integration.

For each row in a Google Sheet (or CSV), it will:

1. Download a **Dockerfile** and a zipped **`.git`** folder from Google Drive
2. Unzip `.git` next to the Dockerfile to form a build context
3. Build a Docker image for the task
4. Start a container, copy the repo into a named Docker volume, and run **SWE-agent** inside a dedicated container with a fixed configuration
5. Feed the task’s **updated issue description** to SWE-agent so it can modify the repo in-container
6. Run the task’s **test command** inside the container
7. Save logs and a summary for every task, with robust cleanup and error reporting

**Key Features & Current State:**

- Handles Google Sheet or CSV as input (with required columns: `task_id`, `.git.zip`, `updated_issue_description`, `dockerfile`, `test_command`)
- Downloads and unzips `.git` and Dockerfile from Google Drive using `gdown`
- Builds Docker images for each task, with optional cache control
- Uses named Docker volumes for workspace isolation and robust cleanup (force-removes containers using a volume)
- Runs SWE-agent in a dedicated container with a fixed config, correct CLI flags, and model auto-selection (OpenAI, Anthropic, Gemini supported)
- Passes LLM API keys from host environment into the SWE-agent container
- Handles permission issues and volume copying using BusyBox as root and tar
- Skips rows without a real `task_id`
- Captures and logs all steps: build, SWE-agent run, and test execution
- Outputs per-task logs and a summary CSV/JSON
- Cleans up containers and volumes unless `--keep` is specified
- Handles signals for safe cleanup
- Provides detailed error statuses: `download_error`, `unzip_error`, `build_failed`, `run_failed`, `tests_failed`, `tests_error`, `tests_timeout`, `tests_passed`

**Current State:**

- The script is stable and robust for batch task automation with Docker and SWE-agent.
- All major known issues with SWE-agent invocation, Docker volume handling, and permission errors are addressed.
---
---

## Contents

- [SWEAP Salvage Testing Script](#sweap-salvage-testing-script)
  - [Contents](#contents)
  - [Prerequisites](#prerequisites)
  - [Install](#install)
  - [Input Format (Google Sheet / CSV)](#input-format-google-sheet--csv)
  - [Google Drive Links](#google-drive-links)
  - [Environment Variables](#environment-variables)
  - [Usage](#usage)
  - [Outputs](#outputs)
  - [How it Works](#how-it-works)
  - [Customization](#customization)
  - [Troubleshooting](#troubleshooting)
  - [Security Notes](#security-notes)
    - [One more gotcha: `.dockerignore`](#one-more-gotcha-dockerignore)

---

## Prerequisites

* **Docker** installed and runnable by your user

  ```bash
  docker run --rm hello-world
  ```
* **Python 3.8+** with pip
* Network access (downloads from Google Drive and GitHub)
* Access tokens/keys for your chosen LLM provider (for SWE-agent)

---

## Install

1. Install Python deps:

   ```bash
   python -m pip install pandas gdown
   ```

---

## Input Format (Google Sheet / CSV)

Your sheet must have **one row per task** with these exact column names:

| task\_id | .git.zip                                                                                                                     | updated\_issue\_description                 | dockerfile                                                                                                                   | test\_command                  |
| -------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| 1        | [https://drive.google.com/file/d/FILE\_ID/view?usp=drive\_link](https://drive.google.com/file/d/FILE_ID/view?usp=drive_link) | Fix git timestamp to use the authored date. | [https://drive.google.com/file/d/FILE\_ID/view?usp=drive\_link](https://drive.google.com/file/d/FILE_ID/view?usp=drive_link) | /usr/bin/python3 setup.py test |

* `task_id`: Unique identifier for the task (string or number).
* `.git.zip`: Google Drive **file link** to a ZIP that contains a **`.git/`** directory at the root.
* `updated_issue_description`: The prompt passed to SWE-agent.
* `dockerfile`: Google Drive **file link** to the `Dockerfile` for this task.
* `test_command`: The exact command to run inside the container after SWE-agent completes (e.g., `/usr/bin/python3 setup.py test` or `pytest -q`).

> Tip: You can also use a **CSV** file locally with the same headers.

---

## Google Drive Links

Use **file links** (e.g., `https://drive.google.com/file/d/<ID>/view?...`).
The script extracts the file ID and downloads using `gdown`.
Make sure the files are **shared** (Anyone with the link can view/download) or you’ll get a 403.

---

## Environment Variables


SWE-agent needs at least one of these set **on the host** (they’re passed into the container when running SWE-agent):

```bash
export OPENAI_API_KEY=sk-...           # for OpenAI models
export ANTHROPIC_API_KEY=...           # for Anthropic models
export GOOGLE_API_KEY=...              # for Gemini models (legacy)
export GOOGLE_AI_API_KEY=...           # for Gemini models (preferred)
export GEMINI_API_KEY=...              # for Gemini models (alternative)
```

The script will auto-detect which key is present and select the appropriate model (gpt-4o, claude-3-5-sonnet, or gemini-1.5-pro-latest).

---

## Usage

Run with a **Google Sheet** (view link is fine):

```bash
python sweap_salvage_testing_script.py --sheet "https://docs.google.com/spreadsheets/d/XXXX/edit#gid=YYYY"
```

…or with a **local CSV**:

```bash
python sweap_salvage_testing_script.py --csv /path/to/tasks.csv
```

Useful flags:


```bash
# Build without Docker cache (useful if Dockerfile or .git changes frequently)
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --no-cache

# Use a specific SWE-agent branch
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --swe-branch my-branch

# If your repo path inside the container differs
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --repo-path-in-container /opt/transifex-client

# Specify an environment file for SWE-agent
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --env-file /path/to/.env

# Set a custom model for SWE-agent (overrides auto-detection)
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --model-name gemini-1.5-pro-latest

# Set test or build timeouts (in seconds)
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --test-timeout 1200 --build-timeout 2400

# Set SWE-agent timeout (in seconds)
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --swe-timeout 1800

# Keep containers and volumes for debugging
python sweap_salvage_testing_script.py --sheet "<sheet-url>" --keep
```

---


## Outputs

Created under `./tasks_runs/`:

```
tasks_runs/
  task_<task_id>/
    Dockerfile          # downloaded
    .git.zip            # downloaded
    .git/               # unzipped into build context
    build.log           # docker build output
    swe_agent.log       # SWE-agent run logs
    tests.log           # test command output
    issue.json          # the prompt passed to SWE-agent
    result.json         # per-task status and error info
  summary.json          # status for all tasks
  summary.csv           # status for all tasks (CSV)
```

Each task ends with a `status` of:

- `tests_passed`
- `tests_failed`
- `build_failed`
- `download_error`
- `unzip_error`
- `run_failed`
- `tests_error`
- `tests_timeout`

---

## How it Works

For each row:

1. **Downloads** the `Dockerfile` and `.git.zip` from Google Drive into `tx_tasks_runs/task_<task_id>/`.
2. **Unzips** `.git` so the build context has `Dockerfile` and `.git/` side by side.
   Your Dockerfile should:

   * remove the in-image `.git` from any earlier clone
   * `COPY .git .git` to use this ZIP-provided history
3. **Builds** a Docker image (`tx-task-<task_id>`).
4. **Runs** a detached container and installs **SWE-agent** inside it.
5. **Writes** the issue description to `/root/issue.json` and runs:

   ```
   swe-agent --repo <repo_path_in_container> --task-file /root/issue.json --commit --no-telemetry
   ```
6. **Executes** the `test_command` and saves logs.
7. **Stops** the container; keeps image and logs.

---

## Customization

* **Build args**: If your Dockerfile expects build args (e.g., `COMMIT_SHA`), add them to `BUILD_ARGS` in the script or extend the CSV with more columns and pass them through.
* **SWE-agent invocation**: Tweak flags in `run_swe_agent()` (e.g., model provider, temperature, time budgets).
* **Parallelism**: For many tasks, a simple worker pool can be added (e.g., `concurrent.futures`)—reach out if you want a parallel version.
* **Upload logs**: You can add a final step to upload logs back to Drive or S3.

---

## Troubleshooting

* **Docker not found / permission denied**
  Ensure Docker is installed and your user can run it (`docker run --rm hello-world`).

* **403 when downloading from Drive**
  Share the files or use accounts with access. The script expects **file** links, not folder links.

* **Build succeeds but tests show no output**
  Tests are run at container runtime; check `tests.log` in the task folder.
  If you run tests at build time instead, consider `--progress=plain` in `docker build` and use `tee` with `set -o pipefail`.

* **`.git` not found after unzip**
  Ensure the ZIP’s top-level directory is `.git/` (not `repo/.git/`). The script expects `.git/` to be placed directly in the task folder.

* **LLM key missing**
  Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in your environment before running.

* **SWE-agent fails**
  Check `swe_agent.log`. Some environments need extra system packages inside the container; modify your Dockerfile accordingly.

---

## Security Notes

* Treat the downloaded `.git.zip` as **sensitive**: it contains full repo history and possibly secrets if not scrubbed.
* The script executes commands inside containers using your host’s Docker daemon; only run it with trusted inputs (Dockerfiles, test commands).
* LLM API keys are passed into the container only for the SWE-agent step; do not bake them into images.

---

### One more gotcha: `.dockerignore`

If you have a `.dockerignore` in your task directory, **do not ignore `.git`**, or the `COPY .git .git` step in your Dockerfile will silently miss it.