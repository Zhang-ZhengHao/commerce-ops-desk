import { expect, test as base } from '@playwright/test';

export const EXPECTED_API_404_PATH = '/api/__e2e_missing__';
export const SESSION_PATH = '/api/session';

function isExpectedHttpFailure(method: string, url: string, status: number): boolean {
  const { pathname } = new URL(url);

  // The foundation spec deliberately proves that the SPA catch-all does not swallow API 404s.
  if (method === 'GET' && pathname === EXPECTED_API_404_PATH && status === 404) {
    return true;
  }

  // A visitor without a demo cookie is expected to begin in the public entry state.
  return method === 'GET' && pathname === SESSION_PATH && status === 401;
}

function isExpectedConsoleError(pageUrl: string, message: string): boolean {
  const isExpected404Page = new URL(pageUrl).pathname === EXPECTED_API_404_PATH;

  // Chromium mirrors the intentional top-level 404 response into the console.
  const isIntentionalApi404 =
    isExpected404Page &&
    message === 'Failed to load resource: the server responded with a status of 404 (Not Found)';

  // The response guard still rejects every unexpected 401. This only suppresses Chromium's
  // duplicate console line for the expected anonymous GET /api/session response.
  const isAnonymousSessionProbe =
    message ===
    'Failed to load resource: the server responded with a status of 401 (Unauthorized)';

  return isIntentionalApi404 || isAnonymousSessionProbe;
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
