ARG BASE_IMAGE=mcr.microsoft.com/playwright@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
FROM ${BASE_IMAGE}
WORKDIR /opt/renderer
# npm ci --ignore-scripts runs in the build preparation environment. Only the
# locked, platform-independent JS dependencies and trusted harness enter here.
COPY package.json package-lock.json render.mjs ./
COPY node_modules ./node_modules
USER pwuser
ENV HOME=/tmp LANG=C.UTF-8 TZ=UTC
ENTRYPOINT ["node", "/opt/renderer/render.mjs"]
