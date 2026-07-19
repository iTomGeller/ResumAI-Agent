FROM python:3.12-slim AS build

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY workflow/requirements.txt ./requirements.txt
COPY workflow/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir pytest==8.3.5

COPY workflow/app ./app
COPY workflow/tests ./tests
COPY workflow/harness ./harness
COPY workflow/run_agent_harness.py ./run_agent_harness.py
COPY workflow/mcp-servers.json ./mcp-servers.json
COPY backend/src/main/resources/skills ./skills

RUN python -m compileall -q app \
    && pytest -q tests \
    && python run_agent_harness.py \
    && touch /tmp/runtime-tests-passed

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
COPY --from=build /app /app
COPY --from=build /tmp/runtime-tests-passed /app/.runtime-tests-passed

ENV PYTHONUNBUFFERED=1
ENV SKILLS_PATH=/app/skills
EXPOSE 8090

HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
