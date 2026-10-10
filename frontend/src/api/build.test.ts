import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  BuildIdentityError,
  getBuildIdentity,
  type BuildIdentity,
} from './build';

const validSourceSha = '0123456789abcdef0123456789abcdef01234567';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('public build identity API client', () => {
  it('loads the exact no-store build contract with same-origin credentials', async () => {
    const identity: BuildIdentity = {
      service: 'commerce-ops-desk',
      version: '0.2.1',
      source_sha: validSourceSha,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(identity));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getBuildIdentity()).resolves.toEqual(identity);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith('/api/build', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      method: 'GET',
    });
  });

  it('accepts an explicitly unverified local source build', async () => {
    const identity: BuildIdentity = {
      service: 'commerce-ops-desk',
      version: '0.2.1',
      source_sha: null,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(identity)),
    );

    await expect(getBuildIdentity()).resolves.toEqual(identity);
  });

  it.each([
    { service: 'other-service', version: '0.2.1', source_sha: validSourceSha },
    { service: 'commerce-ops-desk', version: '0.2.0', source_sha: validSourceSha },
    { service: 'commerce-ops-desk', version: '0.2.1', source_sha: '0123456' },
    {
      service: 'commerce-ops-desk',
      version: '0.2.1',
      source_sha: validSourceSha.toUpperCase(),
    },
    {
      service: 'commerce-ops-desk',
      version: '0.2.1',
      source_sha: validSourceSha,
      injected: '<script>alert(1)</script>',
    },
    ['commerce-ops-desk', '0.2.1', validSourceSha],
    null,
  ])('rejects an unexpected success payload without retaining its text', async (body) => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body)),
    );

    const error = await getBuildIdentity().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(BuildIdentityError);
    expect(error).toMatchObject({ message: 'Build identity unavailable.' });
    expect(String(error)).not.toContain('<script>');
  });

  it.each([
    ['a network failure', () => Promise.reject(new Error('upstream secret text'))],
    ['an unreadable response', () => Promise.resolve(new Response('{not-json'))],
    ['an unsuccessful response', () => Promise.resolve(jsonResponse({ detail: 'private' }, 503))],
  ])('uses one stable local error for %s', async (_caseName, responseFactory) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(responseFactory));

    const error = await getBuildIdentity().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(BuildIdentityError);
    expect(error).toMatchObject({ message: 'Build identity unavailable.' });
    expect(String(error)).not.toMatch(/upstream secret text|private|not-json/);
  });
});
