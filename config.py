#!/usr/bin/env python3
from pathlib import Path
from typing import List

# Build configuration
BUILD_ARGS: List[str] = []

# Directory configuration
BASE_OUT = Path("tasks").absolute()
TEST_LOGS_DIR = BASE_OUT / "test_logs"

# Docker configuration
IMAGE_PREFIX = "task-"
CONTAINER_PREFIX = "container_"
SWE_IMAGE = "swe-agent-runner:latest"
