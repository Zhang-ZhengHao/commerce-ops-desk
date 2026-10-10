# Verified runtime reuse design

## Context

The workstation is running the verified image for
`bf78fe34d120e305baa151a97fb351235a0d37c4`. The approved deployment baseline
`1a333c8a3ae5172f4b025e4543386debec826cbf` could not be built because repeated
verified builds failed while downloading locked packages through the
workstation's unstable PyPI/Fastly path. No new build manifest, candidate,
route transaction, or Caddy change was produced.

Implementing this design necessarily creates a later release commit. The
eventual deployment target is therefore the exact approved merge SHA that
contains this implementation, not the earlier `1a333c8...` baseline. Reuse
policy binds the donor, the observed baseline ancestry, the runtime inventory,
and Dockerfile digests while allowing that later approved SHA to supply the
new deployment tools.

The two revisions have identical application runtime inputs. Their only
runtime-recipe change is the reviewed addition of `--timeout 300` to the
Dockerfile's build-time `pip install` command. Backend, frontend, locked
dependencies, and non-test scripts are unchanged. Changes outside the runtime
are limited to deployment tooling, documentation, and tests.

The existing schema-2 manifest proves that a local immutable image was built
from an approved Git object on one Docker daemon. It does not describe a reuse
operation or bind a donor image to a target image. Hand-written manifests,
retagging, or changing only an OCI label would therefore destroy the useful
provenance boundary. The repository needs an explicit, fail-closed reuse path
that can prove both source equivalence and image equivalence without contacting
an external package index.

This remains a local provenance control. It is not a cryptographic signature,
remote attestation, or defence against an administrator who controls the Git
object database, deployment user, and Docker daemon together.

## Goals

- Create the target image without downloading packages or rebuilding runtime
  payloads when an already verified source-build image has equivalent runtime
  inputs.
- Preserve an auditable chain from approved target commit, through a verified
  donor manifest and immutable donor image, to a distinct immutable target
  image.
- Reject every runtime input or image configuration change outside a small,
  versioned policy.
- Make reuse operator-explicit. A failed source build must never silently fall
  back to reuse.
- Let `deploy.py prepare` and later candidate-state validation independently
  reject malformed or inconsistent reuse evidence before route mutation.
- Harden private-manifest and deployment-asset reads against same-UID pathname
  replacement races encountered while adding the new trust input.

## Non-goals

- No dependency registry, wheelhouse transport, image registry, or signing
  service is introduced.
- No arbitrary Dockerfile semantic comparison is attempted.
- Reused images cannot be donors in reuse policy v1; provenance chains stay one
  hop deep.
- No Caddy bytes, route transaction schema, live data, access-code governance,
  or public-release policy changes are part of this work.
- No old image, container, failed staging directory, candidate data, or other
  deployment evidence is deleted.

## Chosen interface and components

`build_verified_image.py` retains its normal source-build path. Reuse is entered
only when the operator supplies:

```text
--reuse-from-manifest /absolute/private/state/build-<donor-sha>.json
```

The reuse option is mutually exclusive with any future source-build-only
option. It is never inferred from a Docker failure. A normal build failure
exits without starting reuse; the operator must issue a new, explicit command.

All build, recovery, and deployment commands derive one canonical application
root from the current effective UID's account home returned by
`pwd.getpwuid(os.geteuid()).pw_dir`, never from `HOME` or another ambient
variable. The only state directory is its direct child
`apps/commerce-ops-desk/deploy-state`, validated as an absolute,
deployment-user-owned, non-symlink directory of mode `0700`. The account home
must itself be absolute, canonical, deployment-user-owned, and not
group/world-writable. Each descendant component below it is opened with
`O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW` and rejected if it changes identity, has the
wrong owner, or is group/world-writable. The Runbook derives and verifies the
same account-home path. `--build-manifest` and
`--reuse-from-manifest` remain explicit audit inputs, but each must equal the
canonical direct child `build-<corresponding-40-hex-sha>.json` of that one state
directory. Alternate directories are rejected, so all commands for one target
necessarily share one lock.

If a fully validated reuse target image exists but its canonical manifest is
still absent after publication was reported as failed or indeterminate, the
only supported resume path adds:

```text
--recover-unmanifested-image sha256:<expected-target-id>
```

Recovery is valid only together with `--reuse-from-manifest`; the reuse option
alone selects normal reuse, while the recovery option alone is rejected. It
skips image construction, requires the canonical target tag to resolve to the
supplied full immutable ID, and repeats every target Git, approved-ref,
deployment-asset, daemon, donor, runtime-input, Dockerfile-policy, RootFS,
platform, normalized-config, source environment, OCI revision, immutable-ID,
and reuse-label check.

If the canonical target manifest is absent, recovery attempts the same
no-clobber publication. The same command is also an idempotent confirmation
path when the manifest already exists: recovery accepts it
only when a strict safe read is byte-for-byte identical to the uniquely
recomputed canonical manifest; it then `fsync`s that file and its parent
directory and returns success. A different manifest, missing image, different
image, wrong build kind, or invalid image fails without mutation. This makes a
repeated recovery idempotent and closes the case where the no-clobber rename
succeeded but a later durability operation failed. A source-build publication
failure deliberately cannot enter this reuse-only recovery path. Without an
already published source-build manifest, a later process cannot independently
prove that an arbitrary local image was produced by the controlled build, so
an absent-manifest source image remains blocked for administrator
investigation. Once a canonical source-build manifest has been atomically
renamed into place, that rename is the evidence commit point; `prepare` may
independently revalidate and make that existing evidence durable as described
below, but it never creates a missing source-build manifest.

The implementation is divided into three responsibilities:

1. `deploy/workstation/verified_provenance.py` owns strict manifest parsing,
   safe private-file reads, approved Git-object export, and canonical
   runtime-input inventories.
2. `build_verified_image.py` verifies the target and donor, creates the
   metadata-only image, validates image equivalence, and atomically publishes a
   new manifest.
3. `deploy.py` parses the provenance identity, validates its image labels and
   immutable image ID before candidate creation, and persists the identity in
   candidate state for repeated validation before switch is accepted.

The new module is itself a release-critical deployment tool. The Runbook's
exact-checkout procedure must bind its Git blob alongside
`build_verified_image.py`, `deploy.py`, and `select_release_evidence.py`. Both
entry-point scripts continue to work under `/usr/bin/python3 -I`: each loads
the direct sibling module by its explicit path with a small fixed
`importlib.util.spec_from_file_location` loader rather than depending on the
ambient import path. The same entry points must remain loadable by the tests'
existing `spec_from_file_location` pattern. The module is added to every
explicit Ruff, format, and mypy file list in the Makefile, and the tooling
contract tests assert that coverage so release-critical code cannot bypass the
normal static checks.

## Per-target serialization

Normal source builds, verified reuse, recovery, and `deploy.py prepare` all use
the same persistent lock for a target SHA. After validating or deriving the
target SHA from a canonical manifest filename and validating the private state
directory, but before observing the target manifest or target image tag, the
command opens the direct child `.build-<target-sha>.lock` relative to the
retained state-directory descriptor. It first attempts creation with
`O_RDWR|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW` and mode `0600`; on `EEXIST` it
opens the existing name with `O_RDWR|O_CLOEXEC|O_NOFOLLOW`. The opened object
must be deployment-user-owned, regular, single-link, mode `0600`, empty, and
have the same device/inode as a relative no-follow path observation.

The command takes `flock(LOCK_EX|LOCK_NB)`, failing safely when another
cooperating operation owns it, then repeats the file and parent-directory
identity checks. Builders retain that descriptor and lock through target
pre-existence checks, construction or recovery, final evidence revalidation,
manifest publication or durability repair, and a final safe read of the
manifest and target tag. `prepare` retains it from manifest-name validation
through all provenance and image checks, candidate resource creation, runtime
verification, and atomic candidate-state publication. Thus it cannot consume a
manifest while its builder is still completing post-rename durability checks.
Lock files are never unlinked or replaced. An unsafe or replaced lock fails
closed. This serializes all repository-supported operations for one target
without blocking work for different SHAs. A same-UID administrator mutating
Docker or the state directory outside these tools remains outside the stated
local provenance threat boundary.

## Trust and validation sequence

The reuse builder performs these checks in order and stops at the first
failure:

1. Resolve the repository as the exact Git worktree top level under one fully
   isolated Git environment. Every Git invocation uses the fixed
   `/usr/bin/git` binary. All ambient `GIT_*` variables are removed; `PATH` is
   `/usr/bin:/bin`; `HOME` and `XDG_CONFIG_HOME` are fresh private temporary
   directories; global/system config and system attributes are disabled; and
   replacement refs stay disabled. Commands use fixed argv and explicit `-c`
   overrides where archive attributes are relevant.
2. Require the target to be a full lowercase commit SHA that exactly equals the
   approved remote-tracking ref.
3. Acquire and retain the validated per-target lock before inspecting either
   the canonical target manifest or target tag. A normal source build or reuse
   requires both to be absent. Recovery requires the supplied immutable image
   to match the target tag and permits the manifest to be absent or to contain
   the exact safely read canonical expected bytes. Existing files, symlinks,
   broken symlinks, and non-matching manifests are never overwritten.
4. Require the donor manifest to be the canonical direct child
   `build-<donor-sha>.json` of that same state directory, owned by the deployment
   user, regular, single-link, and mode `0600`.
5. Parse the donor as either a legacy schema-2 source build or a schema-3
   `source-build`. Reject unknown schemas, extra fields, target-equals-donor,
   and any `verified-runtime-reuse` donor.
6. Require the donor commit object to exist and be a strict ancestor of the
   target. Also require the observed baseline
   `1a333c8a3ae5172f4b025e4543386debec826cbf` to be an ancestor of the target,
   with the target strictly later than that baseline. The historical remote ref
   need not still point at the donor because the private donor manifest records
   the approval that existed at build time.
7. Require the current Docker daemon ID to equal the donor manifest daemon ID.
   Recheck it after donor inspection, after target inspection, and immediately
   before manifest publication.
8. Inspect both the donor tag and donor immutable ID. They must identify the
   same image, with the donor OCI revision and exactly one matching
   `COMMERCE_OPS_SOURCE_SHA` entry.
9. Prove runtime-input equivalence and the one approved Dockerfile transition.
10. For normal reuse, build and inspect a distinct target image. For recovery,
    skip construction and inspect the supplied pre-existing target image under
    the same requirements.
11. While still holding the target lock and immediately before publication,
    repeat the approved-ref equality check,
    donor and baseline ancestry checks, donor manifest byte digest, donor
    tag-to-ID inspection, target tag-to-ID inspection, daemon identity, both
    Git exports, both runtime inventories, and both Dockerfile raw digests.
    Every result must equal the evidence cached before the build.
12. Publish the manifest only after all repeated checks succeed.

No candidate data directory, Compose project, network, container, or Caddy
transaction is created by this command.

## Runtime-input equivalence

Both commits are inspected through the existing isolated bare-object view.
Before archiving, the builder invokes
`/usr/bin/git ls-tree -rz -t --full-tree <sha> -- backend frontend scripts .dockerignore Dockerfile`
under the same isolated Git environment and parses its NUL-delimited
`<mode> <type> <object>\t<path>` records. Within the three runtime roots, only
`040000 tree` directory records and `100644 blob` or `100755 blob` file records
are accepted. Symlinks (`120000`), gitlinks (`160000`), and every other
mode/type combination fail before archive creation, even if `git archive`
would omit the object or represent it only as an empty directory. The two root
files must each be a regular `100644` or `100755` blob.

The same commits are then exported through `git archive`. Runtime comparison
never reads worktree bytes. Each accepted archive member is cross-checked
against its tree record for canonical path, blob type, and executable bit;
archive directories are structural only, and every other tar member type is
rejected. This ensures that committed `.gitattributes` export policy is
reflected in the actual exported path set while repository-local or global
attributes cannot influence it.

Policy `runtime-copy-inputs-v1` considers every exported regular file below the
exact roots `backend/`, `frontend/`, and `scripts/`, then excludes only these
root-relative subtree prefixes:

```text
backend/.mypy_cache/
backend/.pytest_cache/
backend/.ruff_cache/
backend/__pycache__/
backend/wheelhouse/
backend/postgres_tests/
backend/tests/
frontend/coverage/
frontend/dist/
frontend/node_modules/
frontend/playwright-report/
frontend/test-results/
scripts/tests/
```

It also excludes a file under those roots when its final path component
matches exactly one of `*.db`, `*.db-shm`, `*.db-wal`, `*.sqlite`, `*.sqlite3`,
or `*.pyc`, using case-sensitive `fnmatchcase` semantics with `*` unable to
cross `/`. No other test, fixture, cache, hidden file, or extension is excluded.
The root `.dockerignore` is bound as a separate byte sequence. These rules are
the versioned projection of the approved current Dockerfile and
`.dockerignore`; changing either requires a new policy.

The policy requires identical canonical relative-path sets. Every entry binds
its path, regular-file type, Git executable bit, and SHA-256 of its bytes.
Symlinks, Git links, other non-regular entries, non-canonical paths, control
characters, and non-UTF-8 paths fail closed in policy v1. The exact required
regular-file anchors are `backend/requirements.lock`,
`frontend/package.json`, `frontend/package-lock.json`, and
`scripts/start-hosted.sh`; each must survive archive export and inventory
filtering.

Inventory metadata and bytes come from the isolated `git archive` tar members,
before filesystem extraction: canonical POSIX member name, regular-file type,
Git mode normalized to the string `100644` or `100755`, and SHA-256 of the
exported member bytes. Paths are sorted by their UTF-8 byte representation.

For each of `backend`, `frontend`, and `scripts`, the canonical JSON value is:

```json
{"component":"backend","entries":[{"mode":"100644","path":"backend/example","sha256":"<64 lowercase hex>"}],"schema":1}
```

The component name changes to match the component. Entries use the exact three
keys shown. JSON is serialized with `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=True`, no trailing newline, and UTF-8 encoding. The component
digest is SHA-256 over
`commerce-ops-desk:runtime-copy-input-component:v1\0`, then the JSON length as
an unsigned eight-byte big-endian integer, then the JSON bytes.

The `.dockerignore` component canonical JSON has the exact keys
`{"component":"dockerignore","schema":1,"sha256":"<raw-byte SHA-256>"}` and
uses the same serializer and component-domain encoding. The combined canonical
JSON has the exact shape
`{"components":{"backend":"<digest>","dockerignore":"<digest>","frontend":"<digest>","scripts":"<digest>"},"policy":"runtime-copy-inputs-v1","schema":1}`.
Its digest uses the same length framing with domain
`commerce-ops-desk:runtime-copy-input-combined:v1\0`.

The donor and target component and combined digests must be equal; storing only
an unordered set or concatenating ambiguous text is forbidden. Tests pin
canonical bytes and digests for empty, one-entry, and multi-entry fixtures.

`.dockerignore` must be byte-for-byte identical between donor and target.
Policy v1 does not implement a general Dockerignore or Dockerfile parser.

## Approved Dockerfile transition

Dockerfile policy `pip-timeout-300-v1` accepts exactly this reviewed raw-digest
pair and no other pair:

| Revision | Git blob | Raw SHA-256 |
| --- | --- | --- |
| donor | `1d697b8fe13af6b0f787fe5639568b97aa3973de` | `3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a` |
| target | `b0e67ee8e9ffbb1453cdc2a2bf4a0081510bfcaa` | `35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d` |

For each revision, the isolated `ls-tree` record must be a `100644 blob` naming
the listed object at the exact root path `Dockerfile`. The raw digest is
computed over the bytes
returned by `/usr/bin/git cat-file blob <listed-blob>`. The archive must
also contain exactly one regular `Dockerfile` member whose exported bytes are
byte-for-byte equal to those blob bytes and therefore to the listed raw digest.
`export-ignore`, `export-subst`, a missing or duplicate member, a mode/type
change, or any other archive transformation fails closed.

The donor blob belongs to
`bf78fe34d120e305baa151a97fb351235a0d37c4`. The target blob is observed at the
`1a333c8a3ae5172f4b025e4543386debec826cbf` baseline and must remain unchanged
through the eventual approved implementation merge SHA. It differs from the
donor only by inserting `--timeout 300` into the locked pip-install command.
Whitespace, comments, base images, `COPY`, dependency paths, build commands,
user, environment, health check, volume, entrypoint, or command changes produce
a different raw digest and are rejected. Supporting a future harmless
transition requires a new reviewed policy identifier and tests.

## Offline target-image creation

The builder uses an internal, constant metadata-only Dockerfile. Its complete
instruction set is one `FROM sha256:<verified-donor-id>`, one `ENV` replacing
only `COMMERCE_OPS_SOURCE_SHA`, and one `LABEL` replacing the OCI revision and
setting the exact four reuse labels below. The strictly validated lowercase
SHAs, image ID, and digest are the only substituted values. The file has no
parser directive, `ARG`, interpolation, stage name, `RUN`, `COPY`, `ADD`, or
remote source. Docker is invoked through the fixed binary and scrubbed local
daemon environment with `--pull=false`, `--network=none`, and the exact target
tag `commerce-ops-desk:<target-sha>`. It also receives `--iidfile` pointing to
an absent child of the builder's private temporary directory. The safely read
IID must equal both the target tag inspection ID and a separate inspection by
immutable ID before and after the final provenance recheck.

If the workstation Docker version cannot use the immutable image ID as `FROM`,
the operation fails closed. It must not fall back to a mutable donor tag. A
future temporary-alias implementation would require a separate design that
binds the alias to the donor ID before and after the build.

The target image's runtime-relevant `Config` changes only:

- `org.opencontainers.image.revision` to the target SHA;
- the unique `COMMERCE_OPS_SOURCE_SHA` environment entry to the target SHA;
- `io.github.zhang-zhenghao.commerce-ops.build-kind` to
  `verified-runtime-reuse`;
- `io.github.zhang-zhenghao.commerce-ops.reused-from-source` to the donor SHA;
- `io.github.zhang-zhenghao.commerce-ops.reused-from-image-id` to the donor
  immutable image ID; and
- `io.github.zhang-zhenghao.commerce-ops.runtime-inputs-sha256` to the equal
  combined runtime-input digest.

Those four `io.github.zhang-zhenghao.commerce-ops.*` keys are the complete
allowed reuse-label set. The donor must not already contain any key in that
namespace except `build-kind=source-build` on a schema-3 source-build donor.
The metadata build replaces that value and adds exactly the other three keys;
unknown reserved labels are rejected.

Top-level image creation time and history are deliberately excluded from
runtime equivalence because a metadata-only Docker build necessarily creates
new non-filesystem history records. The immutable target image ID still binds
those daemon-produced values. RootFS equality, the fixed no-`RUN`/`COPY`/`ADD`
Dockerfile, `--iidfile`, and normalized runtime `Config` equality provide the
filesystem and execution-equivalence proof; the design does not claim that the
complete image-inspection JSON is byte-identical.

Post-build inspection requires:

- a valid target immutable ID different from the donor ID;
- exact equality among the `--iidfile` result, target-tag image ID, and
  immutable-ID inspection;
- identical RootFS type and ordered layer/diff-ID list;
- identical OS, architecture, and variant;
- an identical normalized image `Config` after replacing only the approved
  source environment value, OCI revision, and exact reuse labels;
- unique environment-variable names, including exactly one source SHA;
- exact agreement, for every overlapping field, among image labels, donor
  manifest, target manifest payload, and runtime-input digest; the target
  manifest separately binds the donor manifest's exact byte digest.

In particular, command, entrypoint, user, working directory, health check,
ports, volumes, shell, all other environment values, and all unrelated labels
must remain unchanged. A target image appearing without a published manifest is
retained as failed evidence and blocks a retry until explicitly investigated;
the tool never deletes donor or ambiguous target evidence automatically.

## Manifest schemas

New manifests use strict schema 3. Existing top-level identity and deployment
asset fields remain, and one exact `build` discriminated union is added. The
complete top-level field set is exactly `approved_remote_ref`, `build`,
`deployment_assets`, `docker_daemon_id`, `image_id`, `image_reference`,
`schema`, and `source_sha`; the top-level `schema` value is `3`.

A normal source build records:

```json
{
  "schema": 1,
  "kind": "source-build"
}
```

A reused build records this shape, with no optional or unknown fields:

```json
{
  "schema": 1,
  "kind": "verified-runtime-reuse",
  "reused_from": {
    "source_sha": "<40 lowercase hex>",
    "image_reference": "commerce-ops-desk:<donor-sha>",
    "image_id": "sha256:<64 lowercase hex>",
    "manifest_sha256": "<64 lowercase hex>"
  },
  "runtime_equivalence": {
    "schema": 1,
    "policy": "runtime-copy-inputs-v1+pip-timeout-300-v1",
    "donor_copy_inputs_sha256": "<64 lowercase hex>",
    "target_copy_inputs_sha256": "<same 64 lowercase hex>",
    "components": {
      "dockerignore": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "backend": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "frontend": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "scripts": {"donor_sha256": "<hex>", "target_sha256": "<same>"}
    },
    "dockerfile_transition": {
      "policy": "pip-timeout-300-v1",
      "from_sha256": "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a",
      "to_sha256": "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
    }
  }
}
```

The complete manifest still binds the approved remote ref, target source SHA,
target image reference and ID, Docker daemon ID, and target deployment-asset
digests. The donor manifest SHA-256 is over its exact bytes. Schema 2 remains
readable only as legacy source-build evidence; it can neither describe nor
masquerade as reuse. New normal builds emit schema 3 and add only the
`io.github.zhang-zhenghao.commerce-ops.build-kind=source-build` provenance
label beyond their existing image configuration. Angle-bracket strings in the
example are format notation constrained by the surrounding validation rules,
not unresolved design values.

All manifest and candidate-state JSON is decoded with a strict object-pairs
hook that rejects duplicate keys at every nesting level. Non-standard numeric
constants (`NaN`, `Infinity`, and `-Infinity`), booleans in integer fields,
unknown fields, and non-canonical scalar types are rejected before semantic
validation. Canonical serialization never emits those values.

Schema-3 manifests have one canonical byte representation:
`json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)`,
followed by one newline and UTF-8 encoding. Every schema-3 loader must
reserialize the strictly parsed payload with that function and require exact
byte equality with the safely read input; reordered keys, alternate whitespace,
extra newlines, a BOM, and other semantically equivalent encodings are rejected.

Manifest publication is
no-clobber, atomic, and durable: while holding the target lock, open the
verified parent directory once, create a random `0600` temporary file relative
to that directory with `O_EXCL|O_NOFOLLOW`, write the canonical bytes, flush and
`fsync` it, then invoke Linux `renameat2(..., RENAME_NOREPLACE)` between names
relative to that same directory descriptor. `EEXIST` fails without changing
the destination; lack of `RENAME_NOREPLACE` support fails closed rather than
falling back to an overwriting rename. Every ordinary exception before a
successful rename closes and unlinks the verified temporary inode relative to
the retained directory descriptor and `fsync`s that directory; a process or
host crash may leave only a random-name `0600` temporary evidence file, never a
canonical target manifest.

The successful no-clobber rename is the provenance evidence commit point.
After it, the builder still `fsync`s the open file and parent directory,
safe-reads the published bytes, and rechecks the tag before releasing the lock.
A failure in those post-commit checks is reported as indeterminate and leaves
the manifest intact. An existing canonical manifest is evidence, not an
overwrite target. Reuse recovery accepts only the absent-destination and exact
matching-manifest cases described above; `prepare` independently validates and
makes an existing committed manifest durable before consuming it.

## Deployment integration and candidate state

`deploy.py` parses schema 2 and schema 3 into typed internal provenance rather
than passing unvalidated dictionaries. It validates exact field sets, scalar
types, canonical references, digest formats, donor-not-target, equal runtime
digests, the approved policy pair, and label-to-manifest agreement.

`prepare` derives the target SHA only from the canonical build-manifest
filename, acquires the corresponding per-target lock, and then safe-reads and
validates the manifest's matching source SHA. It retains the safely opened
manifest descriptor, completes all schema-appropriate provenance, tag, and
immutable-image checks, then `fsync`s that exact descriptor and the retained
state-directory descriptor before creating candidate data or Docker resources.
This is a durability repair for already committed evidence, not permission to
create a missing source-build manifest. For reuse, `prepare` also requires the
donor image and canonical donor manifest to remain available, match the
recorded digest, and satisfy their original source-build identity. Deployment
assets always come from the target manifest and target checkout, never from the
donor.

Before those side effects, `prepare` uses the same fixed Git binary, isolated
environment, strict ancestry policy, archive projection, canonical inventory,
and Dockerfile digest policy to recompute donor/target equivalence from the
current exact repository. It requires the manifest's approved remote ref to
still equal the target SHA. A manifest and matching labels alone cannot bypass
the source comparison.

Candidate state advances from schema 2 to schema 3 and adds both
`build_provenance` and `build_provenance_sha256`. For a reused image,
`build_provenance` has this exact shape:

```json
{
  "schema": 1,
  "kind": "verified-runtime-reuse",
  "source_manifest_sha256": "<target manifest SHA-256>",
  "reused_from": {
    "source_sha": "<donor SHA>",
    "image_reference": "commerce-ops-desk:<donor SHA>",
    "image_id": "sha256:<donor image ID>",
    "manifest_sha256": "<donor manifest SHA-256>"
  },
  "runtime_equivalence": {
    "schema": 1,
    "policy": "runtime-copy-inputs-v1+pip-timeout-300-v1",
    "donor_copy_inputs_sha256": "<64 lowercase hex>",
    "target_copy_inputs_sha256": "<same 64 lowercase hex>",
    "components": {
      "dockerignore": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "backend": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "frontend": {"donor_sha256": "<hex>", "target_sha256": "<same>"},
      "scripts": {"donor_sha256": "<hex>", "target_sha256": "<same>"}
    },
    "dockerfile_transition": {
      "policy": "pip-timeout-300-v1",
      "from_sha256": "3315d609f482d6f76a3702ab328805efa8cd8b4692810047df912f89a640a42a",
      "to_sha256": "35258f0647df1dd08e0da3a17749eb1b77a4b6086475c18e5c11a954c3c6a69d"
    }
  }
}
```

For a source build it has exactly `schema`, `kind: "source-build"`,
`source_manifest_schema`, and `source_manifest_sha256`.
`source_manifest_schema` is the integer `2` for a legacy manifest or `3` for a
new source-build manifest. The legacy variant requires the target image to have
no label in the reserved CommerceOps provenance namespace; the schema-3 variant
requires exactly `build-kind=source-build` and no other reserved provenance
label. This distinction is never inferred only from the current mutable image.

State loading validates the appropriate exact field set before side effects.
`_assert_state_candidate_ready` derives and safe-reads the canonical target
manifest again, requires its exact byte digest and parsed schema/build arm to
match the persisted provenance, and then rechecks the target image's immutable
ID, tag, daemon, and schema-appropriate labels. For reuse it additionally
safe-reads the canonical donor manifest, requires its exact persisted digest
and source-build arm, and rechecks the donor immutable ID and tag. Thus the
audit chain is not discarded after `prepare`. Route and Caddy transaction
schemas remain unchanged because they already bind the verified candidate's
immutable upstream identity.

Canonical provenance JSON is produced by Python `json.dumps` with
`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`, and no trailing
newline, then encoded as UTF-8. `build_provenance_sha256` is SHA-256 over the
byte prefix
`commerce-ops-desk:candidate-build-provenance:v1\0`, followed by the canonical
JSON byte length as one unsigned eight-byte big-endian integer, followed by the
canonical JSON bytes. Tests pin the bytes and digest. The target manifest hash
is over its exact complete file bytes, so candidate state also binds all
top-level image, daemon, source, ref, and deployment-asset fields.

## Safe file reads

The new trust input cannot rely on the current `lstat`/`resolve` followed by
pathname `read_text` sequence. Private manifest, candidate-state, and verified
deployment-asset reads are changed to a shared pattern:

1. Open and validate the canonical parent directory, retaining its file
   descriptor and device/inode identity.
2. For a nested deployment-asset path, open every directory component relative
   to the preceding descriptor with `O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW`; reject
   `.`/`..`, empty components, symlinks, owner changes, or group/world-writable
   directories. Manifest and state names must be single direct-child names.
3. Observe the final child with `stat(..., follow_symlinks=False)`, then open it
   relative to the retained parent descriptor with
   `O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW`. `O_NONBLOCK` prevents a
   regular-file-to-FIFO or device swap from hanging before `fstat`; it has no
   effect on an accepted regular file.
4. Validate the opened file with `fstat`: regular file, expected owner and mode,
   one link, bounded size, and the same device/inode as the pre-open
   observation. Build manifests and candidate state are deployment-user-owned
   mode `0600` with a 256 KiB limit. The three versioned deployment assets are
   deployment-user-owned mode `0644`, single-link files with the existing 1 MiB
   per-file limit.
5. Read from that one descriptor in a loop until exactly the initial `st_size`
   bytes have been collected, then request one additional byte and require EOF.
   Early EOF, an extra byte, or invalid UTF-8 fails.
6. Repeat `fstat` and relative no-follow `stat`, requiring unchanged device,
   inode, owner, mode, link count, size, nanosecond mtime, and nanosecond ctime;
   also recheck every retained directory identity before accepting the bytes.

Symlinks, hard links, parent replacement, file replacement, short reads,
oversized files, and an identity change at any checkpoint fail closed. Error
messages contain no manifest contents or subprocess output. As stated in the
trust boundary, this detects pathname and ordinary concurrent-write races but
does not claim protection from a malicious same-UID actor that can rewrite an
inode and forge its timestamps.

## Failure semantics and recovery

- Any failure before the no-clobber rename exits non-zero and writes no target
  manifest. A failure after that rename may leave the exact canonical manifest
  present as committed evidence. Reuse may use explicit recovery; `prepare`
  independently revalidates and repairs durability for either build kind.
- A source-build failure never invokes reuse.
- A daemon-ID change, donor retag, unexpected target pre-existence, Git-object
  drift, runtime-input drift, image-config drift, or file identity race stops
  the operation. Reuse recovery accepts only the exact pre-existing image and
  optional manifest described above.
- Temporary source archives and generated build contexts are removed. Existing
  manifests and Docker evidence are retained.
- If reuse target creation succeeds but manifest publication fails or reports
  an indeterminate durability result, the image and any published manifest are
  treated as evidence. The command prints only the image's non-secret full
  immutable ID and the Runbook records the explicit
  `--recover-unmanifested-image <that-id>` procedure. Normal invocations still
  reject the pre-existing tag. Recovery revalidates the entire reuse chain. It
  either publishes an absent manifest without clobbering or accepts and makes
  durable an exact canonical manifest that it independently recomputed; it
  never retags, deletes, or overwrites the image or a differing manifest.
  Invalid evidence remains untouched for an administrator decision.
- A source-build image or manifest left by a failed or indeterminate
  publication is also preserved. Recovery without a donor manifest is rejected
  in policy v1. If its canonical manifest is absent, the image remains blocked;
  if the manifest rename committed, `prepare` may consume it only after the full
  schema-appropriate revalidation and durability repair above. The tool never
  promotes labels alone into proof of a controlled source build.
- `prepare` rejects bad provenance before candidate data, networks, or
  containers are created. Later state drift stops switch and leaves Caddy
  untouched.
- Existing live container, data, Caddy fragment, stopped evidence containers,
  failed staging directories, and quarantines remain unchanged throughout the
  build and prepare phases.

## Test strategy and acceptance

Implementation follows red-green-refactor. The first tests must fail against
the current code and cover at least:

1. Explicit CLI selection and the absence of automatic fallback.
2. Target remote-ref mismatch, non-commit targets, targets not strictly after
   the observed baseline, missing donor objects, non-ancestor donors,
   donor-equals-target, and reuse-of-reuse rejection.
3. Donor manifest wrong directory/name, schema, fields, owner, mode, link count,
   symlink, broken symlink, digest, daemon, tag, image ID, revision, or source
   environment.
4. Added, deleted, renamed, modified, or executable-bit-changed runtime files;
   lockfile drift; `.dockerignore` drift; runtime symlinks or Git links detected
   by the pre-archive tree scan; archive/tree disagreement; missing exact
   anchors; dirty worktree bytes; replacement refs; and hostile global or
   repository-local Git attributes. A fake `git` earlier on `PATH`, hostile
   `GIT_*` variables, and global/system Git configuration must not affect any
   observation.
5. Every Dockerfile change outside the exact approved digest pair, especially
   base image, dependency path, `COPY`, user, health check, entrypoint, and
   command changes.
6. A positive case where only documentation, deployment tooling, backend tests,
   or ignored script tests differ while the approved Dockerfile transition and
   runtime inventory remain valid.
7. Fixed Docker binary and scrubbed daemon environment, immutable donor `FROM`,
   no pull, no network, safe `--iidfile` binding, and a generated Dockerfile
   containing no filesystem or command instruction.
8. RootFS, platform, image ID, image config, source environment, provenance
   label, daemon drift, approved-ref movement, donor manifest/tag replacement,
   and Git-object evidence changes during the build.
9. Strict schema-3 parsing, schema-2 reuse impersonation, unknown build kinds,
   unequal component or combined digests, manifest-to-label mismatch, and
   rejection of reordered, alternatively spaced, BOM-prefixed, or
   extra-newline schema-3 bytes.
10. Parent/file replacement races for manifests, candidate state, and
    deployment assets, including swaps between metadata checks and reads and
    regular-file replacement by a FIFO, socket, or device without blocking.
11. Shared per-target locking across source build, reuse, recovery, and
    `prepare`; unsafe or replaced lock files; two concurrent builders for one
    SHA; rejection of alternate state directories; a hostile `HOME` that cannot
    change the passwd-derived state path; independent builds for different
    SHAs; no manifest and no normally leaked temporary file on
    pre-publication failure; no secret-bearing output; bounded reads; durable
    no-clobber publication; failure after successful rename; `prepare`
    durability repair for committed source-build and reuse manifests; blocking
    an absent-manifest source image; explicit idempotent reuse recovery with
    either an absent or exact canonical manifest; rejection of recovery without
    a donor, build-kind mismatch, and differing recovery evidence; and
    preservation of source-build, donor, and failed evidence.
12. Candidate state carries provenance forward, distinguishes legacy schema-2
    source builds from labelled schema-3 source builds, rebinds the exact target
    manifest, and repeated readiness rejects target or donor manifest, tag,
    image, or label replacement after `prepare` and before any Caddy mutation.
13. Direct execution of both entry points under `/usr/bin/python3 -I`, test
    loading through `spec_from_file_location`, and Makefile Ruff, format, and
    mypy coverage for `verified_provenance.py`, enforced by a tooling contract
    test.

Acceptance requires the focused builder/deployment suites, full `make verify`,
public-history scan, formatting, lint, typing, independent security review,
Verify, and CodeQL to pass on the exact merge SHA. The workstation may resume
only after a fresh exact checkout, release-evidence selection, read-only legacy
attestation, and successful schema-3 reuse build. Candidate preparation,
loopback readiness and build-identity probes, switch, private acceptance, and
rollback/reconcile handling remain governed by the existing Runbook.

Access-code governance is still unresolved. Even after private acceptance, the
public URL, access code, and live-demo call to action must not be published
until those governance checks are separately confirmed.
