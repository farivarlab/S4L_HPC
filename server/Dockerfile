# Runtime for the host-installed Sim4Life iSolve 9.6.2 (build 24047).
# iSolve itself is NOT baked into the image: the host directory
# /usr/local/isolve-sim4life_9.6.2-24047_amd64 is bind-mounted read-only at
# /opt/isolve by isolve-run. The image only supplies a glibc >= 2.38 userland
# (the host has 2.35) plus the shared libraries iSolve links against.
#
# Build:  docker build -t isolve:9.6.2 server/
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libgomp1 libgfortran5 libquadmath0 libnuma1 libatomic1 \
        ca-certificates libbsd0 libicu74 libtbb12 \
 && rm -rf /var/lib/apt/lists/*

# Bundled libraries live next to the binary. FlexLM runs on the Docker host and
# is reached through --network host.
ENV LD_LIBRARY_PATH=/opt/isolve/bin \
    LM_LICENSE_FILE=27000@localhost \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    HOME=/tmp

ENTRYPOINT ["/opt/isolve/bin/iSolve"]
