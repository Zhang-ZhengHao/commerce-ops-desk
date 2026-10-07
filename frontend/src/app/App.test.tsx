import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';

const managerSession = {
  workspace: {
    id: '891a8728-df4b-4f54-b7f8-4afea330835c',
    name: 'Demo workspace 835C',
    expires_at: '2026-10-07T16:00:00Z',
  },
  identity: {
    user_id: '10f35df9-e1d4-4e02-a070-d73abf7accf7',
    membership_id: 'efbb38c3-dcc9-4f99-92de-e900670b1616',
    display_name: 'Demo Manager',
    role: 'manager',
  },
  available_roles: ['manager', 'agent'],
  csrf_token: 'manager-csrf-token',
} as const;

const agentSession = {
  ...managerSession,
  identity: {
    user_id: '6eb6b3c7-33c5-4a0c-9447-2671d7e938a5',
    membership_id: '724f848d-6a6b-4570-ad62-ea256d8dba01',
    display_name: 'Demo Agent',
    role: 'agent',
  },
  csrf_token: 'agent-csrf-token',
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function installFetch(...responses: Response[]) {
  const fetchMock = vi.fn<typeof fetch>();
  for (const response of responses) {
    fetchMock.mockResolvedValueOnce(response);
  }
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe('CommerceOps Desk demo identity', () => {
  it('checks for a session before offering enabled Manager and Agent entry actions', async () => {
    const fetchMock = installFetch(jsonResponse({ detail: 'Not authenticated' }, 401));

    render(<App />);

    expect(screen.getByRole('status')).toHaveTextContent(/checking for an active demo/i);
    expect(await screen.findByRole('button', { name: /enter as manager/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /enter as agent/i })).toBeEnabled();
    expect(
      screen.getByRole('heading', {
        level: 1,
        name: /turn ecommerce exceptions into accountable work/i,
      }),
    ).toBeVisible();
    expect(
      screen.getByRole('heading', { name: /built for a safe public demo/i }),
    ).toBeVisible();
    expect(
      screen.getByRole('heading', { name: /planned workflow/i }),
    ).toBeVisible();
    expect(screen.getByText(/product direction.*not implemented in i02/i)).toBeVisible();
    expect(
      screen.queryByRole('heading', { name: /one accountable path/i }),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/session', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      method: 'GET',
    });
  });

  it('creates a Manager workspace and renders its server-issued identity', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse({ detail: 'Not authenticated' }, 401),
      jsonResponse(managerSession, 201),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /enter as manager/i }));

    expect(
      await screen.findByRole('heading', { name: 'Manager workspace' }),
    ).toBeVisible();
    expect(screen.getByText('Demo workspace 835C')).toBeVisible();
    expect(screen.getByText('Demo Manager')).toBeVisible();
    expect(screen.getByText(/manager access/i)).toBeVisible();
    expect(
      screen.getByRole('heading', { name: /session protections/i }),
    ).toBeVisible();

    const [, request] = fetchMock.mock.calls[1];
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/demo/workspaces');
    expect(request).toMatchObject({
      credentials: 'same-origin',
      method: 'POST',
      body: JSON.stringify({ initial_role: 'manager' }),
    });
    expect(new Headers(request?.headers).get('Idempotency-Key')).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });

  it('enters directly as Agent without exposing a Manager identity first', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse({ detail: 'Not authenticated' }, 401),
      jsonResponse(agentSession, 201),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /enter as agent/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(screen.getByRole('heading', { name: /agent workspace/i })).toBeVisible();
    expect(screen.getByText(/agent access/i)).toBeVisible();
    expect(screen.queryByText('Demo Manager')).not.toBeInTheDocument();
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      initial_role: 'agent',
    });
  });

  it('restores the active identity after a browser refresh', async () => {
    installFetch(jsonResponse(agentSession));

    render(<App />);

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(screen.getByRole('heading', { name: /agent workspace/i })).toBeVisible();
    expect(screen.queryByRole('button', { name: /enter as manager/i })).not.toBeInTheDocument();
  });

  it('switches roles with the in-memory CSRF token and a fresh idempotency key', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      jsonResponse(agentSession),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /switch to agent/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(screen.getByRole('heading', { name: /agent workspace/i })).toBeVisible();
    const [, request] = fetchMock.mock.calls[1];
    const headers = new Headers(request?.headers);
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/demo/role');
    expect(request).toMatchObject({
      credentials: 'same-origin',
      method: 'POST',
      body: JSON.stringify({ role: 'agent' }),
    });
    expect(headers.get('X-CSRF-Token')).toBe('manager-csrf-token');
    expect(headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
    expect(window.localStorage).toHaveLength(0);
    expect(window.sessionStorage).toHaveLength(0);
  });

  it('lets a visitor retry session recovery after a transient error', async () => {
    const user = userEvent.setup();
    installFetch(
      jsonResponse({ detail: 'Service unavailable' }, 503),
      jsonResponse({ detail: 'Not authenticated' }, 401),
    );

    render(<App />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /could not check for an active demo/i,
    );
    await user.click(screen.getByRole('button', { name: /try again/i }));

    expect(await screen.findByRole('button', { name: /enter as manager/i })).toBeEnabled();
  });

  it('recovers a completed role switch when the cookie changed before the body was readable', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      new Response('not-json', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
      jsonResponse(agentSession),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /switch to agent/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/unreadable response/i);

    await user.click(screen.getByRole('button', { name: /retry role switch/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/session');
  });

  it('restores the current CSRF token and reuses the command key before retrying a role switch', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      jsonResponse({ detail: 'Role service is temporarily unavailable' }, 503),
      jsonResponse(managerSession),
      jsonResponse(agentSession),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /switch to agent/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /role service is temporarily unavailable/i,
    );

    await user.click(screen.getByRole('button', { name: /retry role switch/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/session');
    expect(fetchMock.mock.calls[3]?.[0]).toBe('/api/demo/role');
    const firstHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    const retryHeaders = new Headers(fetchMock.mock.calls[3]?.[1]?.headers);
    expect(retryHeaders.get('Idempotency-Key')).toBe(
      firstHeaders.get('Idempotency-Key'),
    );
    expect(retryHeaders.get('X-CSRF-Token')).toBe(managerSession.csrf_token);
  });

  it('replays with the old session when a completed switch lost the response headers', async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(managerSession))
      .mockRejectedValueOnce(new TypeError('The connection closed before headers arrived.'))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Not authenticated' }, 401))
      .mockResolvedValueOnce(jsonResponse(agentSession));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /switch to agent/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/connection closed/i);

    await user.click(screen.getByRole('button', { name: /retry role switch/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/session');
    expect(fetchMock.mock.calls[3]?.[0]).toBe('/api/demo/role');
    const firstHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    const replayHeaders = new Headers(fetchMock.mock.calls[3]?.[1]?.headers);
    expect(replayHeaders.get('Idempotency-Key')).toBe(
      firstHeaders.get('Idempotency-Key'),
    );
    expect(replayHeaders.get('X-CSRF-Token')).toBe(managerSession.csrf_token);
  });

  it('reuses the same idempotency key when retrying a failed workspace command', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse({ detail: 'Not authenticated' }, 401),
      jsonResponse({ detail: 'Capacity is temporarily full' }, 503),
      jsonResponse(managerSession, 201),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /enter as manager/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/capacity is temporarily full/i);
    await user.click(screen.getByRole('button', { name: /retry manager entry/i }));

    expect(await screen.findByText('Demo Manager')).toBeVisible();
    const firstKey = new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get(
      'Idempotency-Key',
    );
    const retryKey = new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get(
      'Idempotency-Key',
    );
    expect(retryKey).toBe(firstKey);
  });
});
