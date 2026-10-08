import type { Page, Response } from '@playwright/test';

import { expect, test } from './fixtures';

const WEBHOOK_TARGET =
  /^\/api\/webhooks\/synthetic\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const CASE_DETAIL_PATH =
  /^\/api\/cases\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

interface Dashboard {
  summary: {
    open: number;
    approaching_sla: number;
    high_severity: number;
    resolved: number;
  };
  by_rule: Array<{ rule_key: string; case_type: string; count: number }>;
}

interface SyntheticWebhookCaseSource {
  kind: 'synthetic_webhook';
  provider: 'synthetic';
  event_type: 'payment.failed';
  external_event_id: string;
  received_at: string;
}

type CaseSource = { kind: 'seeded_demo' } | SyntheticWebhookCaseSource;

interface CaseSummary {
  id: string;
  status: string;
  severity: string;
  version: number;
  source: CaseSource;
  order: {
    order_number: string;
    payment_status: string;
  };
}

interface CasePage {
  items: CaseSummary[];
  total: number;
}

interface CaseDetail extends CaseSummary {
  resolution_reason: string | null;
  notes: Array<{ body: string; author: { display_name: string } }>;
  audit_events: Array<{ action: string; actor: { display_name: string } | null }>;
}

interface WebhookDelivery {
  status: 'processed';
  event_id: string;
  case_id: string;
  replayed: boolean;
}

interface CaseSnapshot {
  dashboard: Dashboard;
  queue: CasePage;
  detail: CaseDetail;
}

function isResponse(
  response: Response,
  method: string,
  pathname: string | RegExp,
  status: number,
): boolean {
  const path = new URL(response.url()).pathname;
  return (
    response.request().method() === method &&
    (typeof pathname === 'string' ? path === pathname : pathname.test(path)) &&
    response.status() === status
  );
}

async function getJson<T>(page: Page, path: string): Promise<T> {
  const response = await page.context().request.get(new URL(path, page.url()).toString(), {
    headers: { Accept: 'application/json' },
  });
  expect(response.status()).toBe(200);
  return await response.json() as T;
}

async function readCaseSnapshot(page: Page, caseId: string): Promise<CaseSnapshot> {
  const [dashboard, queue, detail] = await Promise.all([
    getJson<Dashboard>(page, '/api/dashboard'),
    getJson<CasePage>(page, '/api/cases?page=1&page_size=20&sort=due_at'),
    getJson<CaseDetail>(page, `/api/cases/${encodeURIComponent(caseId)}`),
  ]);
  return { dashboard, queue, detail };
}

function expectNoBusinessChange(actual: CaseSnapshot, expected: CaseSnapshot): void {
  expect(actual.dashboard.summary).toEqual(expected.dashboard.summary);
  expect(actual.dashboard.by_rule).toEqual(expected.dashboard.by_rule);
  expect(actual.queue.total).toBe(expected.queue.total);
  expect(actual.queue.items.map((item) => item.id).sort()).toEqual(
    expected.queue.items.map((item) => item.id).sort(),
  );
  expect(actual.detail.version).toBe(expected.detail.version);
  expect(actual.detail.status).toBe(expected.detail.status);
  expect(actual.detail.audit_events.map((event) => event.action)).toEqual(
    expected.detail.audit_events.map((event) => event.action),
  );
}

function operationalRefresh(page: Page, casePath: string | RegExp) {
  return {
    dashboard: page.waitForResponse((response) =>
      isResponse(response, 'GET', '/api/dashboard', 200),
    ),
    queue: page.waitForResponse((response) =>
      isResponse(response, 'GET', '/api/cases', 200),
    ),
    detail: page.waitForResponse((response) =>
      isResponse(response, 'GET', casePath, 200),
    ),
  };
}

async function waitForOperationalRefresh(
  pending: ReturnType<typeof operationalRefresh>,
) {
  const [dashboard, queue, detail] = await Promise.all([
    pending.dashboard,
    pending.queue,
    pending.detail,
  ]);
  return {
    dashboard: await dashboard.json() as Dashboard,
    queue: await queue.json() as CasePage,
    detail: await detail.json() as CaseDetail,
  };
}

async function expectAuthenticationRejection(response: Response): Promise<void> {
  expect(response.status()).toBe(401);
  expect(await response.json()).toEqual({
    detail: {
      code: 'webhook_authentication_failed',
      message: 'Webhook authentication failed.',
    },
  });
}

async function expectSummaryCard(page: Page, label: string, value: number): Promise<void> {
  const overview = page.getByRole('region', { name: 'Operations overview' });
  const card = overview.locator('.summary-card').filter({ hasText: label });
  await expect(card.locator('dd')).toHaveText(String(value));
}

async function verifyDesktop320Layout(
  page: Page,
  eventId: string,
): Promise<void> {
  const originalViewport = page.viewportSize();
  expect(originalViewport).not.toBeNull();

  try {
    await page.setViewportSize({ width: 320, height: 1100 });
    const panel = page.getByRole('region', { name: 'Synthetic provider' });
    const buttons = [
      panel.getByRole('button', { name: 'Deliver new failure' }),
      panel.getByRole('button', { name: 'Replay same event' }),
      panel.getByRole('button', { name: 'Tamper after signing' }),
      panel.getByRole('button', { name: 'Send stale signature' }),
    ];
    const boxes = [];
    for (const button of buttons) {
      await expect(button).toBeVisible();
      const box = await button.boundingBox();
      expect(box).not.toBeNull();
      expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
      if (box) boxes.push(box);
    }
    expect(boxes).toHaveLength(4);
    for (let index = 1; index < boxes.length; index += 1) {
      const gap = boxes[index].y - (boxes[index - 1].y + boxes[index - 1].height);
      expect(gap).toBeGreaterThanOrEqual(8);
    }

    const displayedEventIds = [
      panel.locator('.synthetic-result-meta dd').filter({ hasText: eventId }),
      page.locator('.case-source-id').filter({ hasText: eventId }),
    ];
    for (const displayedEventId of displayedEventIds) {
      await expect(displayedEventId).toHaveText(eventId);
      const eventLayout = await displayedEventId.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return {
          left: box.left,
          right: box.right,
          overflowWrap: getComputedStyle(element).overflowWrap,
          viewportWidth: document.documentElement.clientWidth,
        };
      });
      expect(eventLayout.overflowWrap).toBe('anywhere');
      expect(eventLayout.left).toBeGreaterThanOrEqual(-0.5);
      expect(eventLayout.right).toBeLessThanOrEqual(eventLayout.viewportWidth + 0.5);
    }

    const overflow = await page.evaluate(() => {
      const viewportWidth = document.documentElement.clientWidth;
      const offenders = Array.from(document.querySelectorAll<HTMLElement>('body *'))
        .filter((element) => {
          const style = getComputedStyle(element);
          if (style.display === 'none' || style.visibility === 'hidden') return false;
          const box = element.getBoundingClientRect();
          return box.width > 0 && (box.left < -0.5 || box.right > viewportWidth + 0.5);
        })
        .map((element) => `${element.tagName.toLowerCase()}.${String(element.className)}`);
      return {
        clientWidth: viewportWidth,
        scrollWidth: document.documentElement.scrollWidth,
        offenders,
      };
    });
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
    expect(overflow.offenders).toEqual([]);
  } finally {
    if (originalViewport) await page.setViewportSize(originalViewport);
  }

  expect(page.viewportSize()).toEqual(originalViewport);
}

test.use({ trace: 'off' });
test.setTimeout(120_000);

test.describe('I04 signed webhook evidence', () => {
  test('proves signed delivery, rejection, provenance, and the complete role workflow', async ({
    browserDiagnostics,
    page,
  }, testInfo) => {
    await page.goto('/');
    const createWorkspace = page.waitForResponse((response) =>
      isResponse(response, 'POST', '/api/demo/workspaces', 201),
    );
    await page.getByRole('button', { name: 'Enter as Manager' }).click();
    await createWorkspace;
    await expect(page.getByRole('heading', { name: 'Manager workspace' })).toBeVisible();

    const queue = page.getByRole('region', { name: 'Exception queue' });
    const panel = page.getByRole('region', { name: 'Synthetic provider' });
    await expect(queue.getByText('4 total')).toBeVisible();
    await expect(panel).toBeVisible();

    const baselineDashboard = await getJson<Dashboard>(page, '/api/dashboard');
    const baselineQueue = await getJson<CasePage>(
      page,
      '/api/cases?page=1&page_size=20&sort=due_at',
    );
    expect(baselineQueue.total).toBe(4);

    const freshDeliveryPending = page.waitForResponse((response) =>
      isResponse(response, 'POST', WEBHOOK_TARGET, 201),
    );
    const freshRefreshPending = operationalRefresh(page, CASE_DETAIL_PATH);
    await panel.getByRole('button', { name: 'Deliver new failure' }).click();

    const freshResponse = await freshDeliveryPending;
    const webhookTarget = freshResponse.url();
    const freshResult = await freshResponse.json() as WebhookDelivery;
    expect(freshResult).toEqual({
      status: 'processed',
      event_id: expect.any(String),
      case_id: expect.any(String),
      replayed: false,
    });
    const freshRefresh = await waitForOperationalRefresh(freshRefreshPending);
    expect(freshRefresh.detail.id).toBe(freshResult.case_id);
    expect(freshRefresh.detail.source).toEqual({
      kind: 'synthetic_webhook',
      provider: 'synthetic',
      event_type: 'payment.failed',
      external_event_id: freshResult.event_id,
      received_at: expect.any(String),
    });
    expect(freshRefresh.detail.status).toBe('open');
    expect(freshRefresh.detail.severity).toBe('high');
    expect(freshRefresh.detail.version).toBe(1);
    expect(freshRefresh.detail.order.payment_status).toBe('failed');
    expect(freshRefresh.detail.audit_events.map((event) => event.action)).toEqual([
      'case.created_from_webhook',
    ]);
    expect(freshRefresh.queue.total).toBe(baselineQueue.total + 1);
    expect(
      freshRefresh.queue.items.find((item) => item.id === freshResult.case_id)?.source,
    ).toEqual(freshRefresh.detail.source);
    expect(freshRefresh.dashboard.summary).toEqual({
      open: baselineDashboard.summary.open + 1,
      approaching_sla: baselineDashboard.summary.approaching_sla + 1,
      high_severity: baselineDashboard.summary.high_severity + 1,
      resolved: baselineDashboard.summary.resolved,
    });
    const baselinePaymentFailedCount =
      baselineDashboard.by_rule.find((item) => item.rule_key === 'payment_failed')?.count ?? 0;
    expect(
      freshRefresh.dashboard.by_rule.find((item) => item.rule_key === 'payment_failed'),
    ).toEqual({
      rule_key: 'payment_failed',
      case_type: 'payment',
      count: baselinePaymentFailedCount + 1,
    });

    await expect(panel.getByText('A new payment failure was committed.')).toBeVisible();
    await expect(panel.getByText(freshResult.event_id, { exact: true })).toBeVisible();
    await expectSummaryCard(page, 'Open cases', freshRefresh.dashboard.summary.open);
    await expect(queue.getByText(`${freshRefresh.queue.total} total`)).toBeVisible();
    const orderNumber = freshRefresh.detail.order.order_number;
    const createdCaseRow = queue.getByRole('button', { name: `Open ${orderNumber}` });
    await expect(createdCaseRow.getByText('Synthetic webhook')).toBeVisible();
    await expect(page.getByRole('heading', { name: `Case ${orderNumber}` })).toBeVisible();
    const provenance = page.getByRole('region', { name: 'Event provenance' });
    await expect(provenance.getByText('Synthetic webhook')).toBeVisible();
    await expect(provenance.getByText('Synthetic', { exact: true })).toBeVisible();
    await expect(provenance.getByText('payment.failed')).toBeVisible();
    await expect(provenance.getByText(freshResult.event_id, { exact: true })).toBeVisible();
    const timeline = page.getByRole('region', { name: 'Accountable timeline' });
    await expect(timeline.getByText('Case.created from webhook by System')).toBeVisible();

    const freshSnapshot = await readCaseSnapshot(page, freshResult.case_id);

    const replayDeliveryPending = page.waitForResponse((response) =>
      response.url() === webhookTarget &&
      response.request().method() === 'POST' &&
      response.status() === 200,
    );
    const replayRefreshPending = operationalRefresh(
      page,
      `/api/cases/${freshResult.case_id}`,
    );
    await panel.getByRole('button', { name: 'Replay same event' }).click();
    const replayResult = await (await replayDeliveryPending).json() as WebhookDelivery;
    expect(replayResult).toEqual({
      status: 'processed',
      event_id: freshResult.event_id,
      case_id: freshResult.case_id,
      replayed: true,
    });
    await waitForOperationalRefresh(replayRefreshPending);
    await expect(panel.getByText('Exact replay returned the same case.')).toBeVisible();
    const replaySnapshot = await readCaseSnapshot(page, freshResult.case_id);
    expectNoBusinessChange(replaySnapshot, freshSnapshot);

    if (testInfo.project.name === 'desktop-chromium') {
      await verifyDesktop320Layout(page, freshResult.event_id);
    }

    browserDiagnostics.expectHttpFailureOnce({
      label: 'tampered synthetic webhook',
      method: 'POST',
      url: webhookTarget,
      status: 401,
    });
    const tamperResponsePending = page.waitForResponse((response) =>
      response.url() === webhookTarget &&
      response.request().method() === 'POST' &&
      response.status() === 401,
    );
    await panel.getByRole('button', { name: 'Tamper after signing' }).click();
    await expectAuthenticationRejection(await tamperResponsePending);
    await expect(page.getByRole('alert')).toContainText('Authentication rejected.');
    const afterTamper = await readCaseSnapshot(page, freshResult.case_id);
    expectNoBusinessChange(afterTamper, freshSnapshot);

    browserDiagnostics.expectHttpFailureOnce({
      label: 'stale synthetic webhook',
      method: 'POST',
      url: webhookTarget,
      status: 401,
    });
    const staleResponsePending = page.waitForResponse((response) =>
      response.url() === webhookTarget &&
      response.request().method() === 'POST' &&
      response.status() === 401,
    );
    await panel.getByRole('button', { name: 'Send stale signature' }).click();
    await expectAuthenticationRejection(await staleResponsePending);
    await expect(page.getByRole('alert')).toContainText('Authentication rejected.');
    const afterStale = await readCaseSnapshot(page, freshResult.case_id);
    expectNoBusinessChange(afterStale, freshSnapshot);

    const casePath = `/api/cases/${freshResult.case_id}`;
    await page.getByRole('combobox', { name: 'Assign to agent' }).selectOption({
      label: 'Demo Agent',
    });
    const assignmentPending = page.waitForResponse((response) =>
      isResponse(response, 'POST', `${casePath}/assignment`, 200),
    );
    const assignmentRefreshPending = operationalRefresh(page, casePath);
    await page.getByRole('button', { name: 'Update assignment' }).click();
    const assignmentReceipt = await (await assignmentPending).json() as {
      case_id: string;
      version: number;
    };
    expect(assignmentReceipt).toEqual({ case_id: freshResult.case_id, version: 2 });
    const assignedRefresh = await waitForOperationalRefresh(assignmentRefreshPending);
    expect(assignedRefresh.detail.audit_events.map((event) => event.action)).toEqual([
      'case.created_from_webhook',
      'case.assigned',
    ]);
    await expect(page.getByRole('region', { name: 'Order' }).getByText('Demo Agent')).toBeVisible();

    const switchPending = page.waitForResponse((response) =>
      isResponse(response, 'POST', '/api/demo/role', 200),
    );
    const agentQueuePending = page.waitForResponse((response) =>
      isResponse(response, 'GET', '/api/cases', 200),
    );
    await page.getByRole('button', { name: 'Switch to Agent' }).click();
    await switchPending;
    const agentQueue = await (await agentQueuePending).json() as CasePage;
    expect(agentQueue.items.some((item) => item.id === freshResult.case_id)).toBe(true);
    await expect(page.getByRole('heading', { name: 'Agent workspace' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Synthetic provider' })).toHaveCount(0);

    const agentDetailPending = page.waitForResponse((response) =>
      isResponse(response, 'GET', casePath, 200),
    );
    await page
      .getByRole('region', { name: 'Exception queue' })
      .getByRole('button', { name: `Open ${orderNumber}` })
      .click();
    await agentDetailPending;
    await expect(page.getByRole('region', { name: 'Event provenance' })).toContainText(
      freshResult.event_id,
    );

    const noteBody = 'Verified the synthetic payment recovery with the customer.';
    await page.getByRole('textbox', { name: 'Internal note' }).fill(noteBody);
    const notePending = page.waitForResponse((response) =>
      isResponse(response, 'POST', `${casePath}/notes`, 200),
    );
    const noteRefreshPending = operationalRefresh(page, casePath);
    await page.getByRole('button', { name: 'Add note' }).click();
    const noteReceipt = await (await notePending).json() as {
      case_id: string;
      version: number;
    };
    expect(noteReceipt).toEqual({ case_id: freshResult.case_id, version: 3 });
    await waitForOperationalRefresh(noteRefreshPending);
    await expect(page.getByRole('region', { name: 'Accountable timeline' })).toContainText(
      noteBody,
    );

    await page.getByRole('combobox', { name: 'Resolution reason' }).selectOption(
      'payment_recovered',
    );
    const resolutionPending = page.waitForResponse((response) =>
      isResponse(response, 'POST', `${casePath}/resolution`, 200),
    );
    const resolutionRefreshPending = operationalRefresh(page, casePath);
    await page.getByRole('button', { name: 'Resolve case' }).click();
    const resolutionReceipt = await (await resolutionPending).json() as {
      case_id: string;
      version: number;
    };
    expect(resolutionReceipt).toEqual({ case_id: freshResult.case_id, version: 4 });
    await waitForOperationalRefresh(resolutionRefreshPending);
    await expect(
      page.getByRole('article', { name: `Case ${orderNumber}` }).getByText('Resolved', {
        exact: true,
      }),
    ).toBeVisible();

    const finalDetail = await getJson<CaseDetail>(page, casePath);
    expect(finalDetail.status).toBe('resolved');
    expect(finalDetail.resolution_reason).toBe('payment_recovered');
    expect(finalDetail.source).toEqual(freshRefresh.detail.source);
    expect(finalDetail.notes).toEqual([
      expect.objectContaining({
        body: noteBody,
        author: expect.objectContaining({ display_name: 'Demo Agent' }),
      }),
    ]);
    expect(finalDetail.audit_events.map((event) => event.action)).toEqual([
      'case.created_from_webhook',
      'case.assigned',
      'case.note_added',
      'case.resolved',
    ]);

    const finalProvenance = page.getByRole('region', { name: 'Event provenance' });
    await expect(finalProvenance.getByText('Synthetic webhook')).toBeVisible();
    await expect(finalProvenance.getByText(freshResult.event_id, { exact: true })).toBeVisible();
    const finalTimeline = page.getByRole('region', { name: 'Accountable timeline' });
    await expect(finalTimeline.getByText('Case.created from webhook by System')).toBeVisible();
    await expect(finalTimeline.getByText('Assigned by Demo Manager')).toBeVisible();
    await expect(finalTimeline.getByText('Note added by Demo Agent')).toBeVisible();
    await expect(finalTimeline.getByText('Resolved by Demo Agent')).toBeVisible();
    await expect(finalTimeline.getByText(noteBody)).toBeVisible();
  });
});
