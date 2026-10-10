import type { Locator, Page } from '@playwright/test';

import { expect, test } from './fixtures';

const FICTIONAL_TEXT_RULE =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';
const SYNTHETIC_WEBHOOK_PATH =
  /^\/api\/webhooks\/synthetic\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const CASE_DETAIL_PATH =
  /^\/api\/cases\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function isCommand(
  response: { url(): string; request(): { method(): string } },
  suffix: string,
) {
  return (
    new URL(response.url()).pathname.endsWith(suffix) &&
    response.request().method() === 'POST'
  );
}

function isCaseCommand(
  response: { url(): string; request(): { method(): string } },
  caseId: string,
  suffix: string,
) {
  return (
    new URL(response.url()).pathname === `/api/cases/${encodeURIComponent(caseId)}${suffix}` &&
    response.request().method() === 'POST'
  );
}

async function expectCommandReceipt(response: {
  status(): number;
  json(): Promise<unknown>;
}) {
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body).toEqual({
    case_id: expect.any(String),
    version: expect.any(Number),
  });
  expect(Object.keys(body as Record<string, unknown>)).toHaveLength(2);
  return body as { case_id: string; version: number };
}

async function pressTabUntilFocused(
  page: Page,
  target: Locator,
  maximumPresses: number,
  targetName: string,
): Promise<void> {
  for (let press = 1; press <= maximumPresses; press += 1) {
    await page.keyboard.press('Tab');
    if (await target.evaluate((element) => element === document.activeElement)) {
      await expect(target).toBeFocused();
      return;
    }
  }

  const focusedElement = await page.evaluate(() => {
    const activeElement = document.activeElement;
    if (!(activeElement instanceof HTMLElement)) return 'no HTML element';
    const label =
      activeElement.getAttribute('aria-label') ??
      activeElement.textContent?.replace(/\s+/g, ' ').trim().slice(0, 80) ??
      '';
    return label
      ? `${activeElement.tagName.toLowerCase()} "${label}"`
      : activeElement.tagName.toLowerCase();
  });
  throw new Error(
    `Keyboard focus did not reach "${targetName}" within ${maximumPresses} Tab presses; ` +
      `focus ended on ${focusedElement}.`,
  );
}

test.describe('I03 real case workflow', () => {
  test('uses Tab and Enter for entry, delivery, assignment, role switching, notes, and resolution', async ({
    page,
  }) => {
    await page.goto('/');
    const managerEntry = page.getByRole('button', { name: 'Enter as Manager' });
    await expect(managerEntry).toBeEnabled();
    await pressTabUntilFocused(page, managerEntry, 3, 'Enter as Manager');
    const createWorkspaceResponse = page.waitForResponse((response) =>
      isCommand(response, '/demo/workspaces'),
    );
    await page.keyboard.press('Enter');
    expect((await createWorkspaceResponse).status()).toBe(201);

    const overview = page.getByRole('region', { name: 'Operations overview' });
    const queue = page.getByRole('region', { name: 'Exception queue' });
    await expect(page.getByRole('heading', { name: 'Manager workspace' })).toBeVisible();
    await expect(overview.getByText('Open cases')).toBeVisible();
    await expect(queue.getByText('4 total')).toBeVisible();

    const provider = page.getByRole('region', { name: 'Synthetic provider' });
    const deliverFailure = page.getByRole('button', {
      name: 'Deliver new failure',
    });
    await expect(provider).toBeVisible();
    await expect(deliverFailure).toBeEnabled();
    await pressTabUntilFocused(page, deliverFailure, 4, 'Deliver new failure');
    const webhookDeliveryResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === 'POST' &&
        SYNTHETIC_WEBHOOK_PATH.test(url.pathname)
      );
    });
    const freshCaseDetailResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === 'GET' &&
        response.status() === 200 &&
        CASE_DETAIL_PATH.test(url.pathname)
      );
    });
    await page.keyboard.press('Enter');
    const webhookDelivery = await webhookDeliveryResponse;
    expect(webhookDelivery.status()).toBe(201);
    const webhookReceipt = (await webhookDelivery.json()) as {
      case_id: string;
      event_id: string;
      replayed: boolean;
      status: string;
    };
    expect(webhookReceipt).toEqual({
      case_id: expect.any(String),
      event_id: expect.any(String),
      replayed: false,
      status: 'processed',
    });
    const freshCaseResponse = await freshCaseDetailResponse;
    const freshCase = (await freshCaseResponse.json()) as {
      id: string;
      case_type: string;
      order: { order_number: string; payment_status: string };
      resolution_reasons: string[];
    };
    const freshCaseId = webhookReceipt.case_id;
    const freshCasePath = `/api/cases/${encodeURIComponent(freshCaseId)}`;
    expect(new URL(freshCaseResponse.url()).pathname).toBe(freshCasePath);
    expect(freshCase.id).toBe(freshCaseId);
    expect(freshCase.case_type).toBe('payment');
    expect(freshCase.order.payment_status).toBe('failed');
    expect(freshCase.resolution_reasons).toContain('payment_recovered');
    const orderNumber = freshCase.order.order_number;
    await expect(
      provider.getByText('A new payment failure was committed.'),
    ).toBeVisible();
    await expect(
      provider.getByText(webhookReceipt.event_id, { exact: true }),
    ).toBeVisible();
    await expect(queue.getByText('5 total')).toBeVisible();

    const freshCaseRow = queue.getByRole('button', { name: `Open ${orderNumber}` });
    await expect(page.getByRole('heading', { name: `Case ${orderNumber}` })).toBeVisible();
    await pressTabUntilFocused(page, freshCaseRow, 20, `Open ${orderNumber}`);
    const managerOpenResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === 'GET' &&
        response.status() === 200 &&
        url.pathname === freshCasePath
      );
    });
    await page.keyboard.press('Enter');
    expect((await managerOpenResponse).status()).toBe(200);
    await expect(page.getByRole('heading', { name: `Case ${orderNumber}` })).toBeVisible();
    const assignee = page.getByRole('combobox', { name: 'Assign to agent' });
    await pressTabUntilFocused(page, assignee, 8, 'Assign to agent');
    await page.keyboard.press('ArrowDown');
    await expect(assignee.locator('option:checked')).toHaveText('Demo Agent');
    const updateAssignment = page.getByRole('button', {
      name: 'Update assignment',
    });
    await pressTabUntilFocused(page, updateAssignment, 2, 'Update assignment');
    const assignmentResponse = page.waitForResponse((response) =>
      isCaseCommand(response, freshCaseId, '/assignment'),
    );
    await page.keyboard.press('Enter');
    const assignmentReceipt = await expectCommandReceipt(await assignmentResponse);
    expect(assignmentReceipt.case_id).toBe(freshCaseId);
    const order = page.getByRole('region', { name: 'Order' });
    await expect(order.getByText('Demo Agent')).toBeVisible();

    const switchToAgent = page.getByRole('button', { name: 'Switch to Agent' });
    await pressTabUntilFocused(page, switchToAgent, 8, 'Switch to Agent');
    const switchRoleResponse = page.waitForResponse((response) =>
      isCommand(response, '/demo/role'),
    );
    await page.keyboard.press('Enter');
    expect((await switchRoleResponse).status()).toBe(200);
    await expect(page.getByRole('heading', { name: 'Agent workspace' })).toBeVisible({
      timeout: 15_000,
    });
    const guide = page.getByRole('complementary', {
      name: 'Five-step evaluator guide',
    });
    await expect(guide).toBeVisible();
    await expect(guide.getByRole('listitem')).toHaveCount(5);
    await expect(queue.getByText('2 total')).toBeVisible();
    await pressTabUntilFocused(page, freshCaseRow, 8, `Open ${orderNumber}`);
    const agentOpenResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === 'GET' &&
        response.status() === 200 &&
        url.pathname === freshCasePath
      );
    });
    await page.keyboard.press('Enter');
    expect((await agentOpenResponse).status()).toBe(200);
    await expect(page.getByRole('heading', { name: `Case ${orderNumber}` })).toBeVisible();

    const note = page.getByRole('textbox', { name: 'Internal note' });
    await expect(page.getByText(FICTIONAL_TEXT_RULE, { exact: true })).toBeVisible();
    await expect(note).toHaveAccessibleDescription(FICTIONAL_TEXT_RULE);
    await pressTabUntilFocused(page, note, 5, 'Internal note');
    const noteBody =
      'Fictional payment recovery evidence checked against the synthetic demo order.';
    await page.keyboard.type(noteBody);
    await expect(note).toHaveValue(noteBody);
    const addNote = page.getByRole('button', { name: 'Add note' });
    await expect(addNote).toBeEnabled();
    await pressTabUntilFocused(page, addNote, 2, 'Add note');
    const noteResponse = page.waitForResponse((response) =>
      isCaseCommand(response, freshCaseId, '/notes'),
    );
    await page.keyboard.press('Enter');
    const noteReceipt = await expectCommandReceipt(await noteResponse);
    expect(noteReceipt.case_id).toBe(freshCaseId);
    await expect(
      page.getByRole('region', { name: 'Accountable timeline' }).getByText(noteBody),
    ).toBeVisible();

    const resolutionReason = page.getByRole('combobox', {
      name: 'Resolution reason',
    });
    await pressTabUntilFocused(page, resolutionReason, 16, 'Resolution reason');
    await page.keyboard.press('ArrowDown');
    await expect(resolutionReason).toHaveValue('customer_contacted');
    const resolveCase = page.getByRole('button', { name: 'Resolve case' });
    await pressTabUntilFocused(page, resolveCase, 2, 'Resolve case');
    const resolutionResponse = page.waitForResponse((response) =>
      isCaseCommand(response, freshCaseId, '/resolution'),
    );
    const resolvedCaseResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().method() === 'GET' &&
        response.status() === 200 &&
        url.pathname === freshCasePath
      );
    });
    await page.keyboard.press('Enter');
    const resolutionReceipt = await expectCommandReceipt(await resolutionResponse);
    expect(resolutionReceipt.case_id).toBe(freshCaseId);
    const resolvedCase = (await (await resolvedCaseResponse).json()) as {
      id: string;
      case_type: string;
      resolution_reason: string | null;
    };
    expect(resolvedCase).toEqual(
      expect.objectContaining({
        id: freshCaseId,
        case_type: 'payment',
        resolution_reason: 'customer_contacted',
      }),
    );
    await expect(
      page
        .getByRole('article', { name: `Case ${orderNumber}` })
        .getByText('Resolved', { exact: true }),
    ).toBeVisible();

    const previousWorkspaceName = (await page.locator('.workspace-meta').textContent())?.trim();
    expect(previousWorkspaceName).toBeTruthy();
    await page.getByRole('button', { name: 'Reset demo data' }).click();
    await expect(page.getByRole('heading', { name: 'Reset this workspace?' })).toBeVisible();
    const resetResponse = page.waitForResponse((response) =>
      isCommand(response, '/demo/reset'),
    );
    await page.getByRole('button', { name: 'Reset workspace', exact: true }).click();
    const reset = await resetResponse;
    expect(reset.status()).toBe(201);
    const resetSession = (await reset.json()) as {
      workspace: { name: string };
      identity: { role: string };
    };
    expect(resetSession.identity.role).toBe('agent');
    expect(resetSession.workspace.name).not.toBe(previousWorkspaceName);
    await expect(page.locator('.workspace-meta')).toHaveText(resetSession.workspace.name);
    await expect(queue.getByText('1 total')).toBeVisible();
  });
});
