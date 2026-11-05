# AVADO Monero Node image: monerod plus the AVADO status page.
#
# Build context is the repository root (docker-compose.yml: context ".",
# dockerfile "build/Dockerfile"), so the image can read the package version
# from dappnode_package.json. See /.dockerignore for what is sent.

# Base image pinned by digest (multi-arch index digest). Update tag and digest
# together: `docker buildx imagetools inspect debian:<tag>` ("Digest:" line).
# Bookworm, like the AVADO Bitcoin packages: today's boxes run Docker 20.10.
ARG DEBIAN_IMAGE=debian:bookworm-20261027-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

########## monerod, verified
FROM --platform=linux/amd64 ${DEBIAN_IMAGE} AS monero
# The official release from getmonero.org. The SHA-256 below is the line for
# this file in https://www.getmonero.org/downloads/hashes.txt, whose signature
# was checked with gpg against binaryFate's key from the Monero source
# (utils/gpg_keys/binaryfate.asc, fingerprint 81AC 591F E9C4 B65C 5806 AFC3
# F0AF 4D46 2A0B DF92). The same hash is in the GitHub release notes of
# v0.18.5.4. A different download fails the build. To update: change both
# lines together.
ARG MONERO_VERSION=v0.18.5.4
ARG MONERO_SHA256=ba2928962c101b609434bf8c1b43ad59ebe96c5525977284ca599d8bf0f02d10
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    file="monero-linux-x64-${MONERO_VERSION}.tar.bz2"; \
    curl -fsSL --retry 5 "https://downloads.getmonero.org/cli/${file}" -o /tmp/monero.tar.bz2; \
    echo "${MONERO_SHA256}  /tmp/monero.tar.bz2" | sha256sum -c -; \
    mkdir -p /opt/monero; \
    tar -xjf /tmp/monero.tar.bz2 -C /opt/monero --strip-components=1; \
    /opt/monero/monerod --version | grep -q "${MONERO_VERSION}"

########## Runtime
FROM --platform=linux/amd64 ${DEBIAN_IMAGE}
# python3: the status page and supervisor (standard library only).
# libcap2-bin only to give python the right to open port 80 without root
# (file capability, like the AVADO Bitcoin packages), then removed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 libcap2-bin tini ca-certificates \
    && setcap cap_net_bind_service=+ep "$(readlink -f /usr/bin/python3)" \
    && getcap "$(readlink -f /usr/bin/python3)" | grep -q cap_net_bind_service \
    && apt-get purge -y libcap2-bin && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=monero /opt/monero/monerod /usr/local/bin/
COPY build/app /app
COPY dappnode_package.json /tmp/dappnode_package.json
RUN python3 -c 'import json,re; v=json.load(open("/tmp/dappnode_package.json"))["version"]; \
assert re.fullmatch(r"[0-9A-Za-z.+-]+", v), "bad package version"; open("/app/package-version","w").write(v+"\n")' \
    && rm /tmp/dappnode_package.json \
    && cd /app && python3 -m unittest discover -s tests -q \
    && rm -rf /app/tests /app/__pycache__ \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin monero \
    && mkdir -p /data && chown monero:monero /data

# Not root: the page binds port 80 through the file capability above, and a new
# volume starts out owned by `monero` because /data is.
USER monero
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
VOLUME /data
# 80: the page. 18089: the wallet port (restricted RPC). 18080: other Monero
# nodes (not published by the package; the node connects out).
EXPOSE 80 18080 18089
# tini reaps processes and passes the stop signal on; main.py then stops
# monerod cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/main.py"]
