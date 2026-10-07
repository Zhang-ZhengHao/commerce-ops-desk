import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from './fixtures';

const assetsDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../docs/assets',
);

test.describe('README screenshots', () => {
  test.skip(
    process.env.UPDATE_README_SCREENSHOTS !== '1',
    'Run npm run capture:demo-entry to update the checked-in asset.',
  );

  test('captures the public demo entry as one full-page image', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');

    await expect(page.getByRole('button', { name: 'Enter as Manager' })).toBeEnabled();
    await expect(page.getByText('Synthetic exception queue')).toBeVisible();
    await expect(page.getByText(/signed commerce event/i)).toHaveCount(0);

    await page.evaluate(async () => {
      if (document.activeElement instanceof HTMLElement) {
        document.activeElement.blur();
      }
      window.scrollTo(0, 0);
      await document.fonts.ready;
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });
    });

    const skipLinkBox = await page
      .getByRole('link', { name: 'Skip to main content' })
      .boundingBox();
    expect(skipLinkBox).not.toBeNull();
    expect((skipLinkBox?.y ?? 0) + (skipLinkBox?.height ?? 0)).toBeLessThanOrEqual(0);
    expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }))).toEqual({
      x: 0,
      y: 0,
    });

    await page.screenshot({
      animations: 'disabled',
      caret: 'hide',
      fullPage: true,
      path: path.join(assetsDirectory, 'demo-entry-i03.png'),
      scale: 'css',
    });
  });
});
