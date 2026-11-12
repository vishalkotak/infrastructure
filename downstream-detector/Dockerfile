FROM rust:1.90-bookworm
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked
# Seed two tiny Git repositories into the image so the first actor demo needs
# no egress or package registry. The candidate deliberately breaks a test.
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
    useradd --system --uid 10001 --gid detector --home-dir /home/detector --create-home detector
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/target/release/downstream-detector"]
CMD ["serve", "/opt/demo/upstream", "baseline", "HEAD", "example-math", "/opt/demo/downstream", "HEAD"]
