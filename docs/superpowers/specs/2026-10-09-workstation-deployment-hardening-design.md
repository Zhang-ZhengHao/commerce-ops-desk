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

Keep the versioned deployment helper and reject unrecognized state at every
transition it initiates, within the documented cooperating-writer boundary.
This preserves useful deployment evidence for the portfolio while avoiding new
registry or signing infrastructure that is not available in the current
environment.

The human deployment operator is part of the trusted computing base. The
versioned helper invokes isolated Python under `sudo`, but its embedded root
code is supplied by the operator's checkout; it is not a preinstalled,
root-owned program with a restricted sudoers interface. Root ownership of the
ledger therefore protects the chain from unprivileged application processes,
accidental same-user file writes, stale operations, and replay through this
helper. It does not protect against a malicious operator who can alter the
helper or invoke equivalent root commands. Building that stronger boundary
would require a separately installed root-owned helper and constrained
sudoers policy and is outside this revision.

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

A single fixed, root-owned advisory lock serializes the complete read, backup,
install, validate, reload, smoke, and possible restore transaction among
cooperating invocations of this tool. After installing a fragment, the helper
rechecks its digest before an automatic restoration. This is defense in depth
against stale tool state, not an atomic compare-and-swap against a root writer
that edits Caddy without taking the tool lock. Root and out-of-band privileged
writers are explicitly outside this automation boundary and must coordinate
changes on the same lock. The design does not claim to eliminate the root-level
TOCTOU window between a digest read and a later replacement.

Each change creates an immutable schema-3 transaction in a root-owned `0700`
directory. Its canonical ledger binds a random bootstrap ID, transaction ID,
operation, exact parent transaction and active-byte digest, the one allowed
CommerceOps site, and complete backup and installed `RouteState` values. A
route state includes the fragment digest, managed profile, route revision,
full Docker/network/data/runtime upstream identity, and deployment-asset
identity where applicable. Backup and ledger files are installed as `0600`
root files through a no-clobber hard-link publication step. Their inode and
containing directory are explicitly fsynced before a head can reference them.

The root-owned canonical schema-1 `active.json` is the only rollback authority.
It binds the current transaction ledger by exact path and byte digest and
repeats its installed route. Loading a head verifies active, ledger, and backup
ownership and canonical bytes; derived paths; all cross-record identities; the
backup byte digest; and its exact managed structure. Rollback accepts only the
backup named by that current head. It creates a new transaction and advances
the head, so T1 cannot be replayed after a T1 rollback has produced T2.

The first hardened cutover may bootstrap only when `active.json` is absent and
the current deployment matches the complete frozen legacy identity: exact
fragment bytes and port, exact adapted active Caddy route, readiness without a
revision header, and the read-only-observed Docker daemon, container, image,
data directory, network endpoint, and runtime fingerprint. That profile is
accepted only for reading, backup, automatic restore, and current-head
rollback; every new candidate installation must use the hardened canonical
profile. A permissive "any one-site fragment" migration is not allowed.

The migration shape was checked read-only on the workstation on 2026-10-09.
The installed Caddy reported version 2.6.2. The complete live fragment was the
frozen legacy template rendered at port 18087 (SHA-256
`740ab123464b07d8e6460c974abc901c4025fc08f34a955402ba5db574994e3a`),
and adaptation produced one `:80` server, one exact host route, and one
loopback reverse-proxy upstream. No workstation state was changed while
collecting this compatibility evidence.

Before a route change, the helper requires the target container to be healthy
and revalidates its complete Docker, network, data, runtime, and loopback-port
identity. This prevents a stopped rollback target whose released port has been
claimed by an unrelated local listener from becoming public. The install path
then passes digest-bound in-memory fragment bytes directly to isolated Python
under `sudo`, atomically replaces only the managed site, rereads the exact
installed bytes, validates Caddy, reloads, compares the active route, checks
the response revision marker, and revalidates the complete upstream. Only
after every check succeeds does a compare-and-swap-style privileged operation
publish `active.json`. If publication reports an error, the helper rereads the
head: the exact proposed head means success, the exact parent permits verified
restoration, and any other state is external drift and forbids restoration.

The site and head replacements are necessarily two filesystem operations. A
process killed between them can leave the site ahead of the recorded head, and
a failed attempt may leave an immutable orphan ledger. Before changing the
site, the command flushes the exact transaction-ledger path to the operator.
The lock-protected `reconcile --transaction` command accepts only that fully
validated transaction when its recorded parent exactly equals the current
head (including the no-head bootstrap case). If the site equals the orphan's
installed route, reconcile repeats target-health, configuration, reload,
active-route, marker, and upstream checks before advancing the head. If those
checks fail before commit, reconcile rereads and binds the unchanged parent and
installed site, preflights the trusted backup, restores it, and repeats the
complete route checks while leaving the parent head unchanged. If the site
already equals the transaction backup, it revalidates that loaded route and
also leaves the parent head unchanged. An exact already-committed head is never
restored implicitly. Any other site, parent, ledger, or unrecoverable upstream
is refused. Orphans never authorize rollback because rollback still accepts
only the transaction named by `active.json`.

Tests inject failures at every pre-commit verification, before and after head
replacement, and in restoration. They also exercise bootstrap and chained
crash snapshots, pre-install target-health refusal, lock contention,
stale-digest refusal, forged or noncanonical state, legacy bootstrap,
current-head-only rollback, replay rejection, marker spoofing, and extra-site
rejection. Documentation must not claim cross-file atomicity, protection from
the trusted deployment operator, or exclusion of a root writer that ignores
the shared lock.

## Source and image identity

The documented build path requires the approved remote `main` ref to equal the
selected SHA. Every Git command runs with replace objects disabled and without
inherited repository-redirection variables; the resolved repository top level
must be the requested checkout. Archive runs through a temporary bare object
view with a clean config and attributes namespace, backed only by the approved
repository's object directory. This prevents mutable `.git/info/attributes`
or global/system attribute files from applying `export-ignore` or
`export-subst` to the context. Versioned attributes inside the selected commit
remain part of that approved source policy. Dirty and untracked checkout
content is deliberately ignored rather than treated as an error.

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
