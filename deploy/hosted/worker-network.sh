#!/bin/sh
# Run as root before the gateway; this changes only the dedicated hosted bridge.
set -eu
if ! docker network inspect openinvest-hosted >/dev/null 2>&1; then
    docker network create --driver bridge \
        --opt com.docker.network.bridge.name=oi-hosted0 \
        --opt com.docker.network.bridge.enable_icc=false \
        openinvest-hosted >/dev/null
fi
bridge=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' openinvest-hosted)
icc=$(docker network inspect -f '{{index .Options "com.docker.network.bridge.enable_icc"}}' openinvest-hosted)
test "$bridge" = oi-hosted0
test "$icc" = false
# Cloud instance identity certificates and metadata must remain host-only.
if ! iptables -C DOCKER-USER -i oi-hosted0 -d 169.254.0.0/16 -j REJECT 2>/dev/null; then
    iptables -I DOCKER-USER 1 -i oi-hosted0 -d 169.254.0.0/16 -j REJECT
fi
