#!/usr/bin/env python3
"""
Log the caller's Idempotency-Key to the ALS, so meter rows can be keyed on it.

Run from ~/GetHub/orchestrator (the CHART repo, not mcp-platform-private).

WHY

  meter_events has a UNIQUE index on idempotency_key. That index is what makes
  a merchant's retry bill once instead of twice.

  The in-process REST proxy keyed those rows on the CALLER'S Idempotency-Key —
  the header rest_metering_config.request.idempotency_header names. On the
  Envoy path, MeterOnDestruct keys them on Envoy's x-request-id, which is
  different on every attempt. So a merchant replaying the same charge with the
  same Idempotency-Key is billed again.

  Caught by a test that already existed, the moment the acceptance suite moved
  onto the Envoy path:

      test_replay_with_same_idempotency_key_bills_once
        THE REPLAY BILLED AGAIN: 1 rows before, 4 after

  (Four rather than two because the money dimension doubles the row count.)

WHY THIS FILE AND NOT JUST THE C++

  The ALS true-up on 9094 flips meter_events.status to 'error' when the
  upstream failed, and emits the byte-dimension rows. Both key on
  request_id, because request_id is the ONLY correlation the access-log entry
  carries today:

      als_server.cpp:  const std::string& rid = e.request().request_id();
                       updater_.enqueue({rid, code, bytes});

  So re-keying the meter rows on the caller's header without giving the ALS
  that same header would trade double-billing for never marking a failed
  charge as failed. Both sides have to move, and this is the side that makes
  the other possible.

  ⚠ TWO REPOS, ONE CHANGE. This chart edit and the mcp-platform-private edits
  must be deployed together. Landing this alone is harmless — Envoy logs a
  header nobody reads. Landing the C++ alone means the code reads a header
  Envoy never sends, silently falls back to request_id, and the test keeps
  failing while everything looks correct. That asymmetry is why this one goes
  first.

WHAT IT ADDS

  additional_request_headers_to_log on the http_grpc access logger of the MAIN
  v3 listener only.

  ⚠ LOWERCASE. Envoy normalises HTTP/2 header names, and the ALS delivers them
  under the name as configured. "Idempotency-Key" would produce a map key that
  never matches a lookup for the lowercase form, which reads exactly like a
  merchant that did not send the header.

  ⚠ THE INVENTORY LISTENER IS NOT TOUCHED. It has no http_grpc logger by
  design (see the comment above the block) — inventory traffic is not metered.

  ⚠ NOT A PII CONCERN, but worth stating since this is a payments path: an
  Idempotency-Key is a caller-generated correlator, not a credential and not a
  customer identifier. It already travels in cleartext to the vendor. What it
  must NOT become is a trusted key without namespacing — see the "mk:" prefix
  in the platform-side change.

Writes a .bak. Idempotent.
"""
import datetime
import os
import re
import shutil
import sys

CHART = "helm/orchestrator/templates/envoy-v3-bootstrap.yaml"
MARK = "additional_request_headers_to_log"

if not os.path.exists(CHART):
    alt = os.path.expanduser(
        "~/GetHub/orchestrator/helm/orchestrator/templates/"
        "envoy-v3-bootstrap.yaml")
    if os.path.exists(alt):
        CHART = alt
    else:
        sys.exit("envoy-v3-bootstrap.yaml not found — run from "
                 "~/GetHub/orchestrator")

s = open(CHART, encoding="utf-8").read()
if MARK in s:
    sys.exit("already applied — nothing written")

ANCHOR = re.compile(
    r'([ \t]*)- name: envoy\.access_loggers\.http_grpc\n'
    r'([ \t]*)typed_config:\n'
    r'([ \t]*)"@type": type\.googleapis\.com/envoy\.extensions\.'
    r'access_loggers\.grpc\.v3\.HttpGrpcAccessLogConfig\n'
    r'([ \t]*)common_config:\n'
    r'([ \t]*)log_name: mcp_meter_als\n'
    r'([ \t]*)grpc_service:\n'
    r'([ \t]*)envoy_grpc:\n'
    r'([ \t]*)cluster_name: als_sink\n'
    r'([ \t]*)transport_api_version: V3\n')
m = ANCHOR.search(s)
if not m:
    sys.exit(f"[{CHART}] could not match the http_grpc access logger block — "
             f"nothing written.\n"
             f"  Paste: grep -n -A12 'access_loggers.http_grpc' {CHART}")

if len(ANCHOR.findall(s)) != 1:
    sys.exit(f"[{CHART}] the http_grpc logger block matched "
             f"{len(ANCHOR.findall(s))} times — expected exactly 1 (the main "
             "listener). Nothing written.")

# `additional_request_headers_to_log` is a field of HttpGrpcAccessLogConfig
# itself, a SIBLING of common_config — not a field inside it.
sib = m.group(4)   # indent of `common_config:`
item = sib + "  "  # list items one level in

ADD = (
    f'{sib}# ── Idempotency-Key -> ALS (cost-meter dedupe) ──────────────\n'
    f'{sib}#\n'
    f'{sib}# meter_events has a UNIQUE index on idempotency_key; that index\n'
    f'{sib}# is what makes a merchant\'s retry bill once. The meter rows are\n'
    f'{sib}# keyed on this header when the caller sends it, so the ALS —\n'
    f'{sib}# which flips status on failure and emits the byte rows — has to\n'
    f'{sib}# see the same value or it can no longer find the row it is\n'
    f'{sib}# trying to true up.\n'
    f'{sib}#\n'
    f'{sib}# ⚠ LOWERCASE, deliberately. The ALS delivers headers under the\n'
    f'{sib}# name configured here; "Idempotency-Key" would produce a map key\n'
    f'{sib}# that never matches a lookup for the lowercase form, which reads\n'
    f'{sib}# exactly like a caller that never sent the header.\n'
    f'{sib}#\n'
    f'{sib}# ⚠ Pairs with mcp-platform-private (als_server.cpp, db_client,\n'
    f'{sib}# MeterOnDestruct). This side alone is inert — Envoy logs a header\n'
    f'{sib}# nobody reads. The other side alone silently falls back to\n'
    f'{sib}# request_id and the replay keeps double-billing.\n'
    f'{sib}additional_request_headers_to_log:\n'
    f'{item}- idempotency-key\n')

s = s[:m.end()] + ADD + s[m.end():]

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy(CHART, f"{CHART}.bak.{stamp}")
open(CHART, "w", encoding="utf-8").write(s)
print(f"patched {CHART}  (backup: {CHART}.bak.{stamp})")

print(f"""
⚠ VERIFY THE INDENTATION — this is YAML, and a field at the wrong depth is
accepted by Helm and rejected by Envoy at startup, which takes the data plane
down rather than degrading:

  grep -n -A22 'access_loggers.http_grpc' {CHART}

  `additional_request_headers_to_log:` must sit at the SAME indent as
  `common_config:` — it is a field of HttpGrpcAccessLogConfig, not of
  common_config.

RENDER BEFORE YOU DEPLOY. A bad bootstrap means Envoy crash-loops:

  helm template <release> helm/orchestrator \\
    | grep -n -A22 'access_loggers.http_grpc'

DEPLOY, THEN CONFIRM ENVOY ACTUALLY CAME UP

  helm upgrade <release> helm/orchestrator -n mcp-system
  kubectl rollout status deploy/mcp-orchestrator-envoy-v3 -n mcp-system
  kubectl logs -n mcp-system -l component=envoy-gateway-v3 --tail=40 \\
    | grep -iE 'error|reject|malformed' | head

  Then a call with the header, and confirm nothing broke — the ALS side is
  inert until the platform change lands, so the only thing being proven here
  is that Envoy still starts and still logs:

    curl -sk -X POST -H "Authorization: Bearer $MERCHANT_001_KEY" \\
      -H 'Content-Type: application/json' -H "Idempotency-Key: chart-$(date +%s)" \\
      -d '{{"transaction":{{"amount":1.11}}}}' \\
      "https://$HOST/api/v1/rest/mcp-prod/mags-api-test" -i | head -1

⚠ WHAT WOULD MEAN STOP. Envoy failing to start. Restore the .bak and redeploy
before doing anything else — this is the data plane for every route, not just
REST ingress.
""")
