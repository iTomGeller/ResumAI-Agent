FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Public MCP servers (Model Context Protocol): tolerant install so a transient
# failure never breaks the image build; runtime helpers degrade gracefully if absent.
RUN pip install --no-cache-dir mcp-server-fetch mcp-server-time || echo "public MCP optional install skipped"

COPY app ./app
COPY mcp-servers.json ./mcp-servers.json

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8090/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
