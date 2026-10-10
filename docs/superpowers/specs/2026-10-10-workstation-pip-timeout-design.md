# Workstation pip timeout hotfix design

## Context

The approved `1618a3c05a1e9378795380c7bc3f00d6e863a4ad` workstation image build failed in the runtime dependency layer. A private, instrumented replay proved that `pip` reached PyPI, resolved every locked dependency, and then timed out while reading the 5.2 MB `psycopg-binary==3.3.6` wheel from `files.pythonhosted.org`. The container was not OOM-killed, disk and inode capacity were healthy, and the same lock file had built successfully before. The failure is therefore a slow external read exceeding pip's default network timeout, not a source, version, or capacity error.

## Decision

Add `--timeout 300` to the Dockerfile's build-time `pip install` command. Keep the existing pinned `backend/requirements.lock`, `--no-cache-dir`, approved-Git-object build flow, OCI revision checks, and private build manifest unchanged.

This is deliberately narrower than adding a wheelhouse or selecting a third-party package mirror. It gives the existing trusted index enough time to deliver large wheels without introducing a new artifact lifecycle or supply-chain endpoint. It also avoids runtime configuration changes because the option applies only to the image build command.

## Verification

Add a container packaging contract that requires the locked runtime install to include `--timeout 300`. Prove the test fails before the Dockerfile change and passes afterward. Then run the full repository verification gate and build the approved merge SHA on the workstation. The deployment may continue only if the final image label, runtime source SHA, and private build manifest all match that merge SHA.

## Rollback and scope

The hotfix changes only the Dockerfile and its packaging contract. Reverting the commit restores the previous timeout. It does not change application behavior, dependency versions, deployment state, Caddy configuration, live data, access-code governance, or public-release policy.
