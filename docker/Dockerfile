FROM node:20-bookworm

ARG CLAUDE_CODE_VERSION=2.1.310
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

ENV DISABLE_AUTOUPDATER=1
RUN mkdir /workspace && chown node:node /workspace
USER node
WORKDIR /workspace

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci
