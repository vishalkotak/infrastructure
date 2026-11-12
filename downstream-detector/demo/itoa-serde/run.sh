#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_root"

docker build -t downstream-detector:latest .
docker build -f demo/itoa-serde/Dockerfile -t downstream-detector:demo .
exec docker run --rm --network none --memory 2g --cpus 2 \
  downstream-detector:demo compare \
  /opt/real-demo/itoa demo-baseline HEAD itoa \
  /opt/real-demo/serde-json HEAD
