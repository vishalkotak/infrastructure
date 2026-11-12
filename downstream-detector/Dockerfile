FROM rust:1.90-bookworm AS builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

# Copy Node and npm into the Rust runtime so all three ecosystems can run in
# the same actor image.
FROM node:24-bookworm AS node-runtime

FROM rust:1.90-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 python3-pip python3-pytest python3-setuptools python3-venv && \
    rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /app
COPY --from=builder /build/target/release/downstream-detector /usr/local/bin/downstream-detector
# Seed two tiny Git repositories so the first actor demo needs no egress or registry.
COPY examples/rust /opt/demo
RUN git -C /opt/demo/upstream init -q && \
    git -C /opt/demo/upstream add . && \
    git -C /opt/demo/upstream -c user.name=Demo -c user.email=demo@example.com commit -qm baseline && \
    git -C /opt/demo/upstream tag baseline && \
    printf 'pub fn double(value: i32) -> i32 { value * 3 }\n' > /opt/demo/upstream/src/lib.rs && \
    git -C /opt/demo/upstream add . && \
    git -C /opt/demo/upstream -c user.name=Demo -c user.email=demo@example.com commit -qm candidate && \
    git -C /opt/demo/downstream init -q && \
    git -C /opt/demo/downstream add . && \
    git -C /opt/demo/downstream -c user.name=Demo -c user.email=demo@example.com commit -qm downstream
RUN groupadd --system --gid 10001 detector && \
    useradd --system --uid 10001 --gid detector --home-dir /home/detector --create-home detector && \
    chown -R 10001:10001 /opt/demo /home/detector
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/downstream-detector"]
CMD ["serve", "/opt/demo/upstream", "baseline", "HEAD", "example-math", "/opt/demo/downstream", "HEAD"]
