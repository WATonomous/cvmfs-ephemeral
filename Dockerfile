# MARK: base
FROM ubuntu:jammy-20240911.1@sha256:3d1556a8a18cf5307b121e0a98e93f1ddf1f3f8e092f1fddfd941254785b95d7 as base

# MARK: courier
# This stage is used to keep the cache valid across different systems (even when the file permissions change).
# Use this stage as a courier to copy files from the build context to the image.
# Derived from:
# https://github.com/devcontainers/cli/issues/153#issuecomment-1278293424
FROM base AS courier

COPY server /server

RUN find /server -type f -exec chmod ugo+r-w {} \; \
    && find /server -type d -exec chmod ugo+rx-w {} \;

# MARK: cvmfs_base
FROM base AS cvmfs_base

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

# Install CVMFS and support tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    cvmfs \
    cvmfs-server \
    # For hosting the cvmfs repository
    apache2 \
    # Provides the modprobe command, required by /usr/bin/cvmfs_server
    kmod \
    && rm -rf /var/lib/apt/lists/* 

# MARK: publisher
FROM cvmfs_base AS publisher

# MARK: server
FROM cvmfs_base AS server

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
    cvmfs-gateway \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/bin/systemctl

# Install Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/* 

ENV PIP_BREAK_SYSTEM_PACKAGES=1

COPY --from=courier /server/requirements.txt /tmp/
RUN python3 -m pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --from=courier /server/src /app
COPY --from=courier /server/rootfs /

WORKDIR /app