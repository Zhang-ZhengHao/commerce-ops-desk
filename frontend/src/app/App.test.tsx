import { act, render, screen, waitFor } from '@testing-library/react';
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

const resetManagerSession = {
  ...managerSession,
  workspace: {
    ...managerSession.workspace,
    id: 'd63c1349-e415-4fb2-b8c9-73d089537d83',
    name: 'Demo workspace 7D83',
  },
  csrf_token: 'reset-manager-csrf-token',
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function installFetch(...responses: Array<Response | Error | Promise<Response>>) {
  let responseIndex = 0;
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const path = new URL(String(input), window.location.origin).pathname;
    if (path === '/api/dashboard') {
      return jsonResponse({
        generated_at: '2026-10-07T16:00:00Z',
        summary: { open: 0, approaching_sla: 0, high_severity: 0, resolved: 0 },
        by_rule: [],
      });
    }
    if (path === '/api/cases') {
      return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 });
    }
    if (path === '/api/agents') return jsonResponse({ items: [] });

    const response = responses[responseIndex++];
    if (response instanceof Error) throw response;
    if (!response) throw new Error(`Missing test response for ${path}`);
    return response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function callsTo(fetchMock: ReturnType<typeof installFetch>, path: string) {
  return fetchMock.mock.calls.filter(
    ([input]) => new URL(String(input), window.location.origin).pathname === path,
  );
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
      screen.getByRole('heading', { name: /operational workflow/i }),
    ).toBeVisible();
    expect(screen.getByText(/available in the temporary i03 workspace/i)).toBeVisible();
    expect(screen.getByText('Synthetic exception queue')).toBeVisible();
    expect(screen.queryByText(/signed commerce event/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/not implemented in i02/i)).not.toBeInTheDocument();
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

    const [input, request] = callsTo(fetchMock, '/api/demo/workspaces')[0];
    expect(input).toBe('/api/demo/workspaces');
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
    const [, request] = callsTo(fetchMock, '/api/demo/workspaces')[0];
    expect(JSON.parse(String(request?.body))).toEqual({
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
    const [, request] = callsTo(fetchMock, '/api/demo/role')[0];
    const headers = new Headers(request?.headers);
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

  it('does not let a delayed Manager snapshot overwrite the Agent workspace', async () => {
    const user = userEvent.setup();
    const managerDashboard = deferred<Response>();
    let dashboardReads = 0;
    let caseReads = 0;
    const dashboard = {
      generated_at: '2026-10-07T16:00:00Z',
      summary: { open: 1, approaching_sla: 0, high_severity: 1, resolved: 0 },
      by_rule: [],
    };
    const casePage = (id: string, orderNumber: string) => ({
      items: [{
        id,
        rule_key: 'refund_review',
        case_type: 'refund',
        severity: 'high',
        status: 'assigned',
        due_at: '2026-10-07T17:15:00Z',
        updated_at: '2026-10-07T16:10:00Z',
        version: 1,
        resolution_reason: null,
        resolved_at: null,
        order: {
          id: `${id}-order`,
          order_number: orderNumber,
          amount_minor: 12999,
          currency: 'USD',
          payment_status: 'paid',
          fulfillment_status: 'unfulfilled',
        },
        assignee: {
          membership_id: agentSession.identity.membership_id,
          display_name: agentSession.identity.display_name,
        },
      }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path === '/api/session') return jsonResponse(managerSession);
      if (path === '/api/demo/role') return jsonResponse(agentSession);
      if (path === '/api/dashboard') {
        dashboardReads += 1;
        return dashboardReads === 1
          ? managerDashboard.promise
          : jsonResponse(dashboard);
      }
      if (path === '/api/cases') {
        caseReads += 1;
        return jsonResponse(
          caseReads === 1
            ? casePage('manager-only-case', 'MANAGER-ONLY')
            : casePage('agent-only-case', 'AGENT-ONLY'),
        );
      }
      if (path === '/api/agents') return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not Found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByText('Demo Manager');
    await waitFor(() => expect(dashboardReads).toBe(1));

    await user.click(screen.getByRole('button', { name: /switch to agent/i }));

    expect(
      await screen.findByRole('heading', { name: 'Agent workspace' }),
    ).toBeVisible();
    expect(
      await screen.findByRole('button', { name: 'Open AGENT-ONLY' }),
    ).toBeVisible();

    await act(async () => {
      managerDashboard.resolve(jsonResponse(dashboard));
      await managerDashboard.promise;
    });

    expect(screen.getByRole('button', { name: 'Open AGENT-ONLY' })).toBeVisible();
    expect(
      screen.queryByRole('button', { name: 'Open MANAGER-ONLY' }),
    ).not.toBeInTheDocument();
  });

  it('confirms and resets the workspace with the current CSRF token', async () => {
    const user = userEvent.setup();
    const resetResponse = deferred<Response>();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      resetResponse.promise,
    );

    render(<App />);
    await screen.findByRole('heading', { name: /manager workspace/i });

    await user.click(screen.getByRole('button', { name: /reset demo data/i }));
    expect(screen.getByRole('heading', { name: /reset this workspace/i })).toBeVisible();
    expect(callsTo(fetchMock, '/api/demo/reset')).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: /^reset workspace$/i }));
    expect(screen.getByRole('status')).toHaveTextContent(/resetting workspace/i);
    resetResponse.resolve(jsonResponse(resetManagerSession, 201));

    expect(await screen.findByText('Demo workspace 7D83')).toBeVisible();
    expect(screen.queryByText('Demo workspace 835C')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(callsTo(fetchMock, '/api/dashboard')).toHaveLength(2);
      expect(callsTo(fetchMock, '/api/cases')).toHaveLength(2);
      expect(callsTo(fetchMock, '/api/agents')).toHaveLength(2);
    });
    const [, request] = callsTo(fetchMock, '/api/demo/reset')[0];
    const headers = new Headers(request?.headers);
    expect(request).toMatchObject({
      credentials: 'same-origin',
      method: 'POST',
    });
    expect(headers.get('X-CSRF-Token')).toBe(managerSession.csrf_token);
    expect(headers.get('Idempotency-Key')).toBeNull();
  });

  it('checks the session after a failed reset and requires confirmation before another POST', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      jsonResponse({ detail: 'Reset is temporarily unavailable.' }, 503),
      jsonResponse(managerSession),
    );

    render(<App />);
    await screen.findByRole('heading', { name: /manager workspace/i });
    await user.click(screen.getByRole('button', { name: /reset demo data/i }));
    await user.click(screen.getByRole('button', { name: /^reset workspace$/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/reset is temporarily unavailable/i);
    expect(alert).toHaveTextContent(/current workspace is still active/i);
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
    expect(callsTo(fetchMock, '/api/demo/reset')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/dashboard')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/cases')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/agents')).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: /try reset again/i }));

    expect(screen.getByRole('heading', { name: /reset this workspace/i })).toBeVisible();
    expect(callsTo(fetchMock, '/api/demo/reset')).toHaveLength(1);
  });

  it('accepts a reset committed before its response was lost', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      new TypeError('The reset response connection closed.'),
      jsonResponse(resetManagerSession),
    );

    render(<App />);
    await screen.findByRole('heading', { name: /manager workspace/i });
    await user.click(screen.getByRole('button', { name: /reset demo data/i }));
    await user.click(screen.getByRole('button', { name: /^reset workspace$/i }));

    expect(await screen.findByText('Demo workspace 7D83')).toBeVisible();
    expect(callsTo(fetchMock, '/api/demo/reset')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('returns to public entry when reset recovery finds no active session', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      new TypeError('The reset response connection closed.'),
      jsonResponse({ detail: 'Not authenticated' }, 401),
    );

    render(<App />);
    await screen.findByRole('heading', { name: /manager workspace/i });
    await user.click(screen.getByRole('button', { name: /reset demo data/i }));
    await user.click(screen.getByRole('button', { name: /^reset workspace$/i }));

    expect(await screen.findByRole('button', { name: /enter as manager/i })).toBeEnabled();
    expect(callsTo(fetchMock, '/api/demo/reset')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
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
    expect(callsTo(fetchMock, '/api/demo/role')).toHaveLength(1);
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
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
    const roleCalls = callsTo(fetchMock, '/api/demo/role');
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
    expect(roleCalls).toHaveLength(2);
    const firstHeaders = new Headers(roleCalls[0]?.[1]?.headers);
    const retryHeaders = new Headers(roleCalls[1]?.[1]?.headers);
    expect(retryHeaders.get('Idempotency-Key')).toBe(
      firstHeaders.get('Idempotency-Key'),
    );
    expect(retryHeaders.get('X-CSRF-Token')).toBe(managerSession.csrf_token);
  });

  it('replays with the old session when a completed switch lost the response headers', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(managerSession),
      new TypeError('The connection closed before headers arrived.'),
      jsonResponse({ detail: 'Not authenticated' }, 401),
      jsonResponse(agentSession),
    );

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /switch to agent/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/connection closed/i);

    await user.click(screen.getByRole('button', { name: /retry role switch/i }));

    expect(await screen.findByText('Demo Agent')).toBeVisible();
    const roleCalls = callsTo(fetchMock, '/api/demo/role');
    expect(callsTo(fetchMock, '/api/session')).toHaveLength(2);
    expect(roleCalls).toHaveLength(2);
    const firstHeaders = new Headers(roleCalls[0]?.[1]?.headers);
    const replayHeaders = new Headers(roleCalls[1]?.[1]?.headers);
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
    const workspaceCalls = callsTo(fetchMock, '/api/demo/workspaces');
    const firstKey = new Headers(workspaceCalls[0]?.[1]?.headers).get(
      'Idempotency-Key',
    );
    const retryKey = new Headers(workspaceCalls[1]?.[1]?.headers).get(
      'Idempotency-Key',
    );
    expect(retryKey).toBe(firstKey);
  });
});
