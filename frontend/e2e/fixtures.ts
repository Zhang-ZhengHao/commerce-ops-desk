import { expect, test as base } from '@playwright/test';

export const EXPECTED_API_404_PATH = '/api/__e2e_missing__';

function isExpectedHttpFailure(method: string, url: string, status: number): boolean {
  const { pathname } = new URL(url);

  // The foundation spec deliberately proves that the SPA catch-all does not swallow API 404s.
  return method === 'GET' && pathname === EXPECTED_API_404_PATH && status === 404;
}

function isExpectedConsoleError(pageUrl: string, message: string): boolean {
  const isExpected404Page = new URL(pageUrl).pathname === EXPECTED_API_404_PATH;

  // Chromium mirrors the intentional top-level 404 response into the console.
  return (
    isExpected404Page &&
    message === 'Failed to load resource: the server responded with a status of 404 (Not Found)'
  );
}

export const test = base.extend<{ browserDiagnostics: void }>({
  browserDiagnostics: [
    async ({ page }, use, testInfo) => {
      const failures: string[] = [];

      const recordPageError = (error: Error) => {
        failures.push(`pageerror: ${error.stack ?? error.message}`);
      };
      const recordConsoleError = (message: { type(): string; text(): string }) => {
        if (
          message.type() === 'error' &&
          !isExpectedConsoleError(page.url(), message.text())
        ) {
          failures.push(`console.error: ${message.text()}`);
        }
      };
      const recordHttpFailure = (response: {
        status(): number;
        url(): string;
        request(): { method(): string };
      }) => {
        const status = response.status();
        const method = response.request().method();

        if (status >= 400 && !isExpectedHttpFailure(method, response.url(), status)) {
          failures.push(`http ${status}: ${method} ${response.url()}`);
        }
      };

      page.on('pageerror', recordPageError);
      page.on('console', recordConsoleError);
      page.on('response', recordHttpFailure);

      await use();

      page.off('pageerror', recordPageError);
      page.off('console', recordConsoleError);
      page.off('response', recordHttpFailure);

      if (failures.length > 0) {
        const body = failures.join('\n');
        await testInfo.attach('browser-diagnostics', {
          body,
          contentType: 'text/plain',
        });
        throw new Error(`Unexpected browser diagnostics:\n${body}`);
      }
    },
    { auto: true },
  ],
});

export { expect };
