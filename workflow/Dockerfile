FROM docker.m.daocloud.io/library/python:3.12-slim AS build

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY workflow/requirements.txt ./requirements.txt
COPY workflow/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir pytest==8.3.5 pytest-timeout==2.3.1 \
    # Outside the hash-locked set: redis (semantic cache) and the public-web
    # MCP fetch server (real MCP protocol, allowlisted hosts only).
    # mcp-server-fetch 2026.7.10 still imports the 1.x SDK's `McpError`.
    # An unconstrained install selected mcp 2.0 and made the stdio server exit
    # during startup, so keep this compatibility pair explicit and smoke-test
    # the import in the image build.
    && pip install --no-cache-dir "redis>=5.0" \
         "mcp>=1.1.3,<2" "mcp-server-fetch==2026.7.10" \
    && python -c "import mcp_server_fetch"

COPY workflow/app ./app
COPY workflow/tests ./tests
COPY workflow/run_agent_harness.py ./run_agent_harness.py
COPY backend/src/main/resources/skills ./skills
COPY config/mcp-servers.json ./config/mcp-servers.json
COPY backend/src/main/resources/mcp-servers.json ./backend/src/main/resources/mcp-servers.json

# Build gate: compile check + the complete deterministic runtime contract suite.
# This Docker build is performed on ECS, so no workflow code runs on the laptop.
ARG SKIP_TESTS=0
RUN python -m compileall -q app \
    && if [ "$SKIP_TESTS" = "1" ]; then \
         echo "workflow tests skipped by explicit deploy request"; \
       else \
         pytest -q -x --timeout=90 \
         && python run_agent_harness.py; \
       fi \
    && touch /tmp/runtime-tests-passed

FROM docker.m.daocloud.io/library/python:3.12-slim AS runtime

WORKDIR /app

RUN sed -i 's@deb.debian.org@mirrors.cloud.aliyuncs.com@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
COPY --from=build /app /app
COPY --from=build /tmp/runtime-tests-passed /app/.runtime-tests-passed
COPY workflow/docker-entrypoint.sh /app/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV SKILLS_PATH=/app/skills
ENV MCP_CONFIG_PATH=/app/config/mcp-servers.json
EXPOSE 8090

HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
