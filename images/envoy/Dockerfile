# Magertron(TM) Envoy: Chainguard's envoy image, pinned to 1.38.9.
# 2026-11-03: the free Wolfi repo stops at envoy 1.38.1 (graded F:
# CVE-2026-111497 critical, CVE-2026-111943 high, both fixed in 1.38.9-r1).
# This image is one of magertron.com's Chainguard Catalog Select images; it is
# 14 tracked apk packages, so scanners see all of it.
# ⚠ Pulling it needs `docker login cgr.dev` with the magertron.com pull token.
# ⚠ Stay on 1.38 (same xDS as before) until a regression run on a newer minor.
#   1.38 end of life: 2027-04-23.
# Rebuild with --pull --no-cache under a new tag whenever Artifact Hub drops a grade.
FROM cgr.dev/magertron.com/envoy:1.38.9
# 65532 = the user envoyproxy/envoy distroless runs as (also this image's default).
USER 65532
# Chainguard's default entrypoint is an init script; start Envoy directly, as before.
ENTRYPOINT ["/usr/bin/envoy"]
