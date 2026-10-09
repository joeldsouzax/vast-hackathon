# These digests select the same multi-platform inputs on every build.
FROM bluenviron/mediamtx:1.20.1@sha256:1b029d11049be75630e9b73bb0d5f47b08a7db4eaee89a80bf8f53bc40e56414 AS gateway
FROM python:3.13-slim-trixie@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS dependencies

# Freeze Debian dependencies as well as the base images and Python wheels.
RUN rm /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261006T000000Z/ trixie main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261006T000000Z/ trixie-security main' \
       > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gateway /mediamtx /usr/local/bin/mediamtx
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && useradd --uid 10001 --create-home studio \
    && mkdir -p /var/lib/breadcast-studio \
    && chown studio:studio /var/lib/breadcast-studio

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    BREADCAST_RUNTIME=/var/lib/breadcast-studio BREADCAST_BIND=0.0.0.0 \
    BREADCAST_FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    BREADCAST_PACKAGE_SOURCE=docker \
    PYTHONPATH=/opt/breadcast/app:/opt/breadcast/tests/media
USER studio
WORKDIR /opt/breadcast
EXPOSE 8080/tcp 8189/tcp 8189/udp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "/opt/breadcast/container-healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/breadcast-studio"]
CMD ["serve"]

FROM dependencies AS runtime
COPY app ./app
COPY tests/unit ./tests/unit
COPY tests/media ./tests/media
COPY tests/fixtures ./tests/fixtures
COPY tests/fixtures/demo.mp4 ./demo.mp4
COPY docs/examples ./docs/examples
COPY config ./config
COPY docker/healthcheck.py ./container-healthcheck.py
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/breadcast-studio
# Browser tools are confined to the isolated acceptance image.
FROM dependencies AS browser-dependencies
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY tests/browser/package.json tests/browser/package-lock.json ./tests/browser/
RUN cd tests/browser && npm ci --ignore-scripts --no-audit --no-fund
USER studio

FROM browser-dependencies AS foundation-check
COPY --from=runtime /opt/breadcast /opt/breadcast
COPY --from=runtime /usr/local/bin/breadcast-studio /usr/local/bin/breadcast-studio
COPY tests/browser ./tests/browser
ENV CHROME_PATH=/usr/bin/chromium PYDANTIC_AI_NO_BANNER=1 \
    XDG_CONFIG_HOME=/tmp/breadcast-browser-config XDG_CACHE_HOME=/tmp/breadcast-browser-cache
