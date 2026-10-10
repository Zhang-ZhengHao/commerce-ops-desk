import { EXPECTED_API_404_PATH, expect, test } from './fixtures';

const EVALUATOR_SOURCE_SHA = '0123456789abcdef0123456789abcdef01234567';
const PUBLIC_REPOSITORY_URL =
  'https://github.com/Zhang-ZhengHao/commerce-ops-desk';
const FICTIONAL_TEXT_RULE =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';
const EVALUATOR_STEPS = [
  'Create an event',
  'Test the boundary',
  'Assign the case',
  'Work as Agent',
  'Verify the trail',
] as const;

async function expectNoHorizontalOverflow(page: {
  evaluate<T>(callback: () => T): Promise<T>;
}) {
  const layout = await page.evaluate(() => {
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

  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.offenders).toEqual([]);
}

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

    const buildResponse = await request.get('/api/build');
    expect(buildResponse.status()).toBe(200);
    expect(buildResponse.headers()['cache-control']).toBe('no-store');
    expect(await buildResponse.json()).toEqual({
      service: 'commerce-ops-desk',
      version: '0.2.1',
      source_sha: EVALUATOR_SOURCE_SHA,
    });
  });

  test('renders the synthetic demo promise and offers both demo roles', async ({
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
      page.getByRole('heading', {
        level: 2,
        name: 'Boundaries of this synthetic demo',
      }),
    ).toBeVisible();
    const guide = page.getByRole('complementary', {
      name: 'Five-step evaluator guide',
    });
    await expect(guide).toBeVisible();
    const guideSteps = guide.getByRole('listitem');
    await expect(guideSteps).toHaveCount(5);
    await expect(guideSteps.locator('strong')).toHaveText([...EVALUATOR_STEPS]);
    await expect(page.getByText(FICTIONAL_TEXT_RULE, { exact: true })).toBeVisible();
    await expect(page.getByText('Synthetic data only')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Enter as Manager' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Enter as Agent' })).toBeEnabled();

    const footer = page.getByRole('contentinfo');
    await expect(footer.getByText('v0.2.1', { exact: true })).toBeVisible();
    const sourceLink = footer.getByRole('link', { name: EVALUATOR_SOURCE_SHA });
    await expect(sourceLink).toHaveAttribute(
      'href',
      `${PUBLIC_REPOSITORY_URL}/commit/${EVALUATOR_SOURCE_SHA}`,
    );
    await expect(
      footer.getByRole('link', { name: 'Public walkthrough · v0.2.0' }),
    ).toHaveAttribute(
      'href',
      `${PUBLIC_REPOSITORY_URL}/releases/tag/v0.2.0`,
    );
    await expectNoHorizontalOverflow(page);
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
    const managerBox = await managerEntry.boundingBox();

    expect(managerBox).not.toBeNull();
    expect((managerBox?.y ?? 800) + (managerBox?.height ?? 0)).toBeLessThan(800);
    await expect(
      page.getByRole('contentinfo').getByRole('link', { name: EVALUATOR_SOURCE_SHA }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test('keeps unknown API routes as JSON 404 responses', async ({ page }) => {
    const response = await page.goto(EXPECTED_API_404_PATH);

    expect(response?.status()).toBe(404);
    expect(await response?.headerValue('content-type')).toContain('application/json');

    const responseBody = JSON.parse(await page.locator('body').innerText()) as unknown;
    expect(responseBody).toEqual({ detail: 'Not Found' });
  });
});
