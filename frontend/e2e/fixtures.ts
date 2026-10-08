import { expect, test as base } from '@playwright/test';

export const EXPECTED_API_404_PATH = '/api/__e2e_missing__';
export const SESSION_PATH = '/api/session';

export interface ExpectedHttpFailure {
  label: string;
  method: string;
  url: string;
  status: number;
}

export interface BrowserDiagnostics {
  expectHttpFailureOnce(expectation: ExpectedHttpFailure): void;
}

interface RegisteredHttpFailure extends ExpectedHttpFailure {
  consumed: boolean;
}

const WEBHOOK_TARGET =
  /\/api\/webhooks\/synthetic\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/gi;
const GENERIC_HTTP_CONSOLE_ERROR =
  /^Failed to load resource: the server responded with a status of ([45][0-9]{2}) \([^\r\n()]+\)$/;
const SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9 .:_-]{0,79}$/;

function redactDiagnostic(value: string): string {
  return value.replace(WEBHOOK_TARGET, '<redacted-target>');
}

function hasExactPath(url: string, expectedPath: string): boolean {
  const parsed = new URL(url);
  return parsed.pathname === expectedPath && parsed.search === '' && parsed.hash === '';
}

function genericHttpConsoleStatus(message: string): number | null {
  const match = GENERIC_HTTP_CONSOLE_ERROR.exec(message);
  return match ? Number(match[1]) : null;
}

function validateExpectation(expectation: ExpectedHttpFailure): void {
  if (!SAFE_LABEL.test(expectation.label)) {
    throw new Error('Expected HTTP failure labels must be short, single-line identifiers.');
  }
  if (!/^[A-Z]+$/.test(expectation.method)) {
    throw new Error('Expected HTTP failure methods must use uppercase ASCII letters.');
  }
  if (
    !Number.isInteger(expectation.status) ||
    expectation.status < 400 ||
    expectation.status > 599
  ) {
    throw new Error('Expected HTTP failure status must be an integer from 400 to 599.');
  }
  let parsed: URL;
  try {
    parsed = new URL(expectation.url);
  } catch {
    throw new Error('Expected HTTP failures require an absolute URL.');
  }
  if (
    (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
    parsed.username !== '' ||
    parsed.password !== '' ||
    parsed.hash !== ''
  ) {
    throw new Error('Expected HTTP failures require an HTTP(S) URL without credentials or a fragment.');
  }
}

export const test = base.extend<{ browserDiagnostics: BrowserDiagnostics }>({
  browserDiagnostics: [
    async ({ page }, use, testInfo) => {
      const failures: string[] = [];
      const registered: RegisteredHttpFailure[] = [];
      const consumedStatuses = new Map<number, number>();
      const consoleStatuses = new Map<number, number>();
      let anonymousSessionFailures = 0;
      let missingApiFailures = 0;

      const authorizeConsoleStatus = (status: number) => {
        consumedStatuses.set(status, (consumedStatuses.get(status) ?? 0) + 1);
      };

      const consumeRegisteredFailure = (
        method: string,
        url: string,
        status: number,
      ): boolean => {
        const expectation = registered.find(
          (candidate) =>
            !candidate.consumed &&
            candidate.method === method &&
            candidate.url === url &&
            candidate.status === status,
        );
        if (!expectation) return false;
        expectation.consumed = true;
        authorizeConsoleStatus(status);
        return true;
      };

      const consumeBuiltInFailure = (
        method: string,
        url: string,
        status: number,
      ): boolean => {
        if (
          method === 'GET' &&
          status === 401 &&
          hasExactPath(url, SESSION_PATH) &&
          anonymousSessionFailures === 0
        ) {
          anonymousSessionFailures += 1;
          authorizeConsoleStatus(status);
          return true;
        }
        if (
          method === 'GET' &&
          status === 404 &&
          hasExactPath(url, EXPECTED_API_404_PATH) &&
          missingApiFailures === 0
        ) {
          missingApiFailures += 1;
          authorizeConsoleStatus(status);
          return true;
        }
        return false;
      };

      const diagnostics: BrowserDiagnostics = {
        expectHttpFailureOnce(expectation) {
          validateExpectation(expectation);
          registered.push({ ...expectation, consumed: false });
        },
      };

      const recordPageError = (error: Error) => {
        failures.push(`pageerror: ${redactDiagnostic(error.stack ?? error.message)}`);
      };
      const recordConsoleError = (message: { type(): string; text(): string }) => {
        if (message.type() !== 'error') return;
        const status = genericHttpConsoleStatus(message.text());
        if (status !== null) {
          consoleStatuses.set(status, (consoleStatuses.get(status) ?? 0) + 1);
          return;
        }
        failures.push(`console.error: ${redactDiagnostic(message.text())}`);
      };
      const recordHttpFailure = (response: {
        status(): number;
        url(): string;
        request(): { method(): string };
      }) => {
        const status = response.status();
        if (status < 400) return;
        const method = response.request().method();
        const url = response.url();
        if (
          consumeRegisteredFailure(method, url, status) ||
          consumeBuiltInFailure(method, url, status)
        ) {
          return;
        }
        failures.push(`http ${status}: ${method} ${redactDiagnostic(url)}`);
      };

      page.on('pageerror', recordPageError);
      page.on('console', recordConsoleError);
      page.on('response', recordHttpFailure);

      try {
        await use(diagnostics);
      } finally {
        page.off('pageerror', recordPageError);
        page.off('console', recordConsoleError);
        page.off('response', recordHttpFailure);

        for (const expectation of registered) {
          if (!expectation.consumed) {
            failures.push(
              `expected http failure was not observed: ${expectation.label} ` +
                `${expectation.method} ${redactDiagnostic(expectation.url)} ` +
                `status ${expectation.status}`,
            );
          }
        }

        for (const [status, observed] of consoleStatuses) {
          const authorized = consumedStatuses.get(status) ?? 0;
          if (observed > authorized) {
            failures.push(
              `console.error: ${observed - authorized} unpaired generic HTTP ${status} ` +
                `failure${observed - authorized === 1 ? '' : 's'}`,
            );
          }
        }

        if (failures.length > 0) {
          const body = redactDiagnostic(failures.join('\n'));
          await testInfo.attach('browser-diagnostics', {
            body,
            contentType: 'text/plain',
          });
          throw new Error(`Unexpected browser diagnostics:\n${body}`);
        }
      }
    },
    { auto: true },
  ],
});

export { expect };
