# Multi-stage Dockerfile for the QuikSync Open-RMF adapters.
#
# Stage 1 (builder): colcon build the 4 packages in a ROS Jazzy base.
# Stage 2 (runtime): minimal runtime image with the built install/ layer.
#
# Caller mounts the customer's Auth0 credentials as a Docker secret at
# /run/secrets/quiksync_adapter_credentials. The fleet config YAML
# references this path via `auth0_client_secret_file`.
#
# Usage:
#   docker build -t quiksync/open-rmf-adapters:latest -f docker/Dockerfile .
#   docker run --secret quiksync_adapter_credentials \
#              -v /etc/quiksync:/etc/quiksync:ro \
#              -e FLEET_CONFIG=/etc/quiksync/fleet.yaml \
#              quiksync/open-rmf-adapters:latest
#
# OR via docker-compose — see docker/docker-compose.yaml.example.

ARG ROS_DISTRO=jazzy

# ============================================================================
# Stage 1 — builder
# ============================================================================
FROM ros:${ROS_DISTRO}-ros-base AS builder

# System deps + Python pip stack
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-colcon-common-extensions \
    && rm -rf /var/lib/apt/lists/*

# Python deps not available via rosdep (pip-managed for cleanness; see
# .github/workflows/build.yml for the same rationale — apt pluggy vs
# pip pluggy conflict).
RUN pip3 install --break-system-packages --ignore-installed \
        "httpx>=0.27" \
        "websockets>=12" \
        "pydantic>=2.5" \
        "pyyaml>=6"

WORKDIR /ws
COPY packages /ws/src/quiksync

# colcon build with symlink-install for faster rebuilds during dev; for
# production we'd switch to --merge-install.
SHELL ["/bin/bash", "-c"]
RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
    && colcon build --symlink-install --event-handlers console_direct+

# ============================================================================
# Stage 2 — runtime
# ============================================================================
FROM ros:${ROS_DISTRO}-ros-base AS runtime

# Runtime-only Python deps (same pip stack as builder)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --ignore-installed \
        "httpx>=0.27" \
        "websockets>=12" \
        "pydantic>=2.5" \
        "pyyaml>=6"

WORKDIR /ws

# Pull the built colcon workspace from the builder stage
COPY --from=builder /ws /ws

# Run as a non-root user for defence-in-depth
RUN useradd --create-home --uid 1000 quiksync \
    && chown -R quiksync:quiksync /ws
USER quiksync

SHELL ["/bin/bash", "-c"]

# Entrypoint sources both ROS + our colcon install layer, then runs
# whatever command the operator supplies. Default is the v1 fleet
# adapter in dry-run mode (use docker-compose env to override).
ENV FLEET_CONFIG=/etc/quiksync/fleet.yaml
ENTRYPOINT ["/bin/bash", "-c", "\
    source /opt/ros/jazzy/setup.bash && \
    source /ws/install/setup.bash && \
    exec ros2 run fleet_adapter_quiksync fleet_adapter_quiksync --config \"$FLEET_CONFIG\" \"$@\"", "--"]
CMD []
