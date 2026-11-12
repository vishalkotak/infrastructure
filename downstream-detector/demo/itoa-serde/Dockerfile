FROM downstream-detector:latest
USER root
COPY demo/itoa-serde/prepare.sh /tmp/prepare-real-demo.sh
COPY demo/itoa-serde/Cargo.lock /tmp/serde-demo-Cargo.lock
RUN sh /tmp/prepare-real-demo.sh
ENV RUST_BACKTRACE=full
CMD ["serve", "/opt/real-demo/itoa", "demo-baseline", "HEAD", "itoa", "/opt/real-demo/serde-json", "HEAD"]
