# Legacy bootstrap upstream identity hotfix design

## Context and reproduced failures

The approved workstation revision
`36ace8a61470fa9e12440131e028b7d12025df83` passed Verify and CodeQL, but a
read-only pre-switch probe proved that its deployment helper cannot reconstruct
the frozen legacy upstream on port `18087`. No candidate was prepared and no
Caddy, container, data, or route-head state was changed.

The failure has three independent causes:

1. `_upstream_identity_for_host_port()` enumerates `docker container ls --all`.
   Two stopped evidence containers retain the same static
   `HostConfig.PortBindings` value, even though they do not own a listener. The
   helper sends each stopped container into the running-and-healthy validator
   and rejects before identifying the live owner.
2. `_upstream_identity_from_inspection()` requires the inspected Docker
   network to contain only the target container. That is correct for a new
   candidate's private Compose network, but the frozen `v0.2.0` container uses
   Docker's shared default `bridge`, which legitimately contains unrelated
   peers.
3. The recorded legacy fingerprint
   `b443797953fc44f8acd0e8a353bec2853d4a100fdf113a48d2815754ce4c30ac`
   cannot be recomputed from any versioned evidence. Its test compares the
   production constant to the same literal and therefore proves only that the
   literal was copied twice. A new read-only attestation of the unchanged live
   container produces
   `892810d70dc768db1db79f84276fd6df9b3e83085e34fe8089b604532db11e89`.

The live container was created before the original 2026-10-09 evidence record,
has restart count zero, is healthy, is the only running owner of the loopback
binding, and still serves the exact frozen Caddy route. The repository does not
contain enough evidence to attribute the old fingerprint mismatch to one
specific historical mistake. This hotfix therefore records a reproducible new
attestation instead of inventing an explanation.

## Chosen approach

### Discover only active port owners

Host-port discovery will enumerate full IDs from running containers only:

```text
docker container ls --quiet --no-trunc
```

Every matching inspection still has to pass the existing exact loopback
binding, running, healthy, image, source revision, mount, data inode, network,
runtime, and Docker-daemon checks. The final result must still contain exactly
one match. A container that stops between listing and inspection fails closed;
a stopped container with stale binding metadata is retained as evidence but is
not treated as a listener.

### Separate network membership from network exclusivity

The generic upstream reconstruction primitive will take a keyword-only
`require_exclusive_network` policy. Both policies always require:

- exactly one network on the target container;
- exact agreement on network name and ID between container and network views;
- the target container's exact name and endpoint ID in the network view.

When exclusivity is required, the network's endpoint set must additionally be
exactly the target container. Hardened routes always use this mode. Legacy
routes use membership-only mode because the frozen runtime is on Docker's
shared default bridge.

A route-aware readiness wrapper will derive the policy solely from the
validated route profile: `hardened-v1` is exclusive and `legacy-v0.2.0` is
membership-only. Switch, automatic restoration, reconcile, and rollback will
all use that wrapper, so a legacy backup remains verifiable without weakening a
hardened route. The separate candidate network contract continues to require a
single endpoint before candidate state is created.

### Make the frozen evidence reproducible

The legacy constant will be updated only to the fingerprint recomputed from the
2026-10-10 read-only payload. The regression test will construct the complete
`UpstreamIdentity` payload and pass it through the production fingerprint
function. The payload binds the current Docker daemon, container and image IDs,
image reference, OCI source SHA, loopback port, data path/device/inode, network
and endpoint IDs, and runtime projection hash.

The Runbook will record the attestation date, fingerprint, ownership semantics,
and the distinction between shared legacy membership and private candidate
exclusivity. Any later drift still stops bootstrap; an operator must never
replace the constant ad hoc during a release.

## Alternatives rejected

- Deleting stopped evidence containers would hide the discovery bug and remove
  diagnostic material without proving which process owns the live port.
- Disconnecting unrelated projects from the default bridge, or rebuilding the
  legacy live container onto a private network, would mutate shared or live
  infrastructure merely to satisfy an incorrect generic invariant.
- Globally removing the singleton network check would weaken hardened candidate
  isolation and allow unnoticed lateral peers on a trusted private network.
- Adding network policy to the persisted upstream schema would be
  self-describing, but would expand this bootstrap-only compatibility fix into
  a ledger/state migration. The route profile already provides the required
  immutable policy discriminator.

## Error handling and safety

- Invalid or truncated Docker IDs, malformed inspections, multiple running
  owners, unhealthy owners, endpoint disagreement, and any hardened network
  peer remain hard failures.
- Legacy shared-network mode ignores only unrelated peer entries; it never
  relaxes validation of the selected container's own endpoint.
- A race that stops or replaces the selected container fails during inspection
  or the repeated readiness check before any Caddy mutation.
- This hotfix does not change Caddy bytes, route transaction schemas, candidate
  data, access-code governance, or public-release policy.

## Verification and acceptance

Implementation is accepted only when:

1. New tests first fail because discovery includes stopped containers, legacy
   extraction rejects a peer, and the frozen evidence constant is not derived
   from the complete attested payload.
2. Running-only discovery still rejects truncated IDs and requires exactly one
   healthy matching owner.
3. Membership-only mode accepts an unrelated peer but rejects target endpoint
   name/ID drift; exclusive mode rejects the same peer.
4. Route-aware tests prove legacy readiness is membership-only and hardened
   readiness remains exclusive through switch, restoration, reconcile, and
   rollback paths.
5. Candidate private-network contract tests remain unchanged and green.
6. The complete workstation suite, full repository verification, public-history
   scan, formatting, lint, typing, and independent review pass.
7. A new exact commit passes Verify and CodeQL, and the real workstation
   read-only bootstrap loader succeeds before build, prepare, or switch resumes.
