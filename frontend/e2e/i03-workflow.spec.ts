import { expect, test } from './fixtures';

const FICTIONAL_TEXT_RULE =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';

function isCommand(
  response: { url(): string; request(): { method(): string } },
  suffix: string,
) {
  return (
    new URL(response.url()).pathname.endsWith(suffix) &&
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

test.describe('I03 real case workflow', () => {
  test('moves a refund case from Manager assignment to Agent resolution', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Enter as Manager' }).click();

    const overview = page.getByRole('region', { name: 'Operations overview' });
    const queue = page.getByRole('region', { name: 'Exception queue' });
    await expect(overview.getByText('Open cases')).toBeVisible();
    await expect(queue.getByText('4 total')).toBeVisible();

    await queue.getByRole('button', { name: 'Open DEMO-1043' }).click();
    await expect(page.getByRole('heading', { name: 'Case DEMO-1043' })).toBeVisible();
    await page.getByRole('combobox', { name: 'Assign to agent' }).selectOption({
      label: 'Demo Agent',
    });
    const assignmentResponse = page.waitForResponse((response) =>
      isCommand(response, '/assignment'),
    );
    await page.getByRole('button', { name: 'Update assignment' }).click();
    const assignmentReceipt = await expectCommandReceipt(await assignmentResponse);
    const order = page.getByRole('region', { name: 'Order' });
    await expect(order.getByText('Demo Agent')).toBeVisible();

    await page.getByRole('button', { name: 'Switch to Agent' }).click();
    await expect(page.getByRole('heading', { name: 'Agent workspace' })).toBeVisible({
      timeout: 15_000,
    });
    const guide = page.getByRole('complementary', {
      name: 'Five-step evaluator guide',
    });
    await expect(guide).toBeVisible();
    await expect(guide.getByRole('listitem')).toHaveCount(5);
    await expect(queue.getByText('2 total')).toBeVisible();
    await queue.getByRole('button', { name: 'Open DEMO-1043' }).click();

    const note = page.getByRole('textbox', { name: 'Internal note' });
    await expect(page.getByText(FICTIONAL_TEXT_RULE, { exact: true })).toBeVisible();
    await expect(note).toHaveAccessibleDescription(FICTIONAL_TEXT_RULE);
    const noteBody =
      'Fictional refund evidence checked against the synthetic demo order.';
    await note.fill(noteBody);
    const noteResponse = page.waitForResponse((response) =>
      isCommand(response, '/notes'),
    );
    await page.getByRole('button', { name: 'Add note' }).click();
    const noteReceipt = await expectCommandReceipt(await noteResponse);
    expect(noteReceipt.case_id).toBe(assignmentReceipt.case_id);
    await expect(page.getByRole('region', { name: 'Accountable timeline' }).getByText(noteBody)).toBeVisible();

    await page.getByRole('combobox', { name: 'Resolution reason' }).selectOption(
      'refund_approved',
    );
    const resolutionResponse = page.waitForResponse((response) =>
      isCommand(response, '/resolution'),
    );
    await page.getByRole('button', { name: 'Resolve case' }).click();
    const resolutionReceipt = await expectCommandReceipt(await resolutionResponse);
    expect(resolutionReceipt.case_id).toBe(assignmentReceipt.case_id);
    await expect(
      page
        .getByRole('article', { name: 'Case DEMO-1043' })
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
    const resetSession = await reset.json() as {
      workspace: { name: string };
      identity: { role: string };
    };
    expect(resetSession.identity.role).toBe('agent');
    expect(resetSession.workspace.name).not.toBe(previousWorkspaceName);
    await expect(page.locator('.workspace-meta')).toHaveText(resetSession.workspace.name);
    await expect(queue.getByText('1 total')).toBeVisible();
  });
});
