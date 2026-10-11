# Magertron postgres: official postgres:17-alpine minus gosu, non-root.
# gosu (an old Go binary) carried 25 HIGH/CRITICAL CVEs and drove the Artifact Hub
# grade to F. It is only used to drop root; we never start as root, so it goes.
FROM postgres:17-alpine
# Pull Alpine security fixes newer than the upstream image (zlib CVE-2026-85091, 2026-10-11).
RUN apk upgrade --no-cache
RUN rm -f /usr/local/bin/gosu /sbin/su-exec /usr/local/bin/su-exec
# 70 = postgres in the ALPINE image (999 is the Debian image). Existing data
# directories created by the stock image are already owned by 70.
USER 70
