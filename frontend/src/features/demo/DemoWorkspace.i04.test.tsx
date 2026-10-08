import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DemoSession } from '../../api/session';
import { DemoWorkspace, type RoleOperation } from './DemoWorkspace';

const managerSession: DemoSession = {
  workspace: {
    id: 'workspace-i04',
    name: 'Webhook proof workspace',
    expires_at: '2026-10-08T20:00:00Z',
  },
  identity: {
    user_id: 'manager-user',
    membership_id: 'manager-membership',
    display_name: 'Demo Manager',
    role: 'manager',
  },
  available_roles: ['manager', 'agent'],
  csrf_token: 'manager-csrf-token',
};

const agentSession: DemoSession = {
  ...managerSession,
  identity: {
    user_id: 'agent-user',
    membership_id: 'agent-membership',
    display_name: 'Demo Agent',
    role: 'agent',
  },
  csrf_token: 'agent-csrf-token',
};

const rotatedManagerSession: DemoSession = {
  ...managerSession,
  identity: {
    ...managerSession.identity,
    membership_id: 'rotated-manager-membership',
  },
  csrf_token: 'rotated-manager-csrf-token',
};

const initialDashboard = {
  generated_at: '2026-10-08T14:00:00Z',
  summary: { open: 1, approaching_sla: 1, high_severity: 1, resolved: 0 },
  by_rule: [{ rule_key: 'payment_failed', case_type: 'payment', count: 1 }],
};

const refreshedDashboard = {
  ...initialDashboard,
  generated_at: '2026-10-08T14:05:00Z',
  summary: { ...initialDashboard.summary, open: 2, high_severity: 2 },
  by_rule: [{ rule_key: 'payment_failed', case_type: 'payment', count: 2 }],
};

const seededCase = {
  id: 'case-seeded',
  rule_key: 'payment_failed',
  case_type: 'payment',
  severity: 'high',
  status: 'open',
  due_at: '2026-10-08T15:00:00Z',
  updated_at: '2026-10-08T14:00:00Z',
  version: 1,
  resolution_reason: null,
  resolved_at: null,
  source: { kind: 'seeded_demo' },
  order: {
    id: 'order-seeded',
    order_number: 'DEMO-1001',
    amount_minor: 4999,
    currency: 'USD',
    payment_status: 'failed',
    fulfillment_status: 'unfulfilled',
  },
  assignee: null,
};

const webhookCaseId = '11111111-1111-4111-8111-111111111111';
const providerEventId = 'evt_A1B2C3D4';
const webhookSource = {
  kind: 'synthetic_webhook',
  provider: 'synthetic',
  event_type: 'payment.failed',
  external_event_id: providerEventId,
  received_at: '2026-10-08T14:05:00Z',
  integration_id: 'must-not-render-integration',
  payload_digest: 'must-not-render-digest',
  signature: 'must-not-render-signature',
  headers: 'must-not-render-headers',
  raw_body: 'must-not-render-body',
  webhook_event_id: 'must-not-render-internal-event',
};

const webhookCase = {
  ...seededCase,
  id: webhookCaseId,
  updated_at: '2026-10-08T14:05:00Z',
  version: 2,
  source: webhookSource,
  order: {
    ...seededCase.order,
    id: 'order-webhook',
    order_number: 'DEMO-WEBHOOK-2001',
    amount_minor: 8799,
  },
  assignee: {
    membership_id: 'agent-two',
    display_name: 'Avery Chen',
  },
};

const seededDetail = {
  ...seededCase,
  created_at: '2026-10-08T13:45:00Z',
  resolution_reasons: ['payment_recovered', 'order_cancelled'],
  notes: [],
  audit_events: [],
};

const webhookDetail = {
  ...webhookCase,
  created_at: '2026-10-08T14:05:00Z',
  resolution_reasons: ['customer_contacted', 'payment_recovered'],
  notes: [],
  audit_events: [],
};

const agents = {
  items: [
    { membership_id: 'agent-one', display_name: 'Demo Agent' },
    { membership_id: 'agent-two', display_name: 'Avery Chen' },
  ],
};

const envelope = {
  path: '/api/webhooks/synthetic/22222222-2222-4222-8222-222222222222',
  body: '{"type":"payment.failed","data":{"order_number":"DEMO-WEBHOOK-2001"}}',
  timestamp: '1791468300',
  event_id: providerEventId,
  signature: `v1=${'a'.repeat(64)}`,
};

const deliveryResult = {
  status: 'processed',
  event_id: providerEventId,
  case_id: webhookCaseId,
  replayed: false,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

type RefreshTarget = 'dashboard' | 'cases' | 'detail';

interface FetchOptions {
  failRefreshOnce?: RefreshTarget;
  commandConflict?: boolean;
  deliveryResponse?: Promise<Response>;
  onRefreshRequest?: (target: RefreshTarget) => Promise<Response> | Response;
}

function installFetch(options: FetchOptions = {}) {
  let deliveryCommitted = false;
  let failedRefresh = false;
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = new URL(String(input), window.location.origin);
    const method = init?.method ?? 'GET';
    const refreshTarget: RefreshTarget | null =
      url.pathname === '/api/dashboard'
        ? 'dashboard'
        : url.pathname === '/api/cases'
          ? 'cases'
          : url.pathname === `/api/cases/${webhookCaseId}`
            ? 'detail'
            : null;

    if (deliveryCommitted && method === 'GET' && refreshTarget !== null) {
      if (options.onRefreshRequest) return options.onRefreshRequest(refreshTarget);
      if (options.failRefreshOnce === refreshTarget && !failedRefresh) {
        failedRefresh = true;
        return jsonResponse({ detail: `${refreshTarget} unavailable` }, 503);
      }
      if (refreshTarget === 'dashboard') return jsonResponse(refreshedDashboard);
      if (refreshTarget === 'cases') {
        return jsonResponse({
          items: [webhookCase, seededCase],
          total: 2,
          page: 1,
          page_size: 20,
        });
      }
      return jsonResponse(webhookDetail);
    }

    if (url.pathname === '/api/dashboard') return jsonResponse(initialDashboard);
    if (url.pathname === '/api/cases') {
      return jsonResponse({ items: [seededCase], total: 1, page: 1, page_size: 20 });
    }
    if (url.pathname === '/api/agents') return jsonResponse(agents);
    if (url.pathname === '/api/cases/case-seeded' && method === 'GET') {
      return jsonResponse(seededDetail);
    }
    if (url.pathname === `/api/cases/${webhookCaseId}` && method === 'GET') {
      return jsonResponse(webhookDetail);
    }
    if (url.pathname === '/api/cases/case-seeded/notes' && method === 'POST') {
      if (options.commandConflict) {
        return jsonResponse({ detail: 'Case changed. Refresh and try again.' }, 409);
      }
      return jsonResponse({ case_id: seededCase.id, version: 2 }, 201);
    }
    if (url.pathname === '/api/demo/webhooks/envelope' && method === 'POST') {
      return jsonResponse(envelope);
    }
    if (url.pathname === envelope.path && method === 'POST') {
      deliveryCommitted = true;
      return options.deliveryResponse ?? jsonResponse(deliveryResult, 201);
    }
    return jsonResponse({ detail: `Unhandled ${method} ${url.pathname}` }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function workspace(
  session: DemoSession = managerSession,
  roleOperation: RoleOperation = { kind: 'idle' },
) {
  return (
    <DemoWorkspace
      session={session}
      roleOperation={roleOperation}
      resetOperation={{ kind: 'idle' }}
      onSwitch={vi.fn()}
      onRetrySwitch={vi.fn()}
      onRequestReset={vi.fn()}
      onCancelReset={vi.fn()}
      onConfirmReset={vi.fn()}
      onRetryResetCheck={vi.fn()}
    />
  );
}

function callsTo(fetchMock: ReturnType<typeof installFetch>, pathname: string) {
  return fetchMock.mock.calls.filter(
    ([input]) => new URL(String(input), window.location.origin).pathname === pathname,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('I04 webhook simulator workspace integration', () => {
  it('mounts the disabled simulator only for Managers', async () => {
    installFetch();
    const { rerender } = render(
      workspace(managerSession, { kind: 'switching', role: 'agent' }),
    );

    const panel = await screen.findByRole('region', { name: /synthetic provider/i });
    expect(within(panel).getByRole('button', { name: /deliver new failure/i })).toBeDisabled();
    expect(within(panel).getByRole('button', { name: /send stale signature/i })).toBeDisabled();

    rerender(workspace(agentSession));

    await waitFor(() => {
      expect(
        screen.queryByRole('region', { name: /synthetic provider/i }),
      ).not.toBeInTheDocument();
    });
  });

  it('refreshes dashboard, current queue and returned detail together before opening the case', async () => {
    const user = userEvent.setup();
    const pending = {
      dashboard: deferred<Response>(),
      cases: deferred<Response>(),
      detail: deferred<Response>(),
    };
    const fetchMock = installFetch({
      commandConflict: true,
      onRefreshRequest: (target) => pending[target].promise,
    });
    render(workspace());

    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.selectOptions(
      within(queue).getByRole('combobox', { name: /filter by status/i }),
      'open',
    );
    await user.click(within(queue).getByRole('button', { name: /open demo-1001/i }));
    const note = await screen.findByRole('textbox', { name: /internal note/i });
    await user.type(note, 'Keep this draft until all refresh reads succeed.');
    await user.click(screen.getByRole('button', { name: /add note/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/case changed/i);

    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    await waitFor(() => {
      expect(callsTo(fetchMock, '/api/dashboard')).toHaveLength(2);
      expect(callsTo(fetchMock, '/api/cases')).toHaveLength(3);
      expect(callsTo(fetchMock, `/api/cases/${webhookCaseId}`)).toHaveLength(1);
    });
    const refreshCaseCall = callsTo(fetchMock, '/api/cases').at(-1);
    expect(new URL(String(refreshCaseCall?.[0]), window.location.origin).searchParams.get('status'))
      .toBe('open');
    const envelopeCall = callsTo(fetchMock, '/api/demo/webhooks/envelope')[0];
    expect(new Headers(envelopeCall?.[1]?.headers).get('X-CSRF-Token')).toBe(
      managerSession.csrf_token,
    );

    pending.dashboard.resolve(jsonResponse(refreshedDashboard));
    pending.cases.resolve(jsonResponse({
      items: [webhookCase, seededCase],
      total: 2,
      page: 1,
      page_size: 20,
    }));
    await waitFor(() => expect(screen.getByText('Open cases').parentElement).toHaveTextContent('1'));
    expect(screen.queryByRole('heading', { name: /case demo-webhook-2001/i }))
      .not.toBeInTheDocument();
    expect(note).toHaveValue('Keep this draft until all refresh reads succeed.');

    pending.detail.resolve(jsonResponse(webhookDetail));

    expect(
      await screen.findByRole('heading', { name: /case demo-webhook-2001/i }),
    ).toBeVisible();
    expect(screen.getByText('Open cases').parentElement).toHaveTextContent('2');
    expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveValue('');
    expect(screen.getByRole('combobox', { name: /assign to agent/i })).toHaveValue('agent-two');
    expect(screen.getByRole('combobox', { name: /resolution reason/i }))
      .toHaveValue('customer_contacted');
    expect(screen.queryByText(/case changed/i)).not.toBeInTheDocument();
  });

  it('uses the latest queue filters when delivery completes after a filter change', async () => {
    const user = userEvent.setup();
    const pendingDelivery = deferred<Response>();
    const fetchMock = installFetch({ deliveryResponse: pendingDelivery.promise });
    render(workspace());

    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    await waitFor(() => expect(callsTo(fetchMock, envelope.path)).toHaveLength(1));

    await user.selectOptions(
      within(queue).getByRole('combobox', { name: /filter by status/i }),
      'resolved',
    );
    await waitFor(() => expect(callsTo(fetchMock, '/api/cases')).toHaveLength(2));

    pendingDelivery.resolve(jsonResponse(deliveryResult, 201));

    expect(
      await screen.findByRole('heading', { name: /case demo-webhook-2001/i }),
    ).toBeVisible();
    const committedRefresh = callsTo(fetchMock, '/api/cases').at(-1);
    expect(
      new URL(String(committedRefresh?.[0]), window.location.origin).searchParams.get(
        'status',
      ),
    ).toBe('resolved');
  });

  it.each<RefreshTarget>(['dashboard', 'cases', 'detail'])(
    'keeps the prior workspace intact when the committed %s refresh fails',
    async (failedTarget) => {
      const user = userEvent.setup();
      const fetchMock = installFetch({ failRefreshOnce: failedTarget });
      render(workspace());

      const queue = await screen.findByRole('region', { name: /exception queue/i });
      await user.click(within(queue).getByRole('button', { name: /open demo-1001/i }));
      const note = await screen.findByRole('textbox', { name: /internal note/i });
      await user.type(note, `Draft preserved for ${failedTarget}.`);
      await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(/event was committed.*workspace did not refresh/i);
      expect(screen.getByRole('heading', { name: /case demo-1001/i })).toBeVisible();
      expect(screen.getByText('Open cases').parentElement).toHaveTextContent('1');
      expect(within(queue).queryByText('DEMO-WEBHOOK-2001')).not.toBeInTheDocument();
      expect(note).toHaveValue(`Draft preserved for ${failedTarget}.`);

      const signerPosts = callsTo(fetchMock, '/api/demo/webhooks/envelope').length;
      const deliveryPosts = callsTo(fetchMock, envelope.path).length;
      await user.click(within(alert).getByRole('button', { name: /refresh workspace/i }));

      expect(
        await screen.findByRole('heading', { name: /case demo-webhook-2001/i }),
      ).toBeVisible();
      expect(callsTo(fetchMock, '/api/demo/webhooks/envelope')).toHaveLength(signerPosts);
      expect(callsTo(fetchMock, envelope.path)).toHaveLength(deliveryPosts);
      expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveValue('');
    },
  );

  it('shows source badges and renders only whitelisted webhook provenance', async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch();
    render(workspace());

    await screen.findByRole('region', { name: /exception queue/i });
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    const queue = screen.getByRole('region', { name: /exception queue/i });
    expect(await within(queue).findByText('Synthetic webhook')).toBeVisible();
    expect(within(queue).getByText('Seeded demo data')).toBeVisible();

    await user.click(within(queue).getByRole('button', { name: /open demo-1001/i }));
    const seededProvenance = await screen.findByRole('region', { name: /event provenance/i });
    expect(within(seededProvenance).getByText('Seeded demo data')).toBeVisible();

    await user.click(
      within(queue).getByRole('button', { name: /open demo-webhook-2001/i }),
    );
    const provenance = await screen.findByRole('region', { name: /event provenance/i });
    expect(within(provenance).getByText('Synthetic')).toBeVisible();
    expect(within(provenance).getByText('payment.failed')).toBeVisible();
    expect(within(provenance).getByText(providerEventId)).toBeVisible();
    expect(within(provenance).getByText(/Oct 8, 2026.*UTC/i)).toBeVisible();

    for (const forbidden of [
      webhookSource.integration_id,
      webhookSource.payload_digest,
      webhookSource.signature,
      webhookSource.headers,
      webhookSource.raw_body,
      webhookSource.webhook_event_id,
      envelope.body,
      envelope.signature,
    ]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
    expect(callsTo(fetchMock, envelope.path)).toHaveLength(1);
  });

  it('remounts the in-memory simulator when the membership key changes', async () => {
    const user = userEvent.setup();
    installFetch();
    const { rerender } = render(workspace());

    await screen.findByRole('region', { name: /synthetic provider/i });
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    expect(await screen.findByRole('button', { name: /replay same event/i })).toBeEnabled();

    rerender(workspace(rotatedManagerSession));

    expect(await screen.findByRole('button', { name: /replay same event/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /tamper after signing/i })).toBeDisabled();
  });
});
