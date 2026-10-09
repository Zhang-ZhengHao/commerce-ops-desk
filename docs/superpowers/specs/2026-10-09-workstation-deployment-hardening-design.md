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
switch, or smoke verification can succeed. It includes the non-root user,
read-only root filesystem, dropped capabilities, no-new-privileges, init,
restart policy, one loopback-only published application port, one expected
data bind mount, the expected tmpfs, and the security-relevant application
environment. Unknown mounts, bindings, privilege, or changed values are a
hard failure.

Tests will first demonstrate acceptance of a malicious inspection and then
cover each rejected mutation independently.

## Caddy transaction and rollback provenance

A single fixed, root-owned lock serializes the complete read, backup, install,
validate, reload, smoke, and possible restore transaction. After installing a
fragment, the helper records its digest. Automatic restoration proceeds only
when the currently installed fragment still has that digest; otherwise it
stops and reports the concurrent change instead of overwriting it.

Each backup is accompanied by provenance stored in a root-owned deployment
ledger outside the deploy user's writable backup directory. The ledger binds
the backup's canonical path, content digest, creation transaction, and the one
allowed CommerceOps site identity. Rollback accepts only a ledger entry and a
fragment that passes a structural allowlist: exactly the CommerceOps address,
exactly one expected reverse-proxy upstream, and no additional site block.
Self-named files without root-owned provenance are rejected.

Tests inject failures at validation, reload, smoke, and restoration. They also
exercise lock contention, digest-CAS refusal, forged backup rejection, and
extra-site rejection.

## Source and image identity

The documented build path first requires a clean checkout whose `HEAD` equals
the approved remote `main` SHA. The image build context is produced from that
exact commit rather than from the mutable working tree. After build, the
helper verifies both the OCI revision label and the immutable local image ID;
deployment state records both. A dirty checkout or a mismatched remote SHA
fails before Docker receives a context.

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
