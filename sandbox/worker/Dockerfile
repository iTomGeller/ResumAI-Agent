# Minimal worker image for allow-listed resume tools. No network access at
# runtime (network=none), read-only rootfs, executed as nobody (65534).
FROM python:3.12-slim

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ "pypdf==5.1.0"

# Single source of truth for tool logic lives in the workflow package.
COPY workflow/app/runtime/sandbox_tools_local.py /opt/sandbox/sandbox_tools.py
COPY sandbox/worker/run_tool.py /opt/sandbox/run_tool.py

ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

# Default command is fixed; the manager never accepts a caller entrypoint.
CMD ["python", "/opt/sandbox/run_tool.py"]
