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

1. Atomically create one unpredictable, runner-owned `0700` directory under
   the selected tmpfs. The database is a pre-created `0600` regular file inside
   that directory, so no predictable final-path symlink can win between config
   evaluation and server startup.
2. Record the private directory's canonical path, device, inode, owner, and the
   exact database path. Repeated Playwright config evaluation must reuse that
   identity; teardown must re-lstat and match it before touching a file.
3. Before allocating a database, scan only private directory names matching
   the exact project PID/port/random-token contract. Reclaim a directory only
   when its PID no longer exists, its inode/owner/mode still match, and it
   contains only the three known regular database files. Keep live,
   permission-unknown, malformed, symlinked, replaced, or unrelated entries.
4. Register teardown for the current database and its two known sidecars, then
   remove the now-empty private directory. A parent-directory replacement or
   inode mismatch is a hard refusal, not a best-effort delete.

The lifecycle logic will be a small TypeScript module with Node-environment
Vitest coverage. Tests use a temporary directory and a supplied liveness
probe, so they demonstrate selection behavior without touching real shared
memory. Adversarial tests pre-place a final-path symlink and replace the parent
directory after preparation; neither case may write or delete the outside
target.

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
