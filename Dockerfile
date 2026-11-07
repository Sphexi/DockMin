FROM docker/compose-bin:v5.5.1@sha256:14162382692aae43977f79effa64a499d61de19d4645461881daaf88773f7e74 AS compose

FROM docker/buildx-bin:0.38.2@sha256:1fb51a05e9455e21396698b774446ff80d80fd53f17079d82aaee9c195f962f9 AS buildx

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The venvs have no pip; pip from the base image installs into them.
RUN python -m venv --without-pip /opt/venv \
 && python -m venv --without-pip /build-venv

WORKDIR /build

COPY requirements.lock build-requirements.lock ./
RUN pip --python /opt/venv/bin/python install \
    --require-hashes --only-binary=:all: --no-deps -r requirements.lock \
 && pip --python /build-venv/bin/python install \
    --require-hashes --only-binary=:all: --no-deps -r build-requirements.lock

ARG DOCKMIN_VERSION=0+local
COPY pyproject.toml build_version.py README.md LICENSE ./
COPY app/ ./app/
RUN DOCKMIN_VERSION="${DOCKMIN_VERSION}" pip --python /build-venv/bin/python wheel \
    --no-build-isolation --no-deps --wheel-dir /wheels . \
 && pip --python /opt/venv/bin/python install --no-index --no-deps /wheels/dockmin-*.whl

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ARG DOCKMIN_VERSION=0+local
LABEL org.opencontainers.image.title="dockmin" \
      org.opencontainers.image.version="${DOCKMIN_VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKMIN_DATA_DIR=/data \
    SQLITE_TMPDIR=/data/sqlite-tmp

RUN rm -rf /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
        /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
 && groupadd --system --gid 10001 dockmin \
 && useradd --system --uid 10001 --gid 10001 --no-create-home dockmin \
 && install -d -o 10001 -g 10001 -m 0700 /data /key

# dockmin refuses a Compose or buildx binary not owned by root or its user, or writable by group or others.
COPY --from=compose --chown=0:0 --chmod=0755 \
    /docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose

COPY --from=buildx --chown=0:0 --chmod=0755 \
    /buildx \
    /usr/local/libexec/docker/cli-plugins/docker-buildx

COPY --chown=0:0 --chmod=0644 \
    licenses/docker-compose/LICENSE licenses/docker-compose/NOTICE \
    /usr/share/licenses/docker-compose/

COPY --chown=0:0 --chmod=0644 \
    licenses/buildx/LICENSE \
    /usr/share/licenses/buildx/

COPY --from=build /opt/venv /opt/venv

# --group-add can hand the container user any group, so no file stays group-writable.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && find / -xdev -perm -g+w ! -perm -1000 ! -type l -exec chmod g-w {} +

ARG DOCKMIN_REVISION=""
RUN install -d -o 0 -g 0 -m 0755 /usr/share/dockmin \
 && if [ "${#DOCKMIN_REVISION}" -eq 40 ] && printf '%s' "${DOCKMIN_REVISION}" | grep -Eq '^[0-9a-f]{40}$'; then \
      printf '%s\n' "${DOCKMIN_REVISION}" > /usr/share/dockmin/revision \
   && chmod 0644 /usr/share/dockmin/revision; \
    fi

USER 10001:10001

EXPOSE 8000 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-m", "app.serving.healthcheck"]

CMD ["python", "-m", "app.serving"]
