import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { accessSync, constants, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig, devices } from '@playwright/test';

import { prepareE2EDatabase } from './e2e/database-lifecycle';

const frontendDirectory = path.dirname(fileURLToPath(import.meta.url));
const productDirectory = path.resolve(frontendDirectory, '..');
const externalBaseURL = process.env.PLAYWRIGHT_BASE_URL?.replace(/\/+$/, '');

function reserveEphemeralPort(): number {
  const probe = [
    "const net = require('node:net');",
    'const server = net.createServer();',
    "server.on('error', (error) => { console.error(error); process.exit(1); });",
    "server.listen(0, '127.0.0.1', () => {",
    '  const address = server.address();',
    "  if (!address || typeof address === 'string') process.exit(1);",
    '  process.stdout.write(String(address.port));',
    '  server.close();',
    '});',
  ].join('\n');

  const candidate = execFileSync(process.execPath, ['-e', probe], {
    encoding: 'utf8',
  }).trim();
  const port = Number(candidate);

  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Unable to allocate an E2E port: ${candidate}`);
  }

  return port;
}

const inheritedHostedPort = process.env.COMMERCE_OPS_E2E_PORT;
const hostedPort = externalBaseURL
  ? undefined
  : Number(inheritedHostedPort ?? reserveEphemeralPort());

if (
  hostedPort !== undefined &&
  (!Number.isInteger(hostedPort) || hostedPort < 1 || hostedPort > 65_535)
) {
  throw new Error(`COMMERCE_OPS_E2E_PORT must be an integer from 1 to 65535.`);
}

if (hostedPort !== undefined) {
  // Config is evaluated again in Playwright workers; preserve the runner's selected port.
  process.env.COMMERCE_OPS_E2E_PORT = String(hostedPort);
}

const baseURL = externalBaseURL ?? `http://127.0.0.1:${hostedPort}`;

function isWritableDirectory(candidate: string): boolean {
  try {
    if (!statSync(candidate).isDirectory()) return false;
    accessSync(candidate, constants.W_OK | constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function e2eDatabaseDirectory(): string {
  const configuredDirectory = process.env.COMMERCE_OPS_E2E_DATABASE_DIR?.trim();
  if (configuredDirectory) {
    const resolvedDirectory = path.resolve(configuredDirectory);
    if (!isWritableDirectory(resolvedDirectory)) {
      throw new Error(
        `COMMERCE_OPS_E2E_DATABASE_DIR must be a writable directory: ${resolvedDirectory}`,
      );
    }
    return resolvedDirectory;
  }

  const linuxSharedMemory = '/dev/shm';
  if (process.platform === 'linux' && isWritableDirectory(linuxSharedMemory)) {
    return linuxSharedMemory;
  }

  const fallbackDirectory = tmpdir();
  if (!isWritableDirectory(fallbackDirectory)) {
    throw new Error(`The operating-system temp directory is not writable: ${fallbackDirectory}`);
  }
  return fallbackDirectory;
}

const preparedDatabase =
  hostedPort === undefined
    ? undefined
    : prepareE2EDatabase({
        directory: e2eDatabaseDirectory(),
        pid: process.pid,
        port: hostedPort,
      });

function hostedEnvironment(): Record<string, string> {
  if (hostedPort === undefined || preparedDatabase === undefined) {
    throw new Error('The internal E2E web server requires a reserved port.');
  }

  const databasePath = preparedDatabase.databasePath;

  const inheritedEnvironment = Object.fromEntries(
    Object.entries(process.env).filter(
      (entry): entry is [string, string] => entry[1] !== undefined,
    ),
  );
  const sessionSecretBytes = randomBytes(48);
  let webhookSecretBytes = randomBytes(48);
  while (sessionSecretBytes.equals(webhookSecretBytes)) {
    webhookSecretBytes = randomBytes(48);
  }

  return {
    ...inheritedEnvironment,
    COMMERCE_OPS_DATABASE_URL: `sqlite+pysqlite:///${databasePath}`,
    COMMERCE_OPS_DEMO_MODE: 'true',
    COMMERCE_OPS_ENVIRONMENT: 'test',
    COMMERCE_OPS_SESSION_SECRET: sessionSecretBytes.toString('base64url'),
    COMMERCE_OPS_VENV_DIR:
      process.env.COMMERCE_OPS_VENV_DIR ?? path.join(productDirectory, '.venv'),
    COMMERCE_OPS_WEBHOOK_ENABLED: 'true',
    COMMERCE_OPS_WEBHOOK_MASTER_SECRET: webhookSecretBytes.toString('base64url'),
    PORT: String(hostedPort),
  };
}

export default defineConfig({
  testDir: './e2e',
  testIgnore: '**/*.test.ts',
  outputDir: './test-results',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [
    ['line'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
  ],
  use: {
    baseURL,
    colorScheme: 'dark',
    locale: 'en-US',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  ...(externalBaseURL
    ? {}
    : {
        globalTeardown: './e2e/database-global-teardown.ts',
        webServer: {
          command: 'npm run build && bash ../scripts/start-hosted.sh',
          cwd: frontendDirectory,
          env: hostedEnvironment(),
          url: `${baseURL}/health`,
          reuseExistingServer: false,
          timeout: 120_000,
          stdout: 'pipe' as const,
          stderr: 'pipe' as const,
        },
      }),
  projects: [
    {
      name: 'desktop-chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
      },
    },
    {
      name: 'mobile-chromium',
      use: {
        ...devices['Pixel 7'],
      },
    },
  ],
});
