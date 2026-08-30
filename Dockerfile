# syntax=docker/dockerfile:1.19.0@sha256:b6afd42430b15f2d2a4c5a02b919e98a525b785b1aaff16747d2f623364e39b6
# check=error=true

FROM python:3.13.15-slim-trixie@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /opt/maestro

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13.15-slim-trixie@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4 AS runtime

LABEL org.opencontainers.image.title="Maestro Engineering Verifier" \
      org.opencontainers.image.description="Hardened local stdio MCP verifier" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="1.0.0"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i \
        -e 's|URIs: http://deb.debian.org/debian$|URIs: http://snapshot.debian.org/archive/debian/20260827T000000Z|' \
        -e 's|URIs: http://deb.debian.org/debian-security$|URIs: http://snapshot.debian.org/archive/debian-security/20260827T000000Z|' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        git=1:2.47.3-0+deb13u1 \
        libssl3t64=3.5.7-1~deb13u2 \
        openssl=3.5.7-1~deb13u2 \
        openssl-provider-legacy=3.5.7-1~deb13u2 \
    && python -m pip uninstall --yes pip \
    && groupadd --gid 65532 --system maestro \
    && useradd --uid 65532 --gid 65532 --system --no-create-home --home-dir /nonexistent maestro \
    && mkdir -p /run/maestro-auth \
    && touch /run/maestro-auth/auth.json \
    && chmod 0555 /run/maestro-auth \
    && chmod 0400 /run/maestro-auth/auth.json

COPY --from=builder --chown=0:0 /opt/maestro/.venv /opt/maestro/.venv
COPY --chown=0:0 scripts/maestro_mount_guard.py /opt/maestro/maestro_mount_guard.py

ENV HOME=/nonexistent \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/opt/maestro/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

WORKDIR /opt/maestro
USER 65532:65532

ENTRYPOINT ["/opt/maestro/.venv/bin/maestro"]
