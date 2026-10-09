# E2E database lifecycle design

## Context

The Playwright harness intentionally places SQLite on `/dev/shm` because the
workspace is NFS-backed and SQLite WAL locking is not reliable there. The
harness currently creates a PID- and port-scoped database but never removes
the database, WAL, or shared-memory sidecar. Repeated successful runs filled
the 64 MiB tmpfs and caused otherwise valid browser flows to return HTTP 500
with `sqlite3.OperationalError: database or disk is full`.

## Chosen approach

Keep the local tmpfs and add an explicit, project-scoped lifecycle module used
by Playwright configuration:

1. Before allocating a database, scan only filenames matching the exact
   `commerce-ops-desk-playwright-<pid>-<port>.sqlite3[-wal|-shm]` contract.
2. Delete a matched group only when its recorded PID no longer exists. Keep
   live, permission-unknown, malformed, symlinked, and unrelated entries.
3. Register teardown for the current database and its two known sidecars.
4. Refuse unsafe paths rather than accepting caller-supplied arbitrary files.

The lifecycle logic will be a small TypeScript module with Node-environment
Vitest coverage. Tests use a temporary directory and a supplied liveness
probe, so they demonstrate selection behavior without touching real shared
memory.

Two alternatives were rejected:

- Moving the database to the NFS-backed workspace would reintroduce the lock
  stalls that motivated `/dev/shm`.
- Manually deleting old files before each local run would repair one sandbox
  but leave CI and future developer environments vulnerable to the same leak.

## Verification

The regression test must first fail against the missing lifecycle behavior.
After implementation, it must prove cleanup of dead-PID database groups,
preservation of active and unrelated files, exact current-run teardown, and
safe handling of malformed or symbolic-link entries. The complete Playwright
suite is then rerun from the previously full tmpfs; successful startup cleanup
must restore capacity and all browser flows must pass.
