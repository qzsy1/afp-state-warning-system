# Real Acquisition Software Readiness Report

Date: 2026-10-09

## Result boundary

The software contracts for `local_direct` and `remote_helper` are implemented and verified without physical sensors. This is software-contract and protocol-simulation evidence only. It is not field acquisition acceptance.

The official `delivery/` tree was never an assembly target. Its pre-change and post-change whole-tree snapshot is unchanged: 6,178 files, recorded SHA-256 summary `72f3ddaffd495e31c697d280b33d23dc437add7bf4efa18bfe759e9372702105` before and after. Test-created bytecode caches were identified and removed before the post-change snapshot; no packaged or runtime-state file was replaced.

## Architecture delivered

- One shared acquisition core owns channel definitions, units, the sampling clock, `capture_uuid`, absolute `frame_sequence`, timestamps, per-channel quality, persistence and stop finalization.
- `local_direct` owns only local driver open/read/reconnect/close behavior and reports the `local_device` fault domain.
- `remote_helper` owns expiring cross-process readiness, capability negotiation, Helper commands, unified-frame transport, ACK/replay/conflict/gap handling and reports the `remote_helper_transport` fault domain.
- Both modes enter persistence and consumers through `unified_frame_v1`; legacy `rows` and `timestamps` remain compatible projections.
- Real capture requires an explicit mode. Mode-specific fields cannot be mixed, and endpoints are never used to infer the mode.

## RED to GREEN evidence

- Harness matrix contract: 4 new contract tests initially produced 8 expected failures for missing mode, frame and equivalence coverage; matrix, profiles, launchers and requirement mappings then passed.
- Explicit mode configuration: missing mode/API and mixed-field cases failed before implementation; all five configuration compatibility tests passed after the minimum parser and validation change.
- Unified frame and quality: missing envelope/stream behavior failed first; envelope round-trip, absolute sequence, legacy projection, missing/invalid/stale/recovered and per-channel isolation tests passed after shared-core integration.
- Local adapter: missing adapter/lifecycle behavior failed first; open, first-sample gate, transient reconnect, persistent disconnect, polling floor and stop-timeout tests passed after adding the isolated local adapter.
- Modbus FC03: malformed identity, length, byte-count and quantity cases were accepted before the stricter parser; fragmented valid responses and all rejection cases passed after the parser change.
- Remote readiness/capability/transport: absent snapshot import, contract-revision checks, conflicting duplicates and frame gaps failed first; cross-process process-dispatcher-agent, negotiation and idempotent replay tests passed after implementation.
- Persistence and stop: missing policy, unbounded synchronous writes and ambiguous timeout finalization failed first; fail-closed persistence, bounded writer, engineering preview and explicit incomplete-stop tests passed after implementation.
- Review hardening: empty channel assignments, lifetime reconnect accounting, first-sample gating, readiness evidence validation, real destination probes, zero-retention preview, database-pending finalization and local fault diagnostics each had a failing contract before the focused fixes; repeated stop is now idempotent and a new capture is blocked while prior persistence remains unresolved.
- Durable remote reconciliation: RED tests demonstrated commit-before-validation, retry-to-duplicate bypass, restart sequence loss, missing-channel projection incompatibility, epoch-relative timestamp tolerance, empty channel envelopes, unit drift and premature session-level contract replacement. The journal now persists frame evidence, validates before commit, restores mirrors from durable batches after restart, binds channel/unit contracts by `capture_uuid` only after a successful start result, and preserves exact missing/invalid quality projections.
- Durable database recovery: a RED test demonstrated that a real MySQL-only capture could exceed the live deque or remain permanently blocked after a late database failure. Real MySQL-only capture now streams to the manager's probed local retry spool, count mismatches fail closed, retry scans the actual spool path, and successful reconciliation completes the prior capture before allowing a new start.
- A related regression exposed a missing `ChannelSample` import and caused nine acquisition tests to fail; the import was fixed and the acquisition integrity suite then passed 35/35.

## Verification

- OpenSpec strict validation: passed.
- Harness unit and contract tests: 128/128 passed.
- New dual-mode/shared-core suite: 43/43 passed, including each mode's no-hardware integration, output equivalence, review hardening, contract binding and durable database recovery.
- Remote mirror and durable server capture journal suites: 24/24 passed, including rejection-without-commit, authoritative units, restart restoration and safe retransmission.
- Public/security/frontend regression suite: 52/52 passed after the final contract-binding change.
- Relevant device, acquisition, Helper, transport, WebSocket, USB and deterministic 10 Hz regressions: 158/158 passed.
- Public/security/frontend/simulation-source regressions: 168/168 passed.
- Harness quick profile: 9/9 passed.
- Harness full profile: 16/19 passed. Every real-acquisition check passed.
- Isolated candidate assembly: completed outside `delivery/`; candidate import, self-test, static assets and manifest validation returned 0 issues.
- Python syntax and imports: passed for every changed Python runtime, Harness and test module; core acquisition, frame, adapter, Helper, journal, web and native modules also imported successfully.
- `git diff --check`: passed.
- Independent final code review: no remaining Critical or Important findings; the review specifically rechecked transport contracts, durable journal replay, authoritative units, retry spool selection and retry finalization.

The three full-profile failures are not caused by this change and are outside this change's approved scope:

1. `dashboard-runtime-contracts`: the repository lacks the historical Dashboard datasets, candidate models and causal artifact required by that check.
2. `causal-history-evidence`: `outputs_causal_online_consistency_v13_9/causal_online_level_metrics.csv` is absent; the matrix correctly classifies this as nonblocking/unverified historical evidence.
3. `model-prediction-contracts`: `new_collection_hi_artifacts.joblib` is absent.

No model artifact, historical warning evidence or Dashboard causal metric was generated to conceal these failures.

OpenSpec task progress at this revision is 43 completed of 43 total, with 0 remaining software tasks. Physical-device and second-Windows-machine observations remain explicitly outside that software-task count and are listed below as `field_pending`.

## Field pending

- On the acquisition computer, install and identify the five real sensor/interface classes, prove channel completeness, units, freshness, 10 Hz behavior, disconnect/reconnect and durable CSV/MySQL finalization.
- On a second Windows computer, install the Helper and prove actual HTTP/WebSocket control, readiness identity, capability negotiation, network interruption, retransmission, ACK reconciliation and stop finalization.
- Where production MySQL is required, validate the target instance, schema, write/rollback probe and final capture reconciliation in the field environment.

Until those observations exist, all affected evidence remains `field_pending`; no field acceptance or production prediction approval is claimed.
