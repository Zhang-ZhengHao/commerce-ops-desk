# Workstation deployment hardening design

## Context

The portfolio demo already has a scoped blue/green deployment helper, but an
independent review found four fail-open boundaries: container inspection does
not prove the intended sandbox, Caddy mutation is not serialized, rollback
files are self-authenticating, and an OCI revision label does not prove that
the image was built from the approved Git tree. The same review also found
that Caddy failure recovery lacks transaction-level fault-injection tests.

This change hardens only the CommerceOps workstation deployment. It does not
manage another Caddy site, distribute the enterprise access code, or claim
production readiness.

## Chosen approach

Keep the versioned deployment helper and make every privileged transition
fail closed. This preserves useful deployment evidence for the portfolio while
avoiding new registry or signing infrastructure that is not available in the
current environment.

Two alternatives were rejected for this revision:

- Replacing the helper with a manual runbook would reduce implementation risk
  but remove reproducible rollback and runtime-contract evidence.
- Moving builds and deployment to a signed registry pipeline would provide a
  stronger supply-chain boundary, but it requires infrastructure and key
  custody outside this repository.

## Runtime contract

Candidate inspection must match one exact security contract before prepare,
switch, or smoke verification can succeed. The immutable image ID is inspected
as the baseline. Container command, entrypoint, healthcheck, working directory,
volumes, exposed ports, and the complete environment must equal that baseline
plus the documented Compose overrides. This prevents non-prefixed execution
hooks such as `BASH_ENV`, `PYTHONPATH`, or `LD_PRELOAD` from bypassing a
prefix-only check.

The host contract includes the numeric non-root user, read-only root
filesystem, dropped capabilities, an empty capability-add set, no host devices
or device requests, no-new-privileges, init, restart policy, one loopback-only
published application port, one expected private-propagation data bind, the
expected tmpfs, and no host PID, IPC, UTS, user, cgroup, or volume namespace
attachment. Unknown mounts, bindings, privilege, or changed values are a hard
failure.

Tests will first demonstrate acceptance of a malicious inspection and then
cover each rejected mutation independently.

## Caddy transaction and rollback provenance

A single fixed, root-owned lock serializes the complete read, backup, install,
validate, reload, smoke, and possible restore transaction for every invocation
of this tool. After installing a fragment, the helper rechecks its digest
before an automatic restoration. This is defense in depth against stale tool
state, not an atomic compare-and-swap against a root administrator that edits
Caddy without taking the tool lock. Root and out-of-band privileged writers
are explicitly outside this automation boundary and must coordinate changes.

Each backup is accompanied by provenance stored in a root-owned deployment
ledger outside the deploy user's writable backup directory. The ledger binds
the backup's canonical path, content digest, creation transaction, and the one
allowed CommerceOps site identity. Rollback accepts only a ledger entry and a
fragment that passes a structural allowlist: exactly the CommerceOps address,
exactly one expected reverse-proxy upstream, and no additional site block.
Self-named files without root-owned provenance are rejected.

The first hardened cutover must also accept the exact currently deployed
legacy CommerceOps fragment as an input and rollback profile. That profile is
frozen as a versioned template and compared through Caddy's adapted JSON just
like the hardened template. It is accepted only for reading, backup, automatic
restore, and trusted-ledger rollback; every new candidate installation must use
the hardened canonical profile. A permissive "any one-site fragment" migration
is not allowed.

Tests inject failures at validation, reload, smoke, and restoration. They also
exercise lock contention, stale-digest refusal, forged backup rejection,
legacy-profile migration, and extra-site rejection. Documentation must not
claim atomic exclusion of a root writer that ignores the shared lock.

## Source and image identity

The documented build path requires the approved remote `main` ref to equal the
selected SHA. Every Git command runs with replace objects disabled and without
inherited repository-redirection variables; the resolved repository top level
must be the requested checkout. The image build context is produced with
`git archive` from that exact commit rather than from the mutable working tree,
so dirty and untracked checkout content is deliberately ignored rather than
treated as an error.

After build, one image inspection verifies both the OCI revision label and the
immutable local image ID. The builder writes those values, the image reference,
the source SHA, and the approved ref into a private, schema-checked build
manifest. `prepare` requires that manifest, verifies that the current tag still
maps to its recorded immutable ID, inspects the baseline by immutable ID, and
stores the same ID in deployment state. A mutable tag can no longer establish
a new identity between build and prepare.

This is a local provenance control, not a claim of cryptographic software
supply-chain attestation. Registry signatures remain future work.

## Verification and acceptance

The implementation is accepted only when:

1. Every new regression test is observed failing for the intended reason.
2. Deployment unit and contract tests pass after the minimal fixes.
3. Compose 2.40.3 parsing and Caddy 2.6.2 adaptation still pass.
4. The complete repository verification target and public-history scan pass.
5. A second read-only security review returns no P0 or P1 finding.

Real image build, PostgreSQL execution, and workstation cutover remain CI or
workstation gates and must not be represented as locally verified beforehand.
