# Workstation compatibility hotfix implementation plan

Status: approved for execution under the operator's standing instruction to
apply the recommended approach automatically

Source design:
[`2026-10-10-workstation-compatibility-hotfix-design.md`](../specs/2026-10-10-workstation-compatibility-hotfix-design.md)

Implementation branch: `fix/workstation-compat-hotfix`

Verified base: `bf78fe34d120e305baa151a97fb351235a0d37c4`

## Execution rules

- Work only in `.worktrees/workstation-compat-hotfix`.
- Observe each regression test fail for the intended missing behavior before
  changing production code.
- Keep the Compose and quarantine fixes independently reviewable.
- Never edit the workstation's old exact-SHA checkout in place. A new commit,
  CI evidence, fresh workstation checkout, and new build manifest are required.
- Preserve the failed candidate directory, empty failed archive, old image,
  old code quarantine, live container, and live data until the versioned
  recovery path succeeds.
- Do not publish a URL, access code, release, tag, or live-demo CTA.

## Checkpoints

### P0 — design and clean baseline

1. Confirm `origin/main` and `github/main` equal the verified base.
2. Create the isolated hotfix worktree.
3. Run the existing workstation deployment test file unchanged.
4. Commit and push this design and plan.

### P1 — RED regressions

1. Change the Compose command assertions to require a supported create argv and
   the existing guarded up argv; verify old production code fails.
2. Add tests requiring candidate data to use the isolated privileged helper,
   while state continues through the local no-clobber rename; verify the helper
   is absent or unused in old production code.
3. Add direct helper contract and unsafe/racing input tests; verify the expected
   failures are caused by missing behavior rather than test setup.

RED command:

```bash
.venv/bin/python -m pytest -q scripts/tests/test_workstation_deployment.py \
  -k 'compose_commands or candidate_data_quarantine'
```

### P2 — minimal implementation and documentation

1. Remove `--no-deps` only from Compose create.
2. Add the fixed-argument isolated data-quarantine helper and strict receipt
   parser; retain the existing local state rename.
3. Update the Runbook to describe the exact Compose split and privileged
   data-only recovery boundary.
4. Add an Unreleased fix note and update the security model for the narrowed
   root helper.

GREEN commands:

```bash
.venv/bin/python -m pytest -q scripts/tests/test_workstation_deployment.py
.venv/bin/python -m pytest -q scripts/tests/test_workstation\*.py
```

### P3 — full verification and review

1. Run formatting, lint, type checks, `git diff --check`, public-history scan,
   and `make verify`.
2. Request two independent read-only reviews focused on privilege scope,
   rename failure semantics, and Compose compatibility.
3. Commit with the verified GitHub noreply identity, push to both remotes, and
   verify both remote branch SHAs equal local `HEAD`.
4. Open a pull request and require Verify and CodeQL success for the exact
   head SHA before merging with a locally created, correctly attributed commit.

### P4 — exact-SHA workstation recovery and candidate

1. Fresh-clone the merged SHA on the workstation and build its verified image.
2. Run the new helper with the old `bf78fe34d120e305baa151a97fb351235a0d37c4`
   source SHA to quarantine the retained data-only candidate.
3. Prepare the new candidate on free port `18088`, verify `/ready` and the exact
   `/api/build` response, then switch only through the transaction helper.
4. Complete all nine private operator-acceptance checks. On failure, use only
   the printed transaction's documented reconcile or rollback path.
