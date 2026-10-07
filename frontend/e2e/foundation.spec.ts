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

  test('renders the public product promise and offers both demo roles', async ({
    page,
  }) => {
    await page.goto('/');

    await expect(page).toHaveTitle('CommerceOps Desk');
    await expect(
      page.getByRole('heading', {
        level: 1,
        name: 'Turn ecommerce exceptions into accountable work.',
      }),
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 2, name: 'Built for a safe public demo' }),
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 2, name: 'Operational workflow' }),
    ).toBeVisible();
    await expect(page.getByText(/available in the temporary I03 workspace/i)).toBeVisible();
    await expect(page.getByText('Synthetic data only')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Enter as Manager' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Enter as Agent' })).toBeEnabled();
  });

  test('serves the SPA shell for a client-side workspace route', async ({ page }) => {
    const response = await page.goto('/workspaces/demo-preview');

    expect(response?.status()).toBe(200);
    await expect(
      page.getByRole('heading', {
        level: 1,
        name: 'Turn ecommerce exceptions into accountable work.',
      }),
    ).toBeVisible();
  });

  test('keeps the public entry usable at the 320px support boundary', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto('/');

    const managerEntry = page.getByRole('button', { name: 'Enter as Manager' });
    await expect(managerEntry).toBeEnabled();
    const layout = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    const managerBox = await managerEntry.boundingBox();

    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    expect(managerBox).not.toBeNull();
    expect((managerBox?.y ?? 800) + (managerBox?.height ?? 0)).toBeLessThan(800);
  });

  test('keeps unknown API routes as JSON 404 responses', async ({ page }) => {
    const response = await page.goto(EXPECTED_API_404_PATH);

    expect(response?.status()).toBe(404);
    expect(await response?.headerValue('content-type')).toContain('application/json');

    const responseBody = JSON.parse(await page.locator('body').innerText()) as unknown;
    expect(responseBody).toEqual({ detail: 'Not Found' });
  });
});
