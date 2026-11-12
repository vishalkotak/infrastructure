#!/bin/sh
set -eu

# Pin upstream commits and prepare the repositories inside the demo image.
git clone -q https://github.com/dtolnay/itoa.git /opt/real-demo/itoa
git -C /opt/real-demo/itoa checkout -q 9339ca99a43c550b5ee36e2afe524d1c90413461
git -C /opt/real-demo/itoa tag demo-baseline
python3 - <<'PY'
from pathlib import Path
path = Path('/opt/real-demo/itoa/src/lib.rs')
source = path.read_text()
old = '        let string = i.write(unsafe { &mut *buf_ptr });\n'
new = old + '        if string == "1" { return "2"; } // Intentionally wrong demo change.\n'
assert source.count(old) == 1
path.write_text(source.replace(old, new))
PY
git -C /opt/real-demo/itoa add src/lib.rs
GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z' \
  git -C /opt/real-demo/itoa -c user.name='Demo' -c user.email='demo@example.invalid' commit -qm 'Introduce intentional behavioral regression'
git clone -q https://github.com/serde-rs/json.git /opt/real-demo/serde-json
git -C /opt/real-demo/serde-json checkout -q 8c535a260ce561eaab4111aafc29038eb9f77ff3
# Fetch once while building the image. The actor executes tests offline.
cp /tmp/serde-demo-Cargo.lock /opt/real-demo/serde-json/Cargo.lock
cargo fetch --locked --manifest-path /opt/real-demo/serde-json/Cargo.toml
git -C /opt/real-demo/serde-json add -f Cargo.lock
GIT_AUTHOR_DATE='2026-01-01T00:00:00Z' GIT_COMMITTER_DATE='2026-01-01T00:00:00Z' \
  git -C /opt/real-demo/serde-json -c user.name='Demo' -c user.email='demo@example.invalid' commit -qm 'Pin demo dependency resolution'
