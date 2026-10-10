import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DemoSession } from '../../api/session';
import { DemoWorkspace } from './DemoWorkspace';

const fictionalTextRule =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';

const managerSession: DemoSession = {
  workspace: {
    id: 'workspace-835c',
    name: 'Demo workspace 835C',
    expires_at: '2026-10-07T20:00:00Z',
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

const dashboard = {
  generated_at: '2026-10-07T16:00:00Z',
  summary: {
    open: 3,
    approaching_sla: 2,
    high_severity: 2,
    resolved: 1,
  },
  by_rule: [
    { rule_key: 'payment_failed', case_type: 'payment', count: 2 },
    { rule_key: 'refund_review', case_type: 'refund', count: 1 },
  ],
};

const caseSummary = {
  id: 'case-payment-1042',
  rule_key: 'payment_failed',
  case_type: 'payment',
  severity: 'high',
  status: 'assigned',
  due_at: '2026-10-07T17:15:00Z',
  updated_at: '2026-10-07T16:10:00Z',
  version: 3,
  resolution_reason: null,
  resolved_at: null,
  source: { kind: 'seeded_demo' as const },
  order: {
    id: 'order-1042',
    order_number: 'DEMO-1042',
    amount_minor: 12999,
    currency: 'USD',
    payment_status: 'failed',
    fulfillment_status: 'unfulfilled',
  },
  assignee: {
    membership_id: 'agent-membership',
    display_name: 'Demo Agent',
  },
};

const caseDetail = {
  ...caseSummary,
  created_at: '2026-10-07T15:45:00Z',
  resolution_reasons: [
    'payment_recovered',
    'customer_contacted',
    'order_cancelled',
  ],
  notes: [
    {
      id: 'note-1',
      body: 'Fictional demo customer requested a retry after 17:00 UTC.',
      author: {
        membership_id: 'agent-membership',
        display_name: 'Demo Agent',
      },
      created_at: '2026-10-07T16:05:00Z',
    },
  ],
  audit_events: [
    {
      id: 'audit-1',
      action: 'case.assigned',
      object_type: 'exception_case',
      object_id: 'case-payment-1042',
      actor: {
        membership_id: 'manager-membership',
        display_name: 'Demo Manager',
      },
      changes: { assignee_id: 'agent-membership' },
      created_at: '2026-10-07T16:00:00Z',
    },
  ],
};

const agents = {
  items: [
    { membership_id: 'agent-membership', display_name: 'Demo Agent' },
    { membership_id: 'agent-two', display_name: 'Avery Chen' },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
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

type RouteHandler = (
  url: URL,
  init?: RequestInit,
) => Response | undefined | Promise<Response | undefined>;

function installOperationsFetch(handler?: RouteHandler) {
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = new URL(String(input), window.location.origin);
    if (handler) {
      const handled = await handler(url, init);
      if (handled) return handled;
    }
    if (url.pathname === '/api/dashboard') return jsonResponse(dashboard);
    if (url.pathname === '/api/cases/case-payment-1042') return jsonResponse(caseDetail);
    if (url.pathname === '/api/cases') {
      return jsonResponse({ items: [caseSummary], total: 1, page: 1, page_size: 20 });
    }
    if (url.pathname === '/api/agents') return jsonResponse(agents);
    return jsonResponse({ detail: 'Not Found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderWorkspace(session: DemoSession = managerSession) {
  return render(
    <DemoWorkspace
      session={session}
      roleOperation={{ kind: 'idle' }}
      resetOperation={{ kind: 'idle' }}
      onSwitch={vi.fn()}
      onRetrySwitch={vi.fn()}
      onRequestReset={vi.fn()}
      onCancelReset={vi.fn()}
      onConfirmReset={vi.fn()}
      onRetryResetCheck={vi.fn()}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('I03 exception operations workspace', () => {
  it.each([
    ['Manager', managerSession],
    ['Agent', agentSession],
  ])('keeps the shared five-step guide visible in the %s workspace', async (_role, session) => {
    installOperationsFetch();

    renderWorkspace(session);

    const guide = screen.getByRole('complementary', {
      name: /five-step evaluator guide/i,
    });
    expect(within(guide).getAllByRole('listitem')).toHaveLength(5);
    expect(within(guide).getByText('Create an event')).toBeVisible();
    expect(guide).toHaveTextContent(/enter as Manager/i);
    expect(within(guide).getByText('Verify the trail')).toBeVisible();
    expect(await screen.findByRole('region', { name: /operations overview/i })).toBeVisible();
  });

  it('shows the live summary and queue, then opens the order and timeline for a case', async () => {
    const user = userEvent.setup();
    const fetchMock = installOperationsFetch();

    renderWorkspace();

    expect(screen.getByRole('status')).toHaveTextContent(/loading operations/i);
    const overview = await screen.findByRole('region', { name: /operations overview/i });
    expect(within(overview).getByText('3')).toBeVisible();
    expect(within(overview).getByText('Open cases')).toBeVisible();
    expect(within(overview).getByText('Approaching SLA')).toBeVisible();

    const queue = screen.getByRole('region', { name: /exception queue/i });
    expect(within(queue).getByText('DEMO-1042')).toBeVisible();
    expect(within(queue).getByText(/payment failed/i)).toBeVisible();
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));

    expect(await screen.findByRole('heading', { name: /case demo-1042/i })).toBeVisible();
    expect(screen.getByText('$129.99')).toBeVisible();
    expect(screen.getByText(/fictional demo customer requested a retry/i)).toBeVisible();
    expect(screen.getByText(/assigned by demo manager/i)).toBeVisible();
    const dashboardCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes('/api/dashboard'),
    );
    expect(dashboardCall?.[1]).toMatchObject({
      cache: 'no-store',
      credentials: 'same-origin',
      method: 'GET',
    });
  });

  it('lets a Manager assign a case with the current version and command protections', async () => {
    const user = userEvent.setup();
    let detailReads = 0;
    const assignedDetail = {
      ...caseDetail,
      version: 4,
      assignee: { membership_id: 'agent-two', display_name: 'Avery Chen' },
    };
    const fetchMock = installOperationsFetch((url, init) => {
      if (url.pathname === '/api/cases/case-payment-1042') {
        detailReads += 1;
        return jsonResponse(detailReads === 1 ? caseDetail : assignedDetail);
      }
      if (url.pathname === '/api/cases/case-payment-1042/assignment') {
        return jsonResponse({ case_id: caseDetail.id, version: 4 });
      }
      return undefined;
    });

    renderWorkspace();
    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));
    await user.selectOptions(
      await screen.findByRole('combobox', { name: /assign to agent/i }),
      'agent-two',
    );
    await user.click(screen.getByRole('button', { name: /update assignment/i }));

    const detailRegion = screen.getByRole('region', { name: /case detail/i });
    const order = within(detailRegion).getByRole('region', { name: 'Order' });
    expect(await within(order).findByText('Avery Chen')).toBeVisible();
    expect(detailReads).toBe(2);
    const assignmentCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes('/api/cases/case-payment-1042/assignment'),
    );
    expect(assignmentCall?.[1]).toMatchObject({
      credentials: 'same-origin',
      method: 'POST',
      body: JSON.stringify({ assignee_id: 'agent-two', version: 3 }),
    });
    const headers = new Headers(assignmentCall?.[1]?.headers);
    expect(headers.get('X-CSRF-Token')).toBe(managerSession.csrf_token);
    expect(headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('lets an Agent add a note to an owned case without exposing Manager assignment', async () => {
    const user = userEvent.setup();
    let detailReads = 0;
    const noteBody = 'Fictional demo customer confirmed a 17:00 UTC retry window.';
    const noteWrite = deferred<Response>();
    const notedDetail = {
      ...caseDetail,
      version: 4,
      notes: [
        ...caseDetail.notes,
        {
          id: 'note-2',
          body: noteBody,
          author: {
            membership_id: 'agent-membership',
            display_name: 'Demo Agent',
          },
          created_at: '2026-10-07T16:20:00Z',
        },
      ],
    };
    const fetchMock = installOperationsFetch((url) => {
      if (url.pathname === '/api/cases/case-payment-1042') {
        detailReads += 1;
        return jsonResponse(detailReads === 1 ? caseDetail : notedDetail);
      }
      if (url.pathname === '/api/cases/case-payment-1042/notes') {
        return noteWrite.promise;
      }
      return undefined;
    });

    renderWorkspace(agentSession);
    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));
    expect(
      await screen.findByRole('heading', { name: /case demo-1042/i }),
    ).toBeVisible();
    expect(screen.queryByRole('combobox', { name: /assign to agent/i })).not.toBeInTheDocument();

    const note = screen.getByRole('textbox', { name: /internal note/i });
    const warning = screen.getByText(fictionalTextRule);
    expect(warning).toBeVisible();
    expect(warning).not.toHaveAttribute('role', 'alert');
    expect(note).toHaveAccessibleDescription(fictionalTextRule);

    await user.type(note, noteBody);
    await user.click(screen.getByRole('button', { name: /add note/i }));

    expect(await screen.findByRole('button', { name: /adding/i })).toBeDisabled();
    expect(note).toBeDisabled();
    expect(note).toHaveAccessibleDescription(fictionalTextRule);
    noteWrite.resolve(jsonResponse({ case_id: caseDetail.id, version: 4 }, 201));

    const timeline = screen.getByRole('region', { name: /accountable timeline/i });
    expect(await within(timeline).findByText(noteBody)).toBeVisible();
    expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveValue('');
    expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveAccessibleDescription(
      fictionalTextRule,
    );
    expect(detailReads).toBe(2);
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes('/api/agents')),
    ).toBe(false);
    const noteCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes('/api/cases/case-payment-1042/notes'),
    );
    expect(JSON.parse(String(noteCall?.[1]?.body))).toEqual({ body: noteBody, version: 3 });
    expect(new Headers(noteCall?.[1]?.headers).get('X-CSRF-Token')).toBe(
      agentSession.csrf_token,
    );
  });

  it('lets an Agent resolve an owned case using the server-provided reason list', async () => {
    const user = userEvent.setup();
    let dashboardReads = 0;
    let detailReads = 0;
    const resolvedDetail = {
      ...caseDetail,
      status: 'resolved',
      version: 4,
      resolution_reason: 'payment_recovered',
      resolved_at: '2026-10-07T16:25:00Z',
    };
    const fetchMock = installOperationsFetch((url) => {
      if (url.pathname === '/api/dashboard') {
        dashboardReads += 1;
        return jsonResponse(
          dashboardReads === 1
            ? dashboard
            : {
                ...dashboard,
                summary: { ...dashboard.summary, open: 2, resolved: 2 },
              },
        );
      }
      if (url.pathname === '/api/cases/case-payment-1042') {
        detailReads += 1;
        return jsonResponse(detailReads === 1 ? caseDetail : resolvedDetail);
      }
      if (url.pathname === '/api/cases/case-payment-1042/resolution') {
        return jsonResponse({ case_id: caseDetail.id, version: 4 });
      }
      return undefined;
    });

    renderWorkspace(agentSession);
    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));
    await user.selectOptions(
      await screen.findByRole('combobox', { name: /resolution reason/i }),
      'payment_recovered',
    );
    await user.click(screen.getByRole('button', { name: /resolve case/i }));

    const detailArticle = screen.getByRole('article', { name: /case demo-1042/i });
    expect(await within(detailArticle).findByText('Resolved')).toBeVisible();
    expect(detailReads).toBe(2);
    const resolvedMetric = screen.getByText('Resolved', { selector: 'dt' }).parentElement;
    await waitFor(() => expect(resolvedMetric).toHaveTextContent('2'));
    expect(dashboardReads).toBe(2);
    const resolutionCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes('/api/cases/case-payment-1042/resolution'),
    );
    expect(JSON.parse(String(resolutionCall?.[1]?.body))).toEqual({
      reason: 'payment_recovered',
      version: 3,
    });
  });

  it('recovers a committed note by refreshing detail without replaying the POST', async () => {
    const user = userEvent.setup();
    const noteBody = 'A fictional bank trace was attached before detail refresh failed.';
    const recoveryRead = deferred<Response>();
    const notedDetail = {
      ...caseDetail,
      version: 4,
      notes: [
        ...caseDetail.notes,
        {
          id: 'note-committed',
          body: noteBody,
          author: {
            membership_id: 'agent-membership',
            display_name: 'Demo Agent',
          },
          created_at: '2026-10-07T16:22:00Z',
        },
      ],
    };
    let detailReads = 0;
    const fetchMock = installOperationsFetch((url) => {
      if (url.pathname === '/api/cases/case-payment-1042') {
        detailReads += 1;
        if (detailReads === 1) return jsonResponse(caseDetail);
        if (detailReads === 2) {
          return jsonResponse({ detail: 'Detail service is temporarily unavailable.' }, 503);
        }
        return recoveryRead.promise;
      }
      if (url.pathname === '/api/cases/case-payment-1042/notes') {
        return jsonResponse({ case_id: caseDetail.id, version: 4 }, 201);
      }
      return undefined;
    });

    renderWorkspace(agentSession);
    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));
    const note = await screen.findByRole('textbox', { name: /internal note/i });
    await user.type(note, noteBody);
    await user.click(screen.getByRole('button', { name: /add note/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/change was saved/i);
    expect(alert).toHaveTextContent(/latest case/i);
    expect(note).toHaveValue(noteBody);
    expect(note).toHaveAccessibleDescription(fictionalTextRule);
    expect(screen.getByRole('button', { name: /add note/i })).toBeDisabled();
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes('/api/cases/case-payment-1042/notes'),
      ),
    ).toHaveLength(1);

    await user.click(within(alert).getByRole('button', { name: /refresh case/i }));

    expect(await screen.findByRole('status')).toHaveTextContent(/loading the saved change/i);
    expect(note).toHaveAccessibleDescription(fictionalTextRule);
    recoveryRead.resolve(jsonResponse(notedDetail));

    const timeline = screen.getByRole('region', { name: /accountable timeline/i });
    expect(await within(timeline).findByText(noteBody)).toBeVisible();
    expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveValue('');
    expect(detailReads).toBe(3);
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes('/api/cases/case-payment-1042/notes'),
      ),
    ).toHaveLength(1);
  });

  it('preserves an unsubmitted note after a version conflict and refreshes the case', async () => {
    const user = userEvent.setup();
    const noteBody = 'Waiting on the fictional customer before another demo attempt.';
    let detailReads = 0;
    const refreshedDetail = { ...caseDetail, version: 4 };
    installOperationsFetch((url) => {
      if (url.pathname === '/api/cases/case-payment-1042') {
        detailReads += 1;
        return jsonResponse(detailReads === 1 ? caseDetail : refreshedDetail);
      }
      if (url.pathname === '/api/cases/case-payment-1042/notes') {
        return jsonResponse({ detail: 'Case changed. Refresh and try again.' }, 409);
      }
      return undefined;
    });

    renderWorkspace(agentSession);
    const queue = await screen.findByRole('region', { name: /exception queue/i });
    await user.click(within(queue).getByRole('button', { name: /open demo-1042/i }));
    const note = await screen.findByRole('textbox', { name: /internal note/i });
    await user.type(note, noteBody);
    await user.click(screen.getByRole('button', { name: /add note/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/case changed/i);
    expect(note).toHaveValue(noteBody);
    expect(note).toHaveAccessibleDescription(fictionalTextRule);
    await user.click(screen.getByRole('button', { name: /refresh case/i }));

    await waitFor(() => expect(detailReads).toBe(2));
    expect(screen.getByRole('textbox', { name: /internal note/i })).toHaveValue(noteBody);
  });

  it('distinguishes an empty workspace from filters with no matches', async () => {
    const user = userEvent.setup();
    const fetchMock = installOperationsFetch((url) => {
      if (url.pathname === '/api/cases' && url.searchParams.get('status') === 'resolved') {
        return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 });
      }
      return undefined;
    });

    renderWorkspace();
    await screen.findByRole('region', { name: /exception queue/i });
    await user.selectOptions(
      screen.getByRole('combobox', { name: /filter by status/i }),
      'resolved',
    );

    expect(await screen.findByRole('heading', { name: /no cases match/i })).toBeVisible();
    expect(screen.getByRole('button', { name: /clear filters/i })).toBeVisible();
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes('status=resolved')),
    ).toBe(true);
  });

  it('shows the unfiltered empty state when the workspace has no cases', async () => {
    installOperationsFetch((url) => {
      if (url.pathname === '/api/cases') {
        return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 });
      }
      return undefined;
    });

    renderWorkspace(agentSession);

    expect(await screen.findByRole('heading', { name: /no exceptions yet/i })).toBeVisible();
    expect(screen.queryByRole('button', { name: /clear filters/i })).not.toBeInTheDocument();
  });

  it('offers a retry after a temporary operations error', async () => {
    const user = userEvent.setup();
    let dashboardReads = 0;
    installOperationsFetch((url) => {
      if (url.pathname === '/api/dashboard') {
        dashboardReads += 1;
        return dashboardReads === 1
          ? jsonResponse({ detail: 'Operations are warming up.' }, 503)
          : jsonResponse(dashboard);
      }
      return undefined;
    });

    renderWorkspace(agentSession);

    expect(await screen.findByRole('alert')).toHaveTextContent(/operations are warming up/i);
    await user.click(screen.getByRole('button', { name: /try again/i }));
    expect(await screen.findByRole('region', { name: /operations overview/i })).toBeVisible();
  });

  it('renders an explicit forbidden state when the active role loses access', async () => {
    installOperationsFetch((url) => {
      if (url.pathname === '/api/dashboard') {
        return jsonResponse({ detail: 'Manager access required' }, 403);
      }
      return undefined;
    });

    renderWorkspace();

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('heading', { name: /access changed/i })).toBeVisible();
    expect(within(alert).getByText(/switch roles or reload access/i)).toBeVisible();
    expect(within(alert).getByRole('button', { name: /reload access/i })).toBeVisible();
  });
});

export {
  agentSession,
  agents,
  caseDetail,
  caseSummary,
  dashboard,
  installOperationsFetch,
  managerSession,
  renderWorkspace,
};
