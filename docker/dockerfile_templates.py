#!/usr/bin/env python3

SWE_DOCKERFILE = r"""
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
