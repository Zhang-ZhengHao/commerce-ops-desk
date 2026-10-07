export type DemoRole = 'manager' | 'agent';

export interface DemoWorkspace {
  id: string;
  name: string;
  expires_at: string;
}

export interface DemoIdentity {
  user_id: string;
  membership_id: string;
  display_name: string;
  role: DemoRole;
}

export interface DemoSession {
  workspace: DemoWorkspace;
  identity: DemoIdentity;
  available_roles: DemoRole[];
  csrf_token: string;
}

export class SessionApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'SessionApiError';
  }
}

const JSON_HEADERS = {
  Accept: 'application/json',
  'Content-Type': 'application/json',
} as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isRole(value: unknown): value is DemoRole {
  return value === 'manager' || value === 'agent';
}

function isDemoSession(value: unknown): value is DemoSession {
  if (!isRecord(value)) return false;
  const { workspace, identity, available_roles: availableRoles, csrf_token: csrfToken } =
    value;

  return (
    isRecord(workspace) &&
    typeof workspace.id === 'string' &&
    typeof workspace.name === 'string' &&
    typeof workspace.expires_at === 'string' &&
    isRecord(identity) &&
    typeof identity.user_id === 'string' &&
    typeof identity.membership_id === 'string' &&
    typeof identity.display_name === 'string' &&
    isRole(identity.role) &&
    Array.isArray(availableRoles) &&
    availableRoles.every(isRole) &&
    typeof csrfToken === 'string' &&
    csrfToken.length > 0
  );
}

async function errorFrom(response: Response): Promise<SessionApiError> {
  let message = `Request failed with status ${response.status}.`;

  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === 'string' && body.detail.trim()) {
      message = body.detail;
    }
  } catch {
    // A stable status-based message is safer than exposing an upstream HTML response.
  }

  return new SessionApiError(message, response.status);
}

async function sessionFrom(response: Response): Promise<DemoSession> {
  if (!response.ok) throw await errorFrom(response);

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new SessionApiError('The demo returned an unreadable response.', 502);
  }

  if (!isDemoSession(body)) {
    throw new SessionApiError('The demo returned an unexpected response.', 502);
  }

  return body;
}

export function createCommandKey(): string {
  if (typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }

  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'));

  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-');
}

export async function restoreDemoSession(): Promise<DemoSession | null> {
  const response = await fetch('/api/session', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    method: 'GET',
  });

  if (response.status === 401) return null;
  return sessionFrom(response);
}

export async function createDemoWorkspace(
  role: DemoRole,
  idempotencyKey: string,
): Promise<DemoSession> {
  const response = await fetch('/api/demo/workspaces', {
    body: JSON.stringify({ initial_role: role }),
    credentials: 'same-origin',
    headers: {
      ...JSON_HEADERS,
      'Idempotency-Key': idempotencyKey,
    },
    method: 'POST',
  });

  return sessionFrom(response);
}

export async function switchDemoRole(
  role: DemoRole,
  csrfToken: string,
  idempotencyKey: string,
): Promise<DemoSession> {
  const response = await fetch('/api/demo/role', {
    body: JSON.stringify({ role }),
    credentials: 'same-origin',
    headers: {
      ...JSON_HEADERS,
      'Idempotency-Key': idempotencyKey,
      'X-CSRF-Token': csrfToken,
    },
    method: 'POST',
  });

  return sessionFrom(response);
}
