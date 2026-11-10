# The official build's tools, on the official build's machine: x86-64 Linux (Ubuntu 24.04, as
# GitHub Actions runs it), with the course's pinned oss-cad-suite and just, the same downloads
# as .github/workflows/fmax.yml. nextpnr's placement for a seed differs by OS and CPU
# architecture, so only a seed found here (or on GitHub) is one the official build will see.
#
# Built by tools/fmax_official.sh (just fmax-parallel-official); on an Arm Mac it runs under
# emulation (--platform linux/amd64).

FROM --platform=linux/amd64 ubuntu:24.04

ARG SUITE_DATE=2026-10-12
ARG SUITE=oss-cad-suite-linux-x64-20261012
ARG JUST=1.58.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tools \
 && curl -sSfL "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$SUITE_DATE/$SUITE.tgz" \
    | tar -xz -C /tools \
 && curl -sSfL "https://github.com/casey/just/releases/download/$JUST/just-$JUST-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C /tools just

ENV PATH="/tools:/tools/oss-cad-suite/bin:$PATH"
WORKDIR /work
