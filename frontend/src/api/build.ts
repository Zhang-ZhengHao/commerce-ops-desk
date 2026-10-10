export const PUBLIC_REPOSITORY_URL =
  'https://github.com/Zhang-ZhengHao/commerce-ops-desk';

export interface BuildIdentity {
  service: 'commerce-ops-desk';
  version: '0.2.1';
  source_sha: string | null;
}

export class BuildIdentityError extends Error {
  constructor() {
    super('Build identity unavailable.');
    this.name = 'BuildIdentityError';
  }
}

const SOURCE_SHA = /^[0-9a-f]{40}$/;
const BUILD_KEYS = ['service', 'version', 'source_sha'] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isBuildIdentity(value: unknown): value is BuildIdentity {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  if (keys.length !== BUILD_KEYS.length || !BUILD_KEYS.every((key) => key in value)) {
    return false;
  }

  return (
    value.service === 'commerce-ops-desk' &&
    value.version === '0.2.1' &&
    (value.source_sha === null ||
      (typeof value.source_sha === 'string' && SOURCE_SHA.test(value.source_sha)))
  );
}

export async function getBuildIdentity(): Promise<BuildIdentity> {
  let response: Response;
  try {
    response = await fetch('/api/build', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      method: 'GET',
    });
  } catch {
    throw new BuildIdentityError();
  }

  if (!response.ok) throw new BuildIdentityError();

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new BuildIdentityError();
  }

  if (!isBuildIdentity(body)) throw new BuildIdentityError();
  return body;
}
