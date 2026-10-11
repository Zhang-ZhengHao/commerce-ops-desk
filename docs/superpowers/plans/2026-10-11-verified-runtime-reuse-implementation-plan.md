# Verified runtime reuse implementation plan

Status: approved for execution

Source design:
[`2026-10-10-verified-runtime-reuse-design.md`](../specs/2026-10-10-verified-runtime-reuse-design.md)

Implementation branch: `feat/verified-runtime-reuse`

Verified design commit: `095a8260d2702f9ac6aba13f5fb536fc1b59efe5`

Observed deployment baseline:
`1a333c8a3ae5172f4b025e4543386debec826cbf`

Verified donor:
`bf78fe34d120e305baa151a97fb351235a0d37c4`

## 1. Execution rules

- Work only in the dedicated implementation worktree at
  `.worktrees/verified-runtime-reuse`. Do not modify or deploy from the primary
  checkout.
- Follow red-green-refactor for every behavior. Run each new test against the
  current implementation and observe the intended failure before adding the
  smallest production change. RED states remain local; every public checkpoint
  must be green.
- Keep source build and reuse operator-distinct. A normal source-build failure
  exits non-zero and never searches for or selects a donor. Reuse is entered
  only with `--reuse-from-manifest`; recovery additionally requires
  `--recover-unmanifested-image`.
- Treat the successful `renameat2(RENAME_NOREPLACE)` of a canonical manifest as
  the evidence commit point. Never overwrite, retag, remove, or silently repair
  differing evidence.
- Use only the passwd entry for the effective UID to derive
  `apps/commerce-ops-desk/deploy-state`. Ambient `HOME`, `PATH`, `PYTHONPATH`,
  `GIT_*`, global Git configuration, and replacement refs are not trust inputs.
- Use `/usr/bin/git`, `/usr/bin/docker`, and the existing fixed system binaries.
  Do not add a test-only production bypass for paths, users, Git objects, or
  Docker identity.
- Preserve the approved runtime boundary. Do not modify `Dockerfile`,
  `.dockerignore`, `backend/`, `frontend/`, or non-test files below `scripts/`
  while implementing the deployment tooling. At every green checkpoint verify
  that the target Dockerfile is still blob
  `b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa` with raw SHA-256
  `35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d`.
- Keep schema 2 readable only as legacy source-build evidence. All newly built
  manifests and newly published candidate states use schema 3.
- Keep secrets and captured subprocess output out of errors, tests, logs,
  commits, and artifacts. Test canaries must be fictional.
- Do not touch the workstation, Docker daemon, Caddy, containers, candidate
  data, or live traffic during repository implementation. Workstation execution
  resumes only after the exact merge SHA passes all repository and CI gates.
- Access-code governance remains unresolved. Do not publish a public URL,
  access code, or live-demo call to action as part of this work.
- At each green checkpoint, commit with a concise Chinese message, push the
  implementation branch to both `origin` and `github`, and require
  `git rev-parse HEAD` to equal both remote branch SHAs before proceeding.

## 2. Checkpoint sequence

| Checkpoint | Outcome | Green commit |
|---|---|---|
| P0 | Approved design plus executable implementation plan | `规划验证运行时复用实施步骤` |
| P1 | Shared provenance primitives, safe state I/O, strict JSON, full-SHA lock, and no-clobber publication | `建立共享来源证据安全基础` |
| P2 | Fixed Git-object export, runtime inventory, and approved Dockerfile transition proof | `验证运行输入与构建配方等价` |
| P3 | Deploy compatibility, candidate schema 3, lock migration, and provenance-first prepare/readiness | `贯通部署来源证据校验` |
| P4 | Schema-3 source builds with durable no-clobber evidence | `升级受验源码构建证据` |
| P5 | Explicit metadata-only reuse and idempotent reuse recovery | `实现显式验证运行时复用` |
| P6 | Runbook, tooling contract, exhaustive regression, and independent review | `完成运行时复用验收门禁` |

Within a checkpoint, use the smaller RED/GREEN slices below. Do not bundle
production behavior ahead of its failing test merely to reach the checkpoint
faster.

## 3. Test and module layout

New production module:

- `deploy/workstation/verified_provenance.py`

Existing production consumers:

- `deploy/workstation/build_verified_image.py`
- `deploy/workstation/deploy.py`
- `deploy/workstation/RUNBOOK.md`
- `Makefile`

Test ownership:

- `scripts/tests/test_verified_provenance.py` covers strict serialization,
  safe file descriptors, canonical state paths, locking, no-clobber
  publication, Git tree/archive binding, and runtime inventories.
- `scripts/tests/test_verified_image_build.py` covers source-build compatibility,
  explicit CLI dispatch, Docker inspection, metadata-only creation, final
  revalidation, and recovery.
- `scripts/tests/test_workstation_provenance.py` covers typed build provenance,
  candidate schema 3, provenance-first prepare, and readiness rebinding.
- `scripts/tests/test_workstation_deployment.py` retains end-to-end deployment,
  quarantine, runtime, Caddy, and Runbook regression coverage.
- `scripts/tests/test_project_tooling.py` binds the new module into every static
  check and exact-checkout tooling contract.

Shared module API, refined only through tests:

- `ProvenanceError`
- `CanonicalStateDirectory` with retained descriptors and `revalidate()`
- `SafeRead` with retained descriptor, raw bytes, identity, `revalidate()`, and
  durability operations
- `BuildLock.acquire(state, source_sha)`
- typed `SourceBuild`, `VerifiedRuntimeReuse`, `RuntimeEquivalence`,
  `ReusedFrom`, `LoadedBuildManifest`, `RevisionEvidence`, and
  `ReuseGitEvidence`
- `open_canonical_state_directory()`
- `canonical_manifest_path()` and `source_sha_from_manifest_path()`
- `safe_read_direct_child()` and `safe_read_nested_child()`
- `strict_json_loads()` and canonical JSON/digest helpers
- `parse_build_manifest_bytes()` and `load_build_manifest()`
- `publish_manifest_no_clobber()`
- `export_revision_evidence()` and `recompute_runtime_equivalence()`
- the one reserved provenance-label namespace and schema-specific expected
  label helpers

Both entry points load this direct sibling through a fixed
`importlib.util.spec_from_file_location` loader, register it in `sys.modules`
before execution, and remain usable both under `/usr/bin/python3 -I` and the
tests' existing `spec_from_file_location` loaders.

## 4. P0 — plan checkpoint

Files changed:

- `docs/superpowers/plans/2026-10-11-verified-runtime-reuse-implementation-plan.md`

Actions:

1. Confirm the implementation worktree is clean at the approved design commit.
2. Confirm the focused builder/deployment baseline remains green.
3. Record this plan without changing production behavior.
4. Run `git diff --check`, commit, push both remotes, and compare all three
   SHAs.

GREEN commands:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q \
  scripts/tests/test_verified_image_build.py \
  scripts/tests/test_workstation_deployment.py
git diff --check
```

## 5. P1 — shared provenance and safe-state foundation

### P1.1 — isolated sibling loading and tooling coverage

RED tests:

- Both entry points load under `/usr/bin/python3 -I ... --help`.
- Both remain loadable with the tests' `spec_from_file_location` pattern.
- A hostile `PYTHONPATH` cannot substitute the sibling module.
- Ruff, format, and mypy file lists include all three deployment Python files.

Minimal GREEN:

- Add the sibling module and fixed loaders.
- Initially expose only constants, exception types, and frozen data types.
- Extend the Makefile and tooling contract immediately so later code cannot
  bypass static checks.

### P1.2 — strict JSON, canonical encoding, and domain digests

RED tests:

- Reject duplicate keys at every depth, `NaN`, `Infinity`, `-Infinity`, BOMs,
  invalid UTF-8, unknown fields, and booleans in integer fields.
- Pin canonical schema-3 manifest bytes and candidate-provenance compact bytes.
- Pin empty, one-entry, and multi-entry domain-plus-length digests.
- Parse schema 2 only as legacy source build; reject any schema-2 `build` arm.
- Strictly parse schema-3 source and reuse arms; reject unknown kinds,
  donor-equals-target, malformed references, unequal runtime digests, unknown
  policies, and non-canonical schema-3 byte encodings.

Minimal GREEN:

- Implement strict object-pairs and numeric-constant hooks.
- Validate exact field sets and exact scalar types into frozen typed values.
- Centralize the pretty manifest serializer, compact provenance serializer,
  and eight-byte big-endian length-framed digest helper.

### P1.3 — passwd-derived state directory and retained safe reads

RED tests:

- Hostile `HOME` does not change the state path.
- Reject relative/noncanonical passwd homes, wrong owner, group/world-writable
  ancestors, symlinked components, alternate state directories, incorrect
  `0700` state mode, and noncanonical manifest names.
- Direct and nested reads reject hard links, symlinks, broken symlinks, FIFO,
  socket/device substitution, wrong owner/mode, oversize content, short reads,
  appended bytes, invalid UTF-8, and file or parent identity drift.
- A FIFO substitution test runs in a bounded subprocess and proves the reader
  does not block.
- Errors do not contain file contents or canary text.

Minimal GREEN:

- Derive the account home with `pwd.getpwuid(os.geteuid()).pw_dir`.
- Open every descendant through retained dirfds using
  `O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW`.
- Open final files with
  `O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW`, bind the pre-open observation to
  `fstat`, read exactly the initial size plus an EOF probe, and repeat all file
  and directory identity checks.
- Apply 256 KiB/`0600` rules to manifests and candidate state, and 1 MiB/`0644`
  rules to the three deployment assets.

### P1.4 — shared full-SHA lock

RED tests:

- Create or reopen only `.build-<40-lowercase-sha>.lock`.
- Require an empty, current-user-owned, single-link regular file of mode
  `0600`.
- Reject symlink, wrong type/mode/owner/link count, nonempty content, inode
  replacement, and parent replacement.
- A second nonblocking owner for the same SHA fails before manifest or tag
  observation; different SHAs proceed independently.
- Lock files remain after release.

Minimal GREEN:

- Create with `O_EXCL`; on `EEXIST`, no-follow open the existing name.
- Handle interrupted syscalls, take `flock(LOCK_EX|LOCK_NB)`, and revalidate the
  pathname, inode, metadata, and retained state directory before and after the
  protected body.
- Keep all descriptors close-on-exec and never unlink a lock.

### P1.5 — durable no-clobber publisher

RED tests:

- Write all canonical bytes to a random `0600` file relative to one retained
  state dirfd, including short-write handling.
- Require file `fsync`, `renameat2(RENAME_NOREPLACE)`, post-rename inode
  `fsync`, parent `fsync`, exact safe reread, and final identity checks.
- Preserve an existing destination on `EEXIST`.
- Fail closed on `ENOSYS`, `EINVAL`, or `EOPNOTSUPP`; never use `os.replace`,
  ordinary rename, or link/unlink as a fallback.
- Before rename, clean the random temporary and sync the directory. After a
  successful rename, retain the canonical manifest on every later failure and
  report an indeterminate committed result.

Minimal GREEN:

- Call libc `renameat2` with relative names, retained same-directory dirfds,
  and `RENAME_NOREPLACE=1`.
- Track an explicit committed flag so cleanup cannot erase or misclassify
  committed evidence.
- Provide a same-filesystem support probe usable before a source image build,
  because source-build orphan recovery is intentionally unsupported.

Focused GREEN commands:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q scripts/tests/test_verified_provenance.py \
  -k 'json or canonical or state or safe_read or lock or publish'
"$PY" -m pytest -q scripts/tests/test_project_tooling.py
"$PY" -m ruff check \
  deploy/workstation/verified_provenance.py \
  deploy/workstation/build_verified_image.py \
  deploy/workstation/deploy.py
"$PY" -m ruff format --check \
  deploy/workstation/verified_provenance.py \
  deploy/workstation/build_verified_image.py \
  deploy/workstation/deploy.py
"$PY" -m mypy --config-file backend/pyproject.toml \
  deploy/workstation/verified_provenance.py \
  deploy/workstation/build_verified_image.py \
  deploy/workstation/deploy.py
```

## 6. P2 — Git-object and runtime-equivalence evidence

### P2.1 — fixed, isolated Git observation

RED tests:

- A fake `git` earlier on `PATH`, hostile `GIT_*`, replacement refs, global and
  system configuration, global attributes, repository info attributes, and
  dirty worktree bytes cannot affect observations.
- Require the exact Git top level, a full lowercase commit ID, and exact
  approved remote-ref equality.
- Reject alternate-object paths that cannot be represented safely without
  newline injection.

Minimal GREEN:

- Invoke only `/usr/bin/git --no-replace-objects` with fixed argv.
- Strip all `GIT_*`, set safe `PATH`, and use fresh private `HOME` and
  `XDG_CONFIG_HOME` plus disabled system/global configuration and attributes.
- Build the existing isolated object view without trusting worktree bytes.

### P2.2 — tree scan and archive cross-binding

RED tests:

- Parse NUL-delimited output from
  `ls-tree -rz -t --full-tree <sha> -- backend frontend scripts .dockerignore Dockerfile`.
- Reject runtime symlinks, gitlinks, special types, illegal mode/type pairs,
  non-UTF-8 paths, control characters, noncanonical paths, and an invalid root
  file before archive creation.
- Reject duplicate, transformed, nonregular, missing, extra, or mode-mismatched
  archive members and any archive/tree disagreement.
- Bind `Dockerfile` independently to its tree blob, raw `cat-file` bytes, and
  one identical archive member.

Minimal GREEN:

- Parse tree bytes without lossy decoding.
- Read tar member bytes before extraction and cross-check path, regular-file
  type, and executable bit against the tree.
- Use archive bytes for runtime inventories and deployment-asset hashes.

### P2.3 — runtime inventory and fixed Dockerfile policy

RED tests:

- Pin canonical component and combined JSON/digests for empty, one-entry, and
  multi-entry fixtures, including UTF-8 byte ordering.
- Require the four anchors and the exact exclusion list from the design.
- Reject add/delete/rename/content/mode changes, dependency-lock drift,
  `.dockerignore` drift, or a changed included script.
- Allow only documented differences in docs, deployment tooling, backend tests,
  script tests, and excluded caches.
- Require donor ancestry, target strict descent from the observed baseline,
  and the exact donor/target Dockerfile blob and raw-digest pair.
- Reject every other Dockerfile transition, including archive transformations.

Minimal GREEN:

- Implement `export_revision_evidence()` and
  `recompute_runtime_equivalence()` returning immutable evidence records.
- Sort inventory paths by UTF-8 bytes and frame every digest with the specified
  domain and length.
- Compare all four component digests and the combined digest exactly.

Focused GREEN commands:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q scripts/tests/test_verified_provenance.py \
  -k 'git or tree or archive or inventory or dockerfile or equivalence'
"$PY" -m pytest -q scripts/tests/test_verified_image_build.py \
  -k 'dirty or replace_ref or attributes or archive or repository'
```

## 7. P3 — deployment provenance and candidate schema 3

Deployment compatibility is implemented before the builder starts emitting
schema 3. Throughout P3, existing schema-2 source manifests remain a complete
positive path.

### P3.1 — typed provenance and candidate schema 3

RED tests:

- Pin canonical candidate provenance and golden digest for legacy schema-2
  source, schema-3 source, and schema-3 reuse.
- Require candidate schema 3 with exactly the prior fields plus
  `build_provenance` and `build_provenance_sha256`.
- Reject schema-2 candidate state, missing or extra fields, duplicate keys,
  nonstandard numbers, boolean integers, wrong state filename/source prefix,
  malformed provenance, or a mismatched domain-length digest.
- Enforce the provenance matrix:

  | Target manifest | Target reserved labels | Donor |
  |---|---|---|
  | schema 2 source | reserved namespace empty | none |
  | schema 3 source | only `build-kind=source-build` | none |
  | schema 3 reuse | exactly four reuse labels | schema 2 or schema 3 source |

Minimal GREEN:

- Add typed candidate source/reuse provenance values.
- Generate provenance only from a validated typed manifest, never from mutable
  image labels alone.
- Upgrade the strict state loader and no-clobber state writer to schema 3.

### P3.2 — migrate prepare and quarantine to the shared lock

RED tests:

- `prepare` extracts the target SHA only from a canonical manifest filename,
  obtains `.build-<full-sha>.lock`, and only then reads the manifest.
- A held builder lock prevents prepare before any manifest, tag, data, Docker,
  or Caddy observation.
- The lock remains held through candidate-state publication.
- Quarantine uses the same full-SHA lock and can safely create it for a legacy
  interrupted candidate that has only the old 12-character lock.
- Old locks remain untouched as evidence and no longer provide mutual
  exclusion.

Minimal GREEN:

- Delete the short-SHA prepare-lock path from active use.
- Have prepare and quarantine use `BuildLock.acquire` with create-or-safe-open
  semantics. Quarantine must not require the new lock to preexist.
- Preserve existing quarantine no-clobber and durability behavior.

### P3.3 — provenance-first prepare

RED tests require every failure below to occur before
`_create_candidate_data_directory`, Compose, networks, or containers:

- filename/content SHA mismatch or moved approved ref;
- target no longer a commit;
- target/donor manifest identity, digest, arm, ancestry, or daemon drift;
- runtime inventory or Dockerfile-policy drift;
- target/donor tag, immutable ID, revision, environment, reserved labels,
  RootFS, platform, or normalized Config mismatch;
- deployment-asset identity or digest drift;
- manifest/state durability or identity failure.

Minimal GREEN and fixed order:

1. Open the passwd-derived canonical state directory.
2. Validate the requested manifest as its direct canonical child and derive
   the target SHA from its filename only.
3. Acquire and retain the target build lock.
4. Safe-read and strictly parse the target manifest, retaining its descriptor.
5. For reuse, safe-read and bind the canonical donor manifest.
6. Recompute approved-ref, ancestry, tree/archive inventories, and Dockerfile
   evidence with fixed Git.
7. Recheck daemon, tag and immutable-ID inspections, schema-specific labels,
   RootFS, platform, and complete normalized Config.
8. Safe-read the three target deployment assets; donor assets are never used.
9. `fsync` the same retained target-manifest inode and state dirfd and repeat
   identity checks.
10. Only then permit candidate data or Docker side effects.

Config normalization must preserve absent-versus-null values and list order.
Only the source environment, OCI revision, and exact four reuse labels may be
normalized back to donor values before a complete deep comparison. All
environment names must be unique.

### P3.4 — persist and rebind provenance

RED tests:

- A successful prepare persists schema 3, the exact target manifest raw hash,
  schema/build arm, donor hash and identity when applicable, complete runtime
  equivalence, and the pinned provenance digest while the target lock remains
  held.
- `_assert_state_candidate_ready` fails before container or Caddy work if the
  target manifest, build arm, raw bytes, daemon, tag, immutable ID, revision,
  source environment, or labels change.
- Reuse readiness also fails if the donor manifest bytes/source-build arm,
  donor tag/ID, or donor labels change.
- Source schema 2 and schema 3 continue to enforce their distinct label rules.

Minimal GREEN:

- Make provenance rebinding the first readiness operation.
- Recompute and compare the candidate provenance digest and target provenance
  from the current safe-read manifest.
- Reinspect both image references and immutable IDs. Readiness need not repeat
  Git archive equivalence, because prepare already persisted that evidence, but
  it must not discard either manifest-to-image link.

Focused GREEN commands:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q \
  scripts/tests/test_workstation_provenance.py \
  scripts/tests/test_workstation_deployment.py
"$PY" -m pytest -q scripts/tests/test_workstation_*.py
```

## 8. P4 — schema-3 source-build evidence

### P4.1 — explicit dispatch and preflight

RED tests:

- Normal invocation selects source build only; a failed Docker build never
  examines a donor or falls back to reuse.
- `--recover-unmanifested-image` without `--reuse-from-manifest` is rejected.
- Target tag and canonical target manifest must both be absent before a normal
  source build.
- The full-SHA lock is acquired before either absence check and held through
  post-publication verification.
- Same-filesystem no-clobber support is proven before invoking Docker.

Minimal GREEN:

- Split top-level dispatch into source, reuse, and recovery functions without
  sharing fallback control flow.
- Validate canonical paths and acquire the shared lock at the outer boundary.

### P4.2 — source image and schema-3 manifest

RED tests:

- Docker source build adds exactly the reserved
  `build-kind=source-build` label while preserving the existing source SHA and
  OCI revision contract.
- Tag and immutable-ID inspection agree and expose exactly one source SHA.
- New source manifests serialize canonically as schema 3 with the exact
  source-build arm.
- Approved ref, daemon, Git export, deployment assets, tag, ID, revision,
  environment, and labels are all revalidated immediately before publication.
- A pre-publication failure publishes no manifest and cleans only its private
  temporary content. A post-rename failure keeps the exact manifest and is
  reported as indeterminate.
- A missing source manifest is never reconstructed from labels.

Minimal GREEN:

- Replace the old overwriting schema-2 writer with the shared no-clobber
  publisher.
- Keep the source-build image and any committed evidence on every failure.
- Preserve suppressed subprocess output and the existing `BuiltImage` result
  contract.

Focused GREEN command:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q scripts/tests/test_verified_image_build.py \
  -k 'source or manifest or fallback or publication or lock'
```

## 9. P5 — explicit verified reuse and recovery

### P5.1 — donor preflight

RED tests:

- The donor manifest is a canonical direct child of the same state directory,
  with exact safe-file metadata and raw-byte digest.
- Accept only schema-2 legacy source or schema-3 source. Reject reuse-of-reuse,
  donor-equals-target, missing/noncommit/nonancestor donor, or a target not
  strictly after the observed baseline.
- Require the same daemon and exact donor tag-to-immutable-ID binding,
  revision, unique source environment, and schema-specific reserved labels.
- Reject any target approved-ref, runtime-inventory, `.dockerignore`, or fixed
  Dockerfile-policy mismatch before construction.

Minimal GREEN:

- Collect donor manifest, Git, daemon, and image evidence into one immutable
  record shared by normal reuse and recovery.

### P5.2 — metadata-only target image

RED tests:

- Generated Dockerfile contains exactly one immutable-ID `FROM`, one `ENV`,
  and one `LABEL`; it contains no parser directive, `ARG`, interpolation, stage
  name, `RUN`, `COPY`, `ADD`, or remote input.
- Docker argv uses the fixed binary, exact target tag, `--pull=false`,
  `--network=none`, and an absent private `--iidfile` child.
- A donor tag is never used as `FROM`, and hostile Docker/Compose environment
  variables are removed.
- The safely read IID equals both target-tag and immutable-ID inspections and
  differs from the donor ID.
- RootFS type and ordered diff IDs, OS/architecture/variant, and complete
  normalized Config match. Any command, entrypoint, user, working directory,
  health check, port, volume, shell, unrelated environment, label, or
  absent/null drift is rejected.
- Target reserved labels are exactly build kind, donor SHA, donor immutable
  ID, and runtime-input digest with their expected values.

Minimal GREEN:

- Render the constant metadata-only Dockerfile from validated lowercase values.
- Build in a private temporary context, safely bind the IID, and perform
  tag/immutable inspections.
- Compare immutable image documents using a narrowly normalized deep copy.

### P5.3 — complete final revalidation and publication

RED tests independently move or replace, after construction:

- approved remote ref;
- baseline/donor ancestry or either Git export;
- donor manifest bytes, tag, immutable ID, or config;
- daemon ID;
- target tag, immutable ID, RootFS, platform, config, or labels.

Every drift must prevent the target manifest. Publication failure output may
contain only the nonsecret full target image ID and explicit recovery command,
never captured output or manifest contents.

Minimal GREEN:

- Rerun and compare all immutable Git, manifest, daemon, donor, target, and
  policy evidence while the same target lock remains held.
- Check daemon identity immediately before canonical publication.
- After publication, exact-safe-read the manifest and recheck the target tag
  before releasing the lock.

### P5.4 — explicit idempotent recovery

RED tests:

- Recovery requires both reuse options and one full target immutable ID.
- Target tag must already resolve to that ID; recovery performs no Docker build,
  tag, removal, or deletion.
- The complete donor/Git/daemon/runtime/config/label validation reruns.
- With no manifest, publish the uniquely recomputed canonical bytes
  no-clobber. With an existing manifest, accept only an exact safe-read byte
  match, then sync that same inode and parent directory.
- Reject semantically equal but noncanonical bytes, different evidence,
  missing/different images, wrong build kind, recovery without a donor, and
  absent-manifest source images.
- Preserve donor, target, existing manifest, and failed evidence on all paths.

Minimal GREEN:

- Reuse the normal evidence collection and manifest construction; skip only
  image creation.
- Make exact-existing-manifest recovery idempotent and distinguish committed
  post-rename durability failures from pre-publication failures.

Focused GREEN commands:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q \
  scripts/tests/test_verified_provenance.py \
  scripts/tests/test_verified_image_build.py \
  scripts/tests/test_workstation_provenance.py
/usr/bin/python3 -I deploy/workstation/build_verified_image.py --help >/dev/null
/usr/bin/python3 -I deploy/workstation/deploy.py --help >/dev/null
```

## 10. P6 — documentation, regression, and release gates

### Documentation and tooling RED/GREEN

Tests require the Runbook to:

- derive and validate the account home from the effective UID's passwd entry,
  never from `$HOME`;
- include `verified_provenance.py` in every exact-checkout blob loop;
- use fixed `/usr/bin/git` in the verified workflow;
- document explicit source build, explicit reuse, no automatic fallback,
  full-ID recovery, no-clobber/indeterminate evidence, and schema-3 candidate
  behavior;
- preserve rollback, reconcile, quarantine, and access-code governance
  boundaries;
- forbid public promotion until the separate governance gate passes.

The Makefile and tooling tests require Ruff, format, and mypy coverage for all
three deployment modules.

### Checkpoint verification

At every green checkpoint:

```bash
PY=.venv/bin/python
git diff --check
"$PY" -m pytest -q scripts/tests/test_verified_provenance.py
"$PY" -m pytest -q \
  scripts/tests/test_verified_image_build.py \
  scripts/tests/test_workstation_provenance.py \
  scripts/tests/test_workstation_deployment.py
make project-test VENV=.venv
git rev-parse HEAD:Dockerfile
/usr/bin/git cat-file blob HEAD:Dockerfile | sha256sum
```

### Final local gate

Run fresh and read the complete output of each command:

```bash
PY=.venv/bin/python
"$PY" -m pytest -q \
  scripts/tests/test_verified_provenance.py \
  scripts/tests/test_verified_image_build.py \
  scripts/tests/test_workstation_provenance.py \
  scripts/tests/test_workstation_deployment.py
make verify VENV=.venv
make public-scan VENV=.venv
git diff --check
/usr/bin/python3 -I deploy/workstation/build_verified_image.py --help >/dev/null
/usr/bin/python3 -I deploy/workstation/deploy.py --help >/dev/null
```

Then require:

1. An independent security review of the exact final diff with no unresolved
   must-fix finding.
2. Local `HEAD` to equal both pushed implementation-branch SHAs.
3. The reviewed merge SHA to be a strict descendant of the observed baseline
   and to equal the approved remote ref used by any later build.
4. GitHub Verify and CodeQL to pass on that exact merge SHA.
5. A fresh exact workstation checkout and release-evidence selection before
   any schema-3 reuse command is considered.

No build, candidate preparation, switch, rollback, Caddy mutation, public
acceptance, or public promotion is performed by this repository implementation
plan itself.
