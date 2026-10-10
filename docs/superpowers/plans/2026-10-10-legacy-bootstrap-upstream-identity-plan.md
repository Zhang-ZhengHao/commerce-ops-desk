# Legacy bootstrap upstream identity hotfix implementation plan

Status: approved for execution under the operator's standing instruction to
apply the recommended approach automatically

Source design:
[`2026-10-10-legacy-bootstrap-upstream-identity-design.md`](../specs/2026-10-10-legacy-bootstrap-upstream-identity-design.md)

Implementation branch: `fix/legacy-bootstrap-upstream-identity`

Verified base: `36ace8a61470fa9e12440131e028b7d12025df83`

## Execution rules

- Work only in `.worktrees/legacy-bootstrap-upstream`.
- Observe every regression test fail for the intended missing behavior before
  editing production code.
- Keep running-owner discovery, route-aware network policy, and frozen evidence
  independently reviewable in tests.
- Never delete stopped evidence containers, disconnect shared bridge peers, or
  edit the workstation checkout to make the probe pass.
- Do not resume build, prepare, quarantine, or switch until a merged new SHA has
  exact Verify and CodeQL success.
- Do not publish a URL, access code, release, tag, or live-demo CTA.

## P0 — design and clean baseline

1. Confirm the two published `main` refs and the worktree base.
2. Run the existing upstream and active-chain suites unchanged.
3. Commit and push this design and plan.

## P1 — RED regressions

1. Change the discovery contract to require running-only full-ID enumeration
   and add a stopped-container fixture that retains the target binding.
2. Add paired network tests: a shared peer is accepted only in membership mode
   and rejected in exclusive mode; endpoint drift fails in both modes.
3. Add route-aware readiness tests for legacy and hardened profiles.
4. Replace the tautological legacy fingerprint test with a complete frozen
   `UpstreamIdentity` fixture and verify the current constant fails.

RED command:

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_workstation_upstream_verification.py \
  scripts/tests/test_workstation_active_chain_integration.py
```

## P2 — minimal implementation and documentation

1. Remove `--all` from full-ID port-owner discovery.
2. Add a safe-default, keyword-only `require_exclusive_network` policy to
   upstream reconstruction/current/readiness helpers.
3. Add one route-aware readiness wrapper and replace every production
   route-state call site with it.
4. Update the legacy fingerprint to the reproducible attestation value.
5. Update the Runbook and changelog with the exact compatibility semantics.

GREEN commands:

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_workstation_upstream_verification.py \
  scripts/tests/test_workstation_active_chain_integration.py
.venv/bin/python -m pytest -q \
  scripts/tests/test_workstation_\*.py
```

## P3 — full verification and review

1. Run formatting, lint, type checks, `git diff --check`, public-history scan,
   and `make verify`.
2. Request independent reviews focused on legacy-only relaxation, hardened
   network exclusivity, discovery races, and evidence reproducibility.
3. Commit with the verified GitHub noreply identity, push both remotes, and
   verify both branch refs equal local `HEAD`.
4. Open a pull request; merge only after Verify and CodeQL succeed for the exact
   head SHA with correctly attributed history.

## P4 — exact-SHA workstation continuation

1. Run the new exact checkout's read-only legacy bootstrap loader before any
   build and require the new fingerprint to match.
2. Fresh-stage the merged SHA and build its verified image manifest.
3. Quarantine only the retained `bf78fe34d120e305baa151a97fb351235a0d37c4`
   candidate data through the versioned helper and verify inode preservation.
4. Prepare the new candidate on `18088`, verify `/ready` and exact `/api/build`,
   then use the transaction helper for switch.
5. Complete all nine private acceptance checks. Use only the exact printed
   reconcile ledger or active-head-authorized rollback backup on failure.
