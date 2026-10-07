import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

const session = {
  workspace: {
    id: 'workspace-i03',
    name: 'Demo workspace I03',
    expires_at: '2026-10-08T00:00:00Z',
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

const resetSession = {
  ...session,
  workspace: {
    ...session.workspace,
    id: 'workspace-i03-reset',
    name: 'Demo workspace RESET',
  },
  csrf_token: 'reset-manager-csrf-token',
};

const dashboard = {
  generated_at: '2026-10-07T16:00:00Z',
  summary: { open: 3, approaching_sla: 2, high_severity: 2, resolved: 1 },
  by_rule: [{ rule_key: 'payment_failed', case_type: 'payment', count: 2 }],
};

const summary = {
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
  order: {
    id: 'order-1042',
    order_number: 'DEMO-1042',
    amount_minor: 12999,
    currency: 'USD',
    payment_status: 'failed',
    fulfillment_status: 'unfulfilled',
  },
  assignee: { membership_id: 'agent-membership', display_name: 'Demo Agent' },
};

const detail = {
  ...summary,
  created_at: '2026-10-07T15:45:00Z',
  resolution_reasons: ['payment_recovered', 'customer_contacted', 'order_cancelled'],
  notes: [
    {
      id: 'note-1',
      body: 'Customer asked us to retry the card after 17:00 UTC.',
      author: { membership_id: 'agent-membership', display_name: 'Demo Agent' },
      created_at: '2026-10-07T16:05:00Z',
    },
  ],
  audit_events: [
    {
      id: 'audit-1',
      action: 'case.assigned',
      object_type: 'exception_case',
      object_id: 'case-payment-1042',
      actor: { membership_id: 'manager-membership', display_name: 'Demo Manager' },
      changes: { assignee_id: 'agent-membership' },
      created_at: '2026-10-07T16:00:00Z',
    },
  ],
};

async function mockWorkspaceApi(page: Page) {
  let activeSession = session;
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    let body: unknown;
    let status = 200;
    if (url.pathname === '/api/session') body = activeSession;
    else if (
      url.pathname === '/api/demo/reset' &&
      route.request().method() === 'POST'
    ) {
      activeSession = resetSession;
      body = resetSession;
      status = 201;
    }
    else if (url.pathname === '/api/dashboard') body = dashboard;
    else if (url.pathname === '/api/cases') {
      body = { items: [summary], total: 1, page: 1, page_size: 20 };
    } else if (url.pathname === '/api/cases/case-payment-1042') body = detail;
    else if (url.pathname === '/api/agents') {
      body = {
        items: [
          { membership_id: 'agent-membership', display_name: 'Demo Agent' },
          { membership_id: 'agent-two', display_name: 'Avery Chen' },
        ],
      };
    } else {
      await route.fulfill({ status: 404, json: { detail: 'Not Found' } });
      return;
    }
    await route.fulfill({ status, json: body });
  });
}

test.describe('I03 operations layout', () => {
  test('uses a queue and detail split on a desktop workspace', async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop-chromium',
      'The split-panel geometry is a desktop-only contract.',
    );
    await mockWorkspaceApi(page);
    await page.goto('/');

    const queue = page.getByRole('region', { name: 'Exception queue' });
    const detailPanel = page.getByRole('region', { name: 'Case detail' });
    await expect(queue).toBeVisible();
    await expect(detailPanel).toBeVisible();

    const queueBox = await queue.boundingBox();
    const detailBox = await detailPanel.boundingBox();
    expect(queueBox).not.toBeNull();
    expect(detailBox).not.toBeNull();
    expect(Math.abs((queueBox?.y ?? 0) - (detailBox?.y ?? 100))).toBeLessThan(8);
    expect(queueBox?.x ?? 1000).toBeLessThan(detailBox?.x ?? 0);
  });

  test('confirms a reset and replaces the active mocked workspace', async ({ page }) => {
    await mockWorkspaceApi(page);
    await page.goto('/');

    await page.getByRole('button', { name: 'Reset demo data' }).click();
    await expect(page.getByRole('heading', { name: 'Reset this workspace?' })).toBeVisible();
    const resetRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return url.pathname === '/api/demo/reset' && request.method() === 'POST';
    });
    await page.getByRole('button', { name: 'Reset workspace', exact: true }).click();

    const request = await resetRequest;
    expect(request.headers()['x-csrf-token']).toBe(session.csrf_token);
    expect(request.headers()['idempotency-key']).toBeUndefined();
    await expect(page.getByText('Demo workspace RESET')).toBeVisible();
    await expect(page.getByText('Demo workspace I03')).not.toBeVisible();
  });

  for (const width of [320, 390]) {
    test(`keeps the complete case workflow inside a ${width}px viewport`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1100 });
      await mockWorkspaceApi(page);
      await page.goto('/');
      await page.getByRole('button', { name: 'Open DEMO-1042' }).click();
      await expect(page.getByRole('heading', { name: 'Case DEMO-1042' })).toBeVisible();
      await page.getByRole('button', { name: 'Reset demo data' }).click();
      await expect(page.getByRole('heading', { name: 'Reset this workspace?' })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Cancel' })).toBeVisible();
      await expect(
        page.getByRole('button', { name: 'Reset workspace', exact: true }),
      ).toBeVisible();

      const overflow = await page.evaluate(() => {
        const viewportWidth = document.documentElement.clientWidth;
        const offenders = Array.from(document.querySelectorAll<HTMLElement>('main *'))
          .filter((element) => {
            const style = getComputedStyle(element);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const box = element.getBoundingClientRect();
            return box.width > 0 && (box.left < -0.5 || box.right > viewportWidth + 0.5);
          })
          .map((element) => `${element.tagName.toLowerCase()}.${element.className}`);
        return {
          clientWidth: viewportWidth,
          scrollWidth: document.documentElement.scrollWidth,
          offenders,
        };
      });

      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
      expect(overflow.offenders).toEqual([]);
    });
  }
});
