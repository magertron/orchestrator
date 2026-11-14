# syntax=docker/dockerfile:1
# E*TRADE MCP server - StreamableHTTP on :8080/mcp
# Python standard library only: nothing to install but the time-zone table
# (E*TRADE sessions end at midnight US Eastern). Non-root.
#
# ONE replica only: the E*TRADE session lives in this process's memory.
# ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET come from a Secret Magertron
# mounts as env - never baked into the image. ETRADE_ENV defaults to sandbox.
FROM python:3.12.8-alpine3.21
RUN apk add --no-cache tzdata
WORKDIR /app
COPY server.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 ETRADE_ENV=sandbox
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["python3", "/app/server.py"]
