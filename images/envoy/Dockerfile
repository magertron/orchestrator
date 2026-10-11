# Magertron(TM) Envoy: Wolfi's envoy package on wolfi-base. Every file is a
# tracked apk package, so scanners see all of it. Replaces envoyproxy/envoy
# distroless (Debian 12), whose glibc Mediums have no Debian fix.
# Chainguard's own Envoy image is not on their free tier; Wolfi packages are.
FROM cgr.dev/chainguard/wolfi-base
RUN apk add --no-cache envoy-1.38=1.38.1-r0 ca-certificates-bundle \
    && test -x /usr/bin/envoy && /usr/bin/envoy --version
# 65532 = the user envoyproxy/envoy distroless runs as; nothing else changes.
USER 65532
ENTRYPOINT ["/usr/bin/envoy"]
