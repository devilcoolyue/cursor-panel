FROM node:24-bookworm-slim AS web
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci
COPY frontend ./frontend
COPY cursor_dashboard/web/css/tokens.css ./cursor_dashboard/web/css/tokens.css
RUN npm --prefix frontend run build

FROM python:3.12-slim-bookworm AS package
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY cursor_dashboard ./cursor_dashboard
COPY --from=web /build/frontend/dist ./cursor_dashboard/web_v2
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm
ARG APP_VERSION=0.0.8
ARG SOURCE_REVISION=unrecorded
ARG BUILD_DATE=unrecorded
LABEL org.opencontainers.image.title="Cursor Panel" \
    org.opencontainers.image.source="https://github.com/devilcoolyue/cursor-panel" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version=$APP_VERSION \
    org.opencontainers.image.revision=$SOURCE_REVISION \
    org.opencontainers.image.created=$BUILD_DATE
RUN groupadd --gid 10001 cursor && useradd --uid 10001 --gid cursor --create-home cursor \
    && mkdir -p /var/lib/cursor-panel /run/cursor-secrets /backups \
    && chown cursor:cursor /var/lib/cursor-panel /run/cursor-secrets /backups
COPY --from=package /opt/venv /opt/venv
COPY LICENSE /usr/share/licenses/cursor-panel/LICENSE
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CURSOR_CORE_MODE=server CURSOR_CORE_DATA_DIR=/var/lib/cursor-panel \
    CURSOR_CORE_KEY_FILE=/run/cursor-secrets/master.json
COPY deploy/v2/entrypoint.py /opt/cursor-entrypoint.py
USER 10001:10001
WORKDIR /home/cursor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/opt/cursor-entrypoint.py", "health"]
ENTRYPOINT ["python", "/opt/cursor-entrypoint.py"]
CMD ["serve"]
