# Workstation compatibility hotfix design

## Context and observed failures

The approved `v0.2.1` candidate at
`bf78fe34d120e305baa151a97fb351235a0d37c4` built successfully on the shared
workstation, but fresh candidate preparation exposed two deterministic gaps in
the deployment helper:

1. Docker Compose `2.40.3+ds1-0ubuntu1` rejects `create --no-deps` because the
   `create` subcommand does not register that option. The same release supports
   `up --no-deps`.
2. `prepare` creates candidate data as numeric `10001:10001` with mode `0700`.
   The deployment operator cannot subsequently move that directory between
   parents: a same-filesystem `renameat2(RENAME_NOREPLACE)` returns `EACCES`.
   The existing quarantine implementation performs that move without
   privilege, so it cannot recover the directory it created.

The failed prepare did not create a candidate container, network, or state
file and did not change Caddy or live data. It retained one empty candidate
data directory and one empty quarantine archive for inspection.

This hotfix changes only workstation candidate creation and recovery. It does
not publish the private route, distribute an enterprise access code, weaken
the immutable-image checks, or alter the Caddy transaction model.

## Chosen approach

### Compose command compatibility

Remove `--no-deps` only from the initial `docker compose create` invocation.
Before that invocation, the helper already parses the same digest-verified
in-memory Compose document with the same environment and requires
`config --services` to return exactly `commerce-ops-desk`. The create command
continues to name that sole service explicitly, and the helper then requires
the project to contain exactly one expected container. There is therefore no
second service or dependency for create to expand.

The final `up` invocation keeps `--no-deps`, which Compose 2.40.3 supports.
Changing create to `up --no-start`, version-detecting flags, or maintaining two
production command paths would add behavior and compatibility surface without
improving the single-service guarantee.

### Privileged candidate-data quarantine

Keep the existing per-SHA prepare lock, unprivileged preflight, archive
creation, state-file move, postconditions, and retry semantics. Move only the
runtime-owned candidate-data rename into one isolated fixed-argument helper:

```text
sudo /usr/bin/python3 -I -c <versioned helper> <fixed validated fields>
```

The helper receives only internally derived direct-child names and canonical
decimal identity fields. It reopens the canonical application root, the new
archive, and the candidate data with `O_NOFOLLOW` directory descriptors. It
binds each object to the owner, mode, device, and inode observed by the parent;
requires the source and destination to share a filesystem; keeps the source
descriptor open across the operation; and publishes only the literal archive
entry `data` with Linux `renameat2(RENAME_NOREPLACE)`.

After the rename, the helper requires the destination to be the same opened
inode, the source name to be absent, and the root and archive paths to remain
bound to their original inodes. It fsyncs the archive and application-root
directories before returning a strict identity receipt. The unprivileged
parent validates that receipt, repeats the destination/source and hierarchy
checks while still holding the prepare lock, and only then moves any
operator-owned state file through the existing unprivileged no-clobber path.

There is no `mv`, overwrite, chown, permission relaxation, delete, or
EACCES-triggered fallback. Data always uses the privileged helper; state never
does. If the helper reports failure after a possible rename, recovery stops
without restoration or deletion. A data-only archive plus retained state is an
expected resumable shape: a later invocation creates a new archive for the
remaining state while preserving the first archive.

## Trust boundary

The versioned inline helper is supplied by the exact checked-out deployment
tool, so the human operator remains part of the trusted computing base. This
is a narrow operational privilege boundary, not protection from a malicious
operator or an uncoordinated root writer. Dirfd and inode checks detect
unexpected replacement and fail closed; Linux does not provide an
inode-conditional rename that can make all cross-process observations atomic.
Consequently, an indeterminate or partially committed result is preserved for
inspection and never automatically reversed.

## Alternatives rejected

- Changing candidate ownership to the deployment user, temporarily chmodding,
  or chowning before quarantine would weaken the runtime isolation contract and
  mutate retained evidence.
- Running the whole quarantine flow as root would unnecessarily enlarge the
  privileged surface and move the operator-owned state file across the trust
  boundary.
- Calling `sudo mv` would lose no-follow, inode binding, no-clobber, and durable
  publication guarantees.
- Manually bypassing `deploy.py` on the workstation would break the exact-SHA
  recovery and audit trail.

## Verification and acceptance

Implementation is accepted only when:

1. Compose argv tests fail first against the unsupported create flag, then
   require the exact supported create command while retaining `up --no-deps`.
2. A regression test fails first because candidate data is not delegated to a
   privileged helper. Tests then exercise the helper without requiring real
   sudo by substituting only the executable while preserving its exact argv.
3. Helper tests cover inode binding, owner/mode checks, no-follow handling,
   no-clobber behavior, destination races, strict receipt parsing, fsync order,
   and data-first partial recovery. Existing state-only, data-only, combined,
   lock, and resume tests remain green.
4. Workstation deployment tests, all workstation tests, static checks, the
   complete `make verify`, public-history scan, and independent review pass.
5. The new exact SHA passes GitHub Verify and CodeQL.
6. On the real workstation, the new versioned helper quarantines the retained
   old-SHA data directory, after which a fresh new-SHA prepare passes on Compose
   2.40.3 before any route switch is attempted.
