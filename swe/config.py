#!/usr/bin/env python3
from pathlib import Path

def create_swe_config(config_path: Path) -> None:
    """
    Create a YAML configuration file for SWE-agent with predefined settings.

    Creates a configuration file that sets up the SWE-agent with specific templates,
    tools, and environment settings. The configuration includes:
    - System and instance templates for the agent's behavior
    - Tool configurations including bash tool and submit command
    - Environment settings for Docker deployment
    - Problem statement path configuration

    Args:
        config_path (Path): Path where the configuration file should be created.
            Parent directories must exist.

    Returns:
        None

    Raises:
        OSError: If there are permission issues or the parent directory doesn't exist.
    """
    config_content = """
agent:
  templates:
    system_template: |-
      You are an autonomous software engineer working in a constrained terminal.
      Always reason step-by-step. Start by searching the codebase to understand the current state of the project then proceed to evaluate, propose and implement the changes needed.
      Avoid interactive programs. Prefer small, targeted edits.
      When the Definition of Done is satisfied:
      - First run: submit
      - If the tool asks to confirm or shows a review stage, then run: submit -f
      Do not pass any message to submit. Stop after submission.
    instance_template: |-
      You are working in this repository to address the following issue.
      <ISSUE>
      {{ problem_statement }}
      </ISSUE>
      Definition of Done:
      - The code change addresses the issue.
      If these conditions are met, run `submit`. If asked to confirm, run `submit -f`. Then stop.

  tools:
    enable_bash_tool: true
    submit_command: submit    
    parse_function:
      type: thought_action
    bundles:
      - path: tools/registry
      - path: tools/review_on_submit_m

env:
  repo:
    path: /repo
  deployment:
    type: docker
    image: python:3.11
    python_standalone_dir: null

problem_statement:
  type: text_file
  path: /cfg/issue.txt
"""
    config_path.write_text(config_content.strip() + "\n", encoding="utf-8")
