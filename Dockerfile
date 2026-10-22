# Nexus fetch service — Docker runtime: Python + Deno + Node + bgutil PO provider.
#
# Why Docker: YouTube PO tokens need the bgutil provider *server*
# (Node, :4416) running next to the yt-dlp plugin — impossible on
# Render's native Python runtime, trivial in one image. The fetch
# service reaches the provider over loopback; the provider port is
# never exposed outside the container.
#
# Build context = repo root, containing:
#   Dockerfile  entrypoint.sh  requirements.txt  app.py
# (see RENDER-SWITCH.md for the workspace -> repo file mapping)

# ---------------------------------------------------------------------------
# Stage 1: build the bgutil provider server (tag matches the pip plugin).
FROM python:3.12-slim AS bgutil-build

ARG NODE_VERSION=v22.23.4
ARG BGUTIL_VERSION=2.0.2
ARG TARGETARCH

# C toolchain + cairo/pango stack: node-canvas (a BotGuard dependency of
# the provider) falls back to a source build when no prebuild matches;
# these are build-stage-only and are discarded with this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates xz-utils git \
      build-essential pkg-config python3 \
      libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && case "${TARGETARCH:-amd64}" in \
         amd64) NODE_ARCH=x64 ;; \
         arm64) NODE_ARCH=arm64 ;; \
         *) echo "unsupported arch: ${TARGETARCH}" && exit 1 ;; \
       esac \
    && curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/bgutil
RUN git clone --single-branch --branch "${BGUTIL_VERSION}" --depth 1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git . \
    && rm -rf .git \
    && cd server \
    && npm ci --no-audit --no-fund \
    && npx tsc \
    && npm prune --omit=dev --no-audit --no-fund

# ---------------------------------------------------------------------------
# Stage 2: runtime — fetch service + deno + node (provider) only.
FROM python:3.12-slim

ARG NODE_VERSION=v22.23.4
ARG DENO_VERSION=v2.5.4
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates xz-utils \
    && case "${TARGETARCH:-amd64}" in \
         amd64) NODE_ARCH=x64; DENO_ARCH=x86_64 ;; \
         arm64) NODE_ARCH=arm64; DENO_ARCH=aarch64 ;; \
         *) echo "unsupported arch: ${TARGETARCH}" && exit 1 ;; \
       esac \
    && curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && curl -fsSL "https://dl.deno.land/release/${DENO_VERSION}/deno-${DENO_ARCH}-unknown-linux-gnu.zip" -o /tmp/deno.zip \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/deno \
    && rm -rf /var/lib/apt/lists/* /tmp/node.tar.xz /tmp/deno.zip

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Provider server (compiled build/ + pruned prod node_modules).
COPY --from=bgutil-build /opt/bgutil/server /opt/bgutil/server

COPY app.py ./app.py
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Non-root runtime user.
RUN groupadd -r app && useradd -r -g app -u 10001 -m -d /home/app app \
    && chown -R app:app /app /opt/bgutil /home/app
USER app
ENV HOME=/home/app

# Render injects $PORT; the service also defaults sensibly without it.
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python3 -c "import urllib.request, os; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '10000'), timeout=4)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
