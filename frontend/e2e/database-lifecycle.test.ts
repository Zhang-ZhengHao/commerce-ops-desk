// @vitest-environment node

import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cleanupPreparedE2EDatabase,
  cleanupStaleE2EDatabases,
  detectProcessLiveness,
  detectProcessStartTime,
  E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE,
  E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE,
  prepareE2EDatabase,
  type PrepareE2EDatabaseOptions,
  type ProcessLiveness,
} from './database-lifecycle';

const databaseFilename = 'commerce-ops-desk.sqlite3';
const directoryDeviceEnvironmentVariable =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_DEVICE';
const directoryInodeEnvironmentVariable =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_INODE';
const directoryOwnerEnvironmentVariable =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_OWNER_UID';
const temporaryDirectories: string[] = [];
const originalEnvironment = new Map<string, string | undefined>();
const mutableFileSystem = createRequire(import.meta.url)('node:fs') as {
  renameSync: typeof renameSync;
};

function temporaryDirectory(): string {
  const directory = mkdtempSync(path.join(tmpdir(), 'commerce-ops-desk-e2e-lifecycle-'));
  temporaryDirectories.push(directory);
  return directory;
}

function createDirectory(parent: string, name: string): string {
  const directory = path.join(parent, name);
  mkdirSync(directory, { mode: 0o700 });
  chmodSync(directory, 0o700);
  return directory;
}

function runDirectory(
  baseDirectory: string,
  pid: number,
  port: number,
  token: string,
): string {
  return path.join(
    baseDirectory,
    `commerce-ops-desk-playwright-${pid}-${port}-12345-${token}`,
  );
}

function identityBoundRunDirectory(
  baseDirectory: string,
  pid: number,
  port: number,
  startTimeTicks: string,
  token: string,
): string {
  return path.join(
    baseDirectory,
    `commerce-ops-desk-playwright-${pid}-${port}-${startTimeTicks}-${token}`,
  );
}

function databasePath(runDirectoryPath: string): string {
  return path.join(runDirectoryPath, databaseFilename);
}

function databaseGroup(databaseFile: string): string[] {
  return [databaseFile, `${databaseFile}-wal`, `${databaseFile}-shm`];
}

function createRegularFile(filename: string, contents = 'fixture'): void {
  writeFileSync(filename, contents, { mode: 0o600 });
  chmodSync(filename, 0o600);
}

function createDatabaseGroup(databaseFile: string): string[] {
  const files = databaseGroup(databaseFile);
  for (const filename of files) createRegularFile(filename);
  return files;
}

function createRunDirectory(
  baseDirectory: string,
  pid: number,
  port: number,
  token: string,
): { directory: string; files: string[] } {
  const directory = runDirectory(baseDirectory, pid, port, token);
  mkdirSync(directory, { mode: 0o700 });
  chmodSync(directory, 0o700);
  return { directory, files: createDatabaseGroup(databasePath(directory)) };
}

function permissions(filename: string): number {
  return Number(lstatSync(filename, { bigint: true }).mode & 0o777n);
}

function prepareTestE2EDatabase(
  options: Omit<PrepareE2EDatabaseOptions, 'processStartTime'>,
) {
  return prepareE2EDatabase({
    ...options,
    processStartTime: () => '12345',
  });
}

function setEnvironment(name: string, value: string | undefined): void {
  if (!originalEnvironment.has(name)) originalEnvironment.set(name, process.env[name]);
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
  for (const [name, value] of originalEnvironment) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  originalEnvironment.clear();
  vi.resetModules();
});

describe('detectProcessLiveness', () => {
  it('reports a process as alive when signal zero succeeds', () => {
    expect(detectProcessLiveness(123, () => undefined)).toBe('alive');
  });

  it('reports a process as dead only for ESRCH', () => {
    expect(
      detectProcessLiveness(123, () => {
        throw Object.assign(new Error('missing'), { code: 'ESRCH' });
      }),
    ).toBe('dead');
  });

  it.each(['EPERM', 'EACCES', 'EIO'])(
    'keeps process state unknown when signal zero fails with %s',
    (code) => {
      expect(
        detectProcessLiveness(123, () => {
          throw Object.assign(new Error(code), { code });
        }),
      ).toBe('unknown');
    },
  );
});

describe('detectProcessStartTime', () => {
  it('reads field 22 when a Linux process name contains spaces and parentheses', () => {
    const fieldsAfterCommand = [
      'S',
      '1',
      '1',
      '1',
      '0',
      '-1',
      '0',
      '0',
      '0',
      '0',
      '0',
      '0',
      '0',
      '0',
      '0',
      '20',
      '0',
      '1',
      '0',
      '987654',
    ];

    expect(
      detectProcessStartTime(
        321,
        () => `321 (worker ) name) ${fieldsAfterCommand.join(' ')}`,
      ),
    ).toBe('987654');
  });
});

describe('prepareE2EDatabase', () => {
  it('does not follow a symlink pre-positioned at the old predictable final path', () => {
    const sandbox = temporaryDirectory();
    const selectedDirectory = createDirectory(sandbox, 'selected');
    const victim = path.join(sandbox, 'victim');
    const predictablePath = path.join(
      selectedDirectory,
      'commerce-ops-desk-playwright-701-3701.sqlite3',
    );
    createRegularFile(victim, 'victim');
    symlinkSync(victim, predictablePath);

    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment: {},
      liveness: () => 'dead',
      pid: 701,
      port: 3701,
    });
    writeFileSync(prepared.databasePath, 'sqlite database');

    expect(readFileSync(victim, 'utf8')).toBe('victim');
    expect(lstatSync(prepared.databasePath).isFile()).toBe(true);
    expect(permissions(prepared.databasePath)).toBe(0o600);
    expect(path.dirname(path.dirname(prepared.databasePath))).toBe(
      realpathSync(selectedDirectory),
    );
    expect(path.basename(path.dirname(prepared.databasePath))).toMatch(
      /^commerce-ops-desk-playwright-701-3701-12345-[A-Za-z0-9]{6}$/,
    );
    expect(permissions(path.dirname(prepared.databasePath))).toBe(0o700);
  });

  it('records and reuses one canonical private-directory identity', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};

    const owner = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      liveness: () => 'dead',
      pid: 702,
      port: 3702,
    });
    const privateDirectory = path.dirname(owner.databasePath);
    const metadata = lstatSync(privateDirectory, { bigint: true });
    const repeatedEvaluation = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      liveness: () => {
        throw new Error('Worker config evaluation must not rescan the base directory.');
      },
      pid: 703,
      port: 3702,
    });

    expect(owner.ownsLifecycle).toBe(true);
    expect(owner.databasePath).toBe(databasePath(privateDirectory));
    expect(environment[E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE]).toBe(
      owner.databasePath,
    );
    expect(environment[E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE]).toBe(
      realpathSync(privateDirectory),
    );
    expect(environment[directoryDeviceEnvironmentVariable]).toBe(
      metadata.dev.toString(),
    );
    expect(environment[directoryInodeEnvironmentVariable]).toBe(
      metadata.ino.toString(),
    );
    expect(environment[directoryOwnerEnvironmentVariable]).toBe(
      metadata.uid.toString(),
    );
    expect(repeatedEvaluation).toEqual({
      databasePath: owner.databasePath,
      ownsLifecycle: false,
    });
    expect(lstatSync(owner.databasePath).isFile()).toBe(true);
  });

  it.each([
    directoryDeviceEnvironmentVariable,
    directoryInodeEnvironmentVariable,
    directoryOwnerEnvironmentVariable,
  ])('refuses worker reuse when %s no longer matches', (identityVariable) => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const owner = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 704,
      port: 3704,
    });
    environment[identityVariable] = (
      BigInt(environment[identityVariable] ?? '0') + 1n
    ).toString();

    expect(() =>
      prepareTestE2EDatabase({
        directory: selectedDirectory,
        environment,
        pid: 705,
        port: 3704,
      }),
    ).toThrow();
    expect(existsSync(owner.databasePath)).toBe(true);
  });

  it('refuses worker reuse after the private directory mode changes', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const owner = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 706,
      port: 3706,
    });
    chmodSync(path.dirname(owner.databasePath), 0o755);

    expect(() =>
      prepareTestE2EDatabase({
        directory: selectedDirectory,
        environment,
        pid: 707,
        port: 3706,
      }),
    ).toThrow();
    expect(existsSync(owner.databasePath)).toBe(true);
  });
});

describe('cleanupStaleE2EDatabases', () => {
  it('removes a stale run when its PID has been reused by a newer process', () => {
    const baseDirectory = temporaryDirectory();
    const directory = identityBoundRunDirectory(
      baseDirectory,
      100,
      3100,
      '12345',
      'Aa10Bb',
    );
    mkdirSync(directory, { mode: 0o700 });
    chmodSync(directory, 0o700);
    createDatabaseGroup(databasePath(directory));

    cleanupStaleE2EDatabases(
      baseDirectory,
      () => 'alive',
      () => '67890',
    );

    expect(existsSync(directory)).toBe(false);
  });

  it('preserves a run owned by the same live process instance', () => {
    const baseDirectory = temporaryDirectory();
    const directory = identityBoundRunDirectory(
      baseDirectory,
      100,
      3100,
      '12345',
      'Cc10Dd',
    );
    mkdirSync(directory, { mode: 0o700 });
    chmodSync(directory, 0o700);
    const files = createDatabaseGroup(databasePath(directory));

    cleanupStaleE2EDatabases(
      baseDirectory,
      () => 'alive',
      () => '12345',
    );

    expect(files.every(existsSync)).toBe(true);
  });

  it('isolates a claimed stale directory from a same-name replacement', () => {
    const baseDirectory = temporaryDirectory();
    const directory = identityBoundRunDirectory(
      baseDirectory,
      100,
      3100,
      '12345',
      'Ee10Ff',
    );
    mkdirSync(directory, { mode: 0o700 });
    chmodSync(directory, 0o700);
    createDatabaseGroup(databasePath(directory));
    let probes = 0;

    cleanupStaleE2EDatabases(
      baseDirectory,
      () => {
        probes += 1;
        if (probes === 2) {
          mkdirSync(directory, { mode: 0o700 });
          chmodSync(directory, 0o700);
          createRegularFile(databasePath(directory), 'replacement');
        }
        return 'dead';
      },
      () => undefined,
    );

    expect(probes).toBe(2);
    expect(readFileSync(databasePath(directory), 'utf8')).toBe('replacement');
  });

  it('removes a safe private run directory only for a definitely dead PID', () => {
    const baseDirectory = temporaryDirectory();
    const stale = createRunDirectory(baseDirectory, 101, 3101, 'Aa11Bb');
    const live = createRunDirectory(baseDirectory, 202, 3202, 'Cc22Dd');
    const unknown = createRunDirectory(baseDirectory, 303, 3303, 'Ee33Ff');
    const states = new Map<number, ProcessLiveness>([
      [101, 'dead'],
      [202, 'alive'],
      [303, 'unknown'],
    ]);

    cleanupStaleE2EDatabases(
      baseDirectory,
      (pid) => states.get(pid) ?? 'unknown',
      (pid) => (pid === 202 ? '12345' : undefined),
    );

    expect(existsSync(stale.directory)).toBe(false);
    expect(live.files.every(existsSync)).toBe(true);
    expect(unknown.files.every(existsSync)).toBe(true);
  });

  it('preserves legacy flat files outside the private-run-directory contract', () => {
    const baseDirectory = temporaryDirectory();
    const legacyDatabase = path.join(
      baseDirectory,
      'commerce-ops-desk-playwright-404-3404.sqlite3',
    );
    const legacyFiles = createDatabaseGroup(legacyDatabase);

    cleanupStaleE2EDatabases(
      baseDirectory,
      () => 'dead',
      () => undefined,
    );

    expect(legacyFiles.every(existsSync)).toBe(true);
  });

  it('preserves malformed, symlinked, wrong-mode, and unknown-entry groups', () => {
    const baseDirectory = temporaryDirectory();
    const outside = createDirectory(baseDirectory, 'outside-target');
    const directorySymlink = runDirectory(baseDirectory, 405, 3405, 'Gg44Hh');
    symlinkSync(outside, directorySymlink, 'dir');

    const malformed = path.join(
      baseDirectory,
      'commerce-ops-desk-playwright-406-3406-short',
    );
    mkdirSync(malformed, { mode: 0o700 });
    const unknownEntry = createRunDirectory(baseDirectory, 407, 3407, 'Ii55Jj');
    createRegularFile(path.join(unknownEntry.directory, 'notes.txt'));
    const symlinkedFile = runDirectory(baseDirectory, 408, 3408, 'Kk66Ll');
    mkdirSync(symlinkedFile, { mode: 0o700 });
    chmodSync(symlinkedFile, 0o700);
    const victim = path.join(outside, 'victim');
    createRegularFile(victim, 'victim');
    symlinkSync(victim, databasePath(symlinkedFile));
    const wrongMode = createRunDirectory(baseDirectory, 409, 3409, 'Mm77Nn');
    chmodSync(wrongMode.directory, 0o755);

    cleanupStaleE2EDatabases(
      baseDirectory,
      () => 'dead',
      () => undefined,
    );

    expect(lstatSync(directorySymlink).isSymbolicLink()).toBe(true);
    expect(existsSync(malformed)).toBe(true);
    expect(unknownEntry.files.every(existsSync)).toBe(true);
    expect(existsSync(path.join(unknownEntry.directory, 'notes.txt'))).toBe(true);
    expect(lstatSync(databasePath(symlinkedFile)).isSymbolicLink()).toBe(true);
    expect(readFileSync(victim, 'utf8')).toBe('victim');
    expect(wrongMode.files.every(existsSync)).toBe(true);
  });

  it('preserves a same-name replacement whose inode changes during cleanup', () => {
    const baseDirectory = temporaryDirectory();
    const original = createRunDirectory(baseDirectory, 410, 3410, 'Oo88Pp');
    const parked = path.join(baseDirectory, 'parked-original');
    let probeCalled = false;

    cleanupStaleE2EDatabases(
      baseDirectory,
      (pid) => {
        if (pid !== 410) return 'unknown';
        probeCalled = true;
        renameSync(original.directory, parked);
        mkdirSync(original.directory, { mode: 0o700 });
        chmodSync(original.directory, 0o700);
        createRegularFile(databasePath(original.directory), 'replacement');
        return 'dead';
      },
      () => undefined,
    );

    expect(probeCalled).toBe(true);
    expect(readFileSync(databasePath(original.directory), 'utf8')).toBe(
      'replacement',
    );
    expect(original.files.every((filename) => existsSync(path.join(parked, path.basename(filename))))).toBe(true);
  });
});

describe('cleanupPreparedE2EDatabase', () => {
  it('claims the recorded directory before deleting through its stable identity', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 900,
      port: 3900,
    });
    const preparedDirectory = path.dirname(prepared.databasePath);
    const originalRenameSync = mutableFileSystem.renameSync;
    let directoryWasClaimed = false;

    mutableFileSystem.renameSync = ((source, destination) => {
      originalRenameSync(source, destination);
      if (source === preparedDirectory) {
        directoryWasClaimed = true;
        mkdirSync(preparedDirectory, { mode: 0o700 });
        chmodSync(preparedDirectory, 0o700);
        createRegularFile(prepared.databasePath, 'replacement');
      }
    }) as typeof renameSync;
    syncBuiltinESMExports();

    try {
      cleanupPreparedE2EDatabase(environment);
    } finally {
      mutableFileSystem.renameSync = originalRenameSync;
      syncBuiltinESMExports();
    }

    expect(directoryWasClaimed).toBe(true);
    expect(readFileSync(prepared.databasePath, 'utf8')).toBe('replacement');
  });

  it('removes only the recorded database group and then its private directory', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 901,
      port: 3901,
    });
    const privateDirectory = path.dirname(prepared.databasePath);
    createRegularFile(`${prepared.databasePath}-wal`);
    createRegularFile(`${prepared.databasePath}-shm`);

    cleanupPreparedE2EDatabase(environment);

    expect(existsSync(privateDirectory)).toBe(false);
    expect(existsSync(selectedDirectory)).toBe(true);
  });

  it('refuses a symlink replacement of the recorded directory without touching outside files', () => {
    const sandbox = temporaryDirectory();
    const selectedDirectory = createDirectory(sandbox, 'selected');
    const outsideDirectory = createDirectory(sandbox, 'outside');
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 902,
      port: 3902,
    });
    const preparedDirectory = path.dirname(prepared.databasePath);
    const parkedDirectory =
      preparedDirectory === selectedDirectory
        ? path.join(sandbox, 'parked-selected')
        : path.join(selectedDirectory, 'parked-private');
    const outsideFiles = createDatabaseGroup(
      path.join(outsideDirectory, path.basename(prepared.databasePath)),
    );
    renameSync(preparedDirectory, parkedDirectory);
    symlinkSync(outsideDirectory, preparedDirectory, 'dir');

    expect(() => cleanupPreparedE2EDatabase(environment)).toThrow();
    expect(outsideFiles.every(existsSync)).toBe(true);
  });

  it('refuses a recorded inode mismatch without touching the database', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 903,
      port: 3903,
    });
    environment[directoryInodeEnvironmentVariable] = (
      BigInt(environment[directoryInodeEnvironmentVariable] ?? '0') + 1n
    ).toString();

    expect(() => cleanupPreparedE2EDatabase(environment)).toThrow();
    expect(existsSync(prepared.databasePath)).toBe(true);
  });

  it('preserves the entire group when the private directory has an unknown entry', () => {
    const selectedDirectory = temporaryDirectory();
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 904,
      port: 3904,
    });
    const unknown = path.join(path.dirname(prepared.databasePath), 'notes.txt');
    createRegularFile(unknown);

    expect(() => cleanupPreparedE2EDatabase(environment)).toThrow();
    expect(existsSync(prepared.databasePath)).toBe(true);
    expect(existsSync(unknown)).toBe(true);
  });

  it('preserves the entire group when a known sidecar is a symlink', () => {
    const sandbox = temporaryDirectory();
    const selectedDirectory = createDirectory(sandbox, 'selected');
    const victim = path.join(sandbox, 'victim');
    const environment: Record<string, string | undefined> = {};
    const prepared = prepareTestE2EDatabase({
      directory: selectedDirectory,
      environment,
      pid: 905,
      port: 3905,
    });
    createRegularFile(victim, 'victim');
    symlinkSync(victim, `${prepared.databasePath}-wal`);

    expect(() => cleanupPreparedE2EDatabase(environment)).toThrow();
    expect(existsSync(prepared.databasePath)).toBe(true);
    expect(lstatSync(`${prepared.databasePath}-wal`).isSymbolicLink()).toBe(true);
    expect(readFileSync(victim, 'utf8')).toBe('victim');
  });
});

describe('Playwright configuration integration', () => {
  it('reuses one environment-owned private database across config evaluation', async () => {
    const directory = temporaryDirectory();
    setEnvironment('COMMERCE_OPS_E2E_DATABASE_DIR', directory);
    setEnvironment('COMMERCE_OPS_E2E_PORT', '3906');
    setEnvironment(E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE, undefined);
    setEnvironment(E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE, undefined);
    setEnvironment(directoryDeviceEnvironmentVariable, undefined);
    setEnvironment(directoryInodeEnvironmentVariable, undefined);
    setEnvironment(directoryOwnerEnvironmentVariable, undefined);
    setEnvironment('PLAYWRIGHT_BASE_URL', undefined);

    const firstConfig = (await import('../playwright.config')).default;
    const preparedPath = process.env[E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE];
    const privateDirectory =
      process.env[E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE];
    expect(preparedPath).toBeTruthy();
    expect(privateDirectory).toBeTruthy();
    expect(path.dirname(preparedPath!)).toBe(privateDirectory);
    expect(path.dirname(privateDirectory!)).toBe(realpathSync(directory));
    expect(path.basename(privateDirectory!)).toMatch(
      /^commerce-ops-desk-playwright-\d+-3906-\d+-[A-Za-z0-9]{6}$/,
    );
    expect(lstatSync(preparedPath!).isFile()).toBe(true);
    expect(process.env[directoryDeviceEnvironmentVariable]).toBeTruthy();
    expect(process.env[directoryInodeEnvironmentVariable]).toBeTruthy();
    expect(process.env[directoryOwnerEnvironmentVariable]).toBeTruthy();
    expect(firstConfig.globalTeardown).toBe('./e2e/database-global-teardown.ts');
    expect(firstConfig.testIgnore).toBe('**/*.test.ts');
    const firstWebServer = firstConfig.webServer as {
      env?: Record<string, string>;
    };
    expect(firstWebServer.env?.COMMERCE_OPS_DATABASE_URL).toBe(
      `sqlite+pysqlite:///${preparedPath}`,
    );

    vi.resetModules();
    const repeatedConfig = (await import('../playwright.config')).default;
    const repeatedWebServer = repeatedConfig.webServer as {
      env?: Record<string, string>;
    };

    expect(process.env[E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE]).toBe(preparedPath);
    expect(repeatedWebServer.env?.COMMERCE_OPS_DATABASE_URL).toBe(
      `sqlite+pysqlite:///${preparedPath}`,
    );
    expect(existsSync(preparedPath!)).toBe(true);
  });
});
