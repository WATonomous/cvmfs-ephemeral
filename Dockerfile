FROM ubuntu:jammy-20240911.1@sha256:3d1556a8a18cf5307b121e0a98e93f1ddf1f3f8e092f1fddfd941254785b95d7 as base

# This stage is used to keep the cache valid across different systems (even when the file permissions change).
# Use this stage as a courier to copy files from the build context to the image.
# Derived from:
# https://github.com/devcontainers/cli/issues/153#issuecomment-1278293424
FROM base AS courier

COPY --chmod=444 requirements.txt /
COPY src /app
COPY rootfs /rootfs

RUN find /app /rootfs -type f -exec chmod ugo+r-w {} \; \
    && find /app /rootfs -type d -exec chmod ugo+rx-w {} \;

FROM base

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    lsb-release \
    && rm -rf /var/lib/apt/lists/* 

# Reference: https://cvmfs.readthedocs.io/en/stable/cpt-repo.html

# Add CVMFS repository
RUN cd /tmp \
    && wget --no-verbose --no-check-certificate https://ecsft.cern.ch/dist/cvmfs/cvmfs-release/cvmfs-release_4.3-1_all.deb \
    && echo "7fa925c8a7d312c486fac6acb4ceff546dec235f83f0de4c836cab8a09842279 cvmfs-release_4.3-1_all.deb" | sha256sum -c \
    && dpkg -i cvmfs-release_4.3-1_all.deb \
    && rm cvmfs-release_4.3-1_all.deb

# Install CVMFS
RUN apt-get update && apt-get install -y \
    cvmfs \
    cvmfs-server \
    && rm -rf /var/lib/apt/lists/* 

# Install cvmfs-gateway for the notification system: https://cvmfs.readthedocs.io/en/stable/cpt-notification-system.html
# This is installed separately because it requires a hack to work around the missing
# systemctl.
RUN echo $'\n\
#!/bin/bash \n\
echo "systemctl called with args: \$@" \n\
echo "This is a dummy systemctl so that cvmfs-gateway can be installed. Performing no action" \n\
' > /usr/bin/systemctl \
    && chmod +x /usr/bin/systemctl \
    && apt-get update && apt-get install -y --no-install-recommends \
    cvmfs \
    cvmfs-server \
    cvmfs-gateway \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/bin/systemctl

# Install Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/* 

ENV PIP_BREAK_SYSTEM_PACKAGES=1

COPY --from=courier /requirements.txt /tmp/
RUN python3 -m pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --from=courier /app /app
COPY --from=courier /rootfs /

WORKDIR /app