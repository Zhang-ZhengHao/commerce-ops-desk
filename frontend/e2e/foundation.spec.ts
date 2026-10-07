import { EXPECTED_API_404_PATH, expect, test } from './fixtures';

test.describe('I01 hosted foundation', () => {
  test('publishes liveness and database readiness as JSON', async ({ request }) => {
    const healthResponse = await request.get('/health');
    expect(healthResponse.status()).toBe(200);
    expect(await healthResponse.json()).toEqual({
      status: 'ok',
      service: 'commerce-ops-desk',
    });

    const readinessResponse = await request.get('/ready');
    expect(readinessResponse.status()).toBe(200);
    expect(await readinessResponse.json()).toEqual({
      status: 'ready',
      database: 'reachable',
    });
  });

  test('renders the public product promise and keeps unfinished roles disabled', async ({
    page,
  }) => {
    await page.goto('/');

    await expect(page).toHaveTitle('CommerceOps Desk');
    await expect(
      page.getByRole('heading', { level: 1, name: 'CommerceOps Desk' }),
    ).toBeVisible();
    await expect(
      page.getByText('Turn ecommerce exceptions into owned, auditable work.'),
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 2, name: 'Clear by design' }),
    ).toBeVisible();
    await expect(page.getByText('Synthetic data only')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Enter as Manager' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Enter as Agent' })).toBeDisabled();
  });

  test('serves the SPA shell for a client-side workspace route', async ({ page }) => {
    const response = await page.goto('/workspaces/demo-preview');

    expect(response?.status()).toBe(200);
    await expect(
      page.getByRole('heading', { level: 1, name: 'CommerceOps Desk' }),
    ).toBeVisible();
  });

  test('keeps unknown API routes as JSON 404 responses', async ({ page }) => {
    const response = await page.goto(EXPECTED_API_404_PATH);

    expect(response?.status()).toBe(404);
    expect(await response?.headerValue('content-type')).toContain('application/json');

    const responseBody = JSON.parse(await page.locator('body').innerText()) as unknown;
    expect(responseBody).toEqual({ detail: 'Not Found' });
  });
});
