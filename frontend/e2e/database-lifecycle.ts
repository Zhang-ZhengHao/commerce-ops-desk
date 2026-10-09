import { randomBytes } from 'node:crypto';
import {
  chmodSync,
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmdirSync,
  statSync,
  unlinkSync,
  type BigIntStats,
} from 'node:fs';
import path from 'node:path';

const runDirectoryPattern =
  /^commerce-ops-desk-playwright-([1-9]\d*)-([1-9]\d*)-(unknown|0|[1-9]\d{0,19})-([A-Za-z0-9]{6,64})$/;
const processStartTimePattern = /^(?:0|[1-9]\d{0,19})$/;
const databaseFilename = 'commerce-ops-desk.sqlite3';
const databaseFilenames = new Set([
  databaseFilename,
  `${databaseFilename}-wal`,
  `${databaseFilename}-shm`,
]);
const privateDirectoryMode = 0o700n;
const privateFileMode = 0o600n;

export type ProcessLiveness = 'alive' | 'dead' | 'unknown';
export type ProcessLivenessProbe = (pid: number) => ProcessLiveness;
export type ProcessStartTimeProbe = (pid: number) => string | undefined;
type SignalProcess = (pid: number) => void;
type ReadProcessStat = (pid: number) => string;
type Environment = Record<string, string | undefined>;

export const E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE =
  'COMMERCE_OPS_E2E_DATABASE_PATH';
export const E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE =
  'COMMERCE_OPS_E2E_DATABASE_EXPECTED_DIRECTORY';
export const E2E_DATABASE_DIRECTORY_DEVICE_ENVIRONMENT_VARIABLE =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_DEVICE';
export const E2E_DATABASE_DIRECTORY_INODE_ENVIRONMENT_VARIABLE =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_INODE';
export const E2E_DATABASE_DIRECTORY_OWNER_ENVIRONMENT_VARIABLE =
  'COMMERCE_OPS_E2E_DATABASE_DIRECTORY_OWNER_UID';

const lifecycleEnvironmentVariables = [
  E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE,
  E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE,
  E2E_DATABASE_DIRECTORY_DEVICE_ENVIRONMENT_VARIABLE,
  E2E_DATABASE_DIRECTORY_INODE_ENVIRONMENT_VARIABLE,
  E2E_DATABASE_DIRECTORY_OWNER_ENVIRONMENT_VARIABLE,
] as const;

export interface PreparedE2EDatabase {
  databasePath: string;
  ownsLifecycle: boolean;
}

export interface PrepareE2EDatabaseOptions {
  directory: string;
  environment?: Environment;
  liveness?: ProcessLivenessProbe;
  pid: number;
  port: number;
  processStartTime?: ProcessStartTimeProbe;
}

interface RunDirectoryIdentity {
  pid: number;
  port: number;
  startTimeTicks?: string;
}

interface FileSnapshot {
  filename: string;
  metadata: BigIntStats;
}

interface ValidatedGroup {
  directoryMetadata: BigIntStats;
  files: FileSnapshot[];
}

interface RecordedE2EDatabase {
  databasePath: string;
  directory: string;
  directoryMetadata: BigIntStats;
  identity: RunDirectoryIdentity;
  ownerUid: bigint;
}

function permissions(metadata: BigIntStats): bigint {
  return metadata.mode & 0o777n;
}

function sameIdentity(left: BigIntStats, right: BigIntStats): boolean {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.uid === right.uid &&
    left.mode === right.mode
  );
}

function currentOwnerUid(fallback: bigint): bigint {
  return typeof process.getuid === 'function' ? BigInt(process.getuid()) : fallback;
}

function parseRunDirectoryName(filename: string): RunDirectoryIdentity | undefined {
  const match = runDirectoryPattern.exec(filename);
  if (!match) return undefined;

  const pid = Number(match[1]);
  const port = Number(match[2]);
  if (
    !Number.isSafeInteger(pid) ||
    pid < 1 ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65_535
  ) {
    return undefined;
  }
  return {
    pid,
    port,
    startTimeTicks: match[3] === 'unknown' ? undefined : match[3],
  };
}

function validatePidAndPort(pid: number, port: number): void {
  if (!Number.isSafeInteger(pid) || pid < 1) {
    throw new Error(`The E2E database PID must be a positive safe integer: ${pid}`);
  }
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`The E2E database port must be an integer from 1 to 65535: ${port}`);
  }
}

function canonicalBaseDirectory(directory: string): string {
  if (!path.isAbsolute(directory)) {
    throw new Error(`The E2E database directory must be absolute: ${directory}`);
  }
  const canonicalDirectory = realpathSync(directory);
  const metadata = lstatSync(canonicalDirectory, { bigint: true });
  if (!metadata.isDirectory()) {
    throw new Error(`The E2E database directory must be a directory: ${directory}`);
  }
  return canonicalDirectory;
}

function unsignedEnvironmentInteger(environment: Environment, name: string): bigint {
  const value = environment[name]?.trim();
  if (!value || !/^(?:0|[1-9]\d*)$/.test(value)) {
    throw new Error(`${name} must be an unsigned integer.`);
  }
  return BigInt(value);
}

function validatePrivateDirectory(
  directory: string,
  expectedMetadata: BigIntStats,
  ownerUid: bigint,
): BigIntStats | undefined {
  try {
    const currentMetadata = lstatSync(directory, { bigint: true });
    if (
      !currentMetadata.isDirectory() ||
      currentMetadata.uid !== ownerUid ||
      permissions(currentMetadata) !== privateDirectoryMode ||
      !sameIdentity(currentMetadata, expectedMetadata) ||
      realpathSync(directory) !== directory
    ) {
      return undefined;
    }
    return currentMetadata;
  } catch {
    return undefined;
  }
}

function validateAnchoredDirectory(
  directoryReference: string,
  expectedMetadata: BigIntStats,
  ownerUid: bigint,
): BigIntStats | undefined {
  try {
    const currentMetadata = statSync(directoryReference, { bigint: true });
    if (
      !currentMetadata.isDirectory() ||
      currentMetadata.uid !== ownerUid ||
      permissions(currentMetadata) !== privateDirectoryMode ||
      !sameIdentity(currentMetadata, expectedMetadata)
    ) {
      return undefined;
    }
    return currentMetadata;
  } catch {
    return undefined;
  }
}

function validateDatabaseGroup(
  directory: string,
  expectedDirectoryMetadata: BigIntStats,
  ownerUid: bigint,
  requireDatabase: boolean,
  anchored = false,
): ValidatedGroup | undefined {
  const validateDirectory = anchored
    ? validateAnchoredDirectory
    : validatePrivateDirectory;
  if (!validateDirectory(directory, expectedDirectoryMetadata, ownerUid)) {
    return undefined;
  }

  let entries: string[];
  try {
    entries = readdirSync(directory);
  } catch {
    return undefined;
  }
  if (
    entries.some((entry) => !databaseFilenames.has(entry)) ||
    (requireDatabase && !entries.includes(databaseFilename))
  ) {
    return undefined;
  }

  const files: FileSnapshot[] = [];
  for (const entry of entries) {
    const filename = path.join(directory, entry);
    try {
      const metadata = lstatSync(filename, { bigint: true });
      if (
        !metadata.isFile() ||
        metadata.uid !== ownerUid ||
        permissions(metadata) !== privateFileMode
      ) {
        return undefined;
      }
      files.push({ filename, metadata });
    } catch {
      return undefined;
    }
  }

  const directoryMetadata = validateDirectory(
    directory,
    expectedDirectoryMetadata,
    ownerUid,
  );
  if (!directoryMetadata) return undefined;
  for (const file of files) {
    try {
      const currentMetadata = lstatSync(file.filename, { bigint: true });
      if (!currentMetadata.isFile() || !sameIdentity(currentMetadata, file.metadata)) {
        return undefined;
      }
    } catch {
      return undefined;
    }
  }
  return { directoryMetadata, files };
}

function removeValidatedGroup(
  directory: string,
  group: ValidatedGroup,
  ownerUid: bigint,
  anchored = false,
  directoryRemovalPath = directory,
): boolean {
  const validateDirectory = anchored
    ? validateAnchoredDirectory
    : validatePrivateDirectory;
  if (!validateDirectory(directory, group.directoryMetadata, ownerUid)) {
    return false;
  }
  const expectedEntries = new Set(
    group.files.map((file) => path.basename(file.filename)),
  );
  for (const file of group.files) {
    try {
      const currentMetadata = lstatSync(file.filename, { bigint: true });
      if (!currentMetadata.isFile() || !sameIdentity(currentMetadata, file.metadata)) {
        return false;
      }
    } catch {
      return false;
    }
  }
  try {
    const currentEntries = readdirSync(directory);
    if (
      currentEntries.length !== expectedEntries.size ||
      currentEntries.some((entry) => !expectedEntries.has(entry))
    ) {
      return false;
    }
  } catch {
    return false;
  }

  try {
    const claimedFiles: FileSnapshot[] = [];
    for (const file of group.files) {
      // A fresh name turns any pre-rename path swap into an identity mismatch.
      const claimedFilename = path.join(
        directory,
        `.commerce-ops-desk-cleanup-${randomBytes(16).toString('hex')}`,
      );
      renameSync(file.filename, claimedFilename);
      const claimedMetadata = lstatSync(claimedFilename, { bigint: true });
      if (
        !claimedMetadata.isFile() ||
        !sameIdentity(claimedMetadata, file.metadata)
      ) {
        return false;
      }
      claimedFiles.push({ filename: claimedFilename, metadata: claimedMetadata });
    }
    const claimedEntries = new Set(
      claimedFiles.map((file) => path.basename(file.filename)),
    );
    const currentEntries = readdirSync(directory);
    if (
      currentEntries.length !== claimedEntries.size ||
      currentEntries.some((entry) => !claimedEntries.has(entry))
    ) {
      return false;
    }
    for (const file of claimedFiles) {
      const currentMetadata = lstatSync(file.filename, { bigint: true });
      if (!currentMetadata.isFile() || !sameIdentity(currentMetadata, file.metadata)) {
        return false;
      }
    }
    for (const file of claimedFiles) unlinkSync(file.filename);
    if (!validateDirectory(directory, group.directoryMetadata, ownerUid)) {
      return false;
    }
    if (readdirSync(directory).length !== 0) return false;
    if (
      directoryRemovalPath !== directory &&
      !validatePrivateDirectory(
        directoryRemovalPath,
        group.directoryMetadata,
        ownerUid,
      )
    ) {
      return false;
    }
    rmdirSync(directoryRemovalPath);
    return true;
  } catch {
    return false;
  }
}

function recordedE2EDatabase(environment: Environment): RecordedE2EDatabase {
  const databasePath = environment[E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE]?.trim();
  const directory =
    environment[E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE]?.trim();
  if (!databasePath || !directory) {
    throw new Error('The complete E2E database directory identity is required.');
  }
  if (
    !path.isAbsolute(databasePath) ||
    !path.isAbsolute(directory) ||
    path.resolve(directory) !== directory ||
    path.dirname(databasePath) !== directory ||
    path.basename(databasePath) !== databaseFilename
  ) {
    throw new Error(`Refusing an unsafe E2E database path: ${databasePath}`);
  }

  const identity = parseRunDirectoryName(path.basename(directory));
  if (!identity) {
    throw new Error(`Refusing an unsafe E2E run directory: ${directory}`);
  }
  const recordedDevice = unsignedEnvironmentInteger(
    environment,
    E2E_DATABASE_DIRECTORY_DEVICE_ENVIRONMENT_VARIABLE,
  );
  const recordedInode = unsignedEnvironmentInteger(
    environment,
    E2E_DATABASE_DIRECTORY_INODE_ENVIRONMENT_VARIABLE,
  );
  const ownerUid = unsignedEnvironmentInteger(
    environment,
    E2E_DATABASE_DIRECTORY_OWNER_ENVIRONMENT_VARIABLE,
  );

  let directoryMetadata: BigIntStats;
  try {
    directoryMetadata = lstatSync(directory, { bigint: true });
  } catch {
    throw new Error(`The recorded E2E run directory is unavailable: ${directory}`);
  }
  if (
    directoryMetadata.dev !== recordedDevice ||
    directoryMetadata.ino !== recordedInode ||
    directoryMetadata.uid !== ownerUid ||
    ownerUid !== currentOwnerUid(directoryMetadata.uid) ||
    !validatePrivateDirectory(directory, directoryMetadata, ownerUid)
  ) {
    throw new Error(`The recorded E2E run directory identity changed: ${directory}`);
  }

  let databaseMetadata: BigIntStats;
  try {
    databaseMetadata = lstatSync(databasePath, { bigint: true });
  } catch {
    throw new Error(`The recorded E2E database is unavailable: ${databasePath}`);
  }
  if (
    !databaseMetadata.isFile() ||
    databaseMetadata.uid !== ownerUid ||
    permissions(databaseMetadata) !== privateFileMode
  ) {
    throw new Error(`The recorded E2E database identity changed: ${databasePath}`);
  }

  return { databasePath, directory, directoryMetadata, identity, ownerUid };
}

function createPrivateDatabase(
  baseDirectory: string,
  pid: number,
  port: number,
  processStartTime: ProcessStartTimeProbe,
): RecordedE2EDatabase {
  const detectedStartTime = processStartTime(pid);
  if (
    detectedStartTime !== undefined &&
    !processStartTimePattern.test(detectedStartTime)
  ) {
    throw new Error(
      `The E2E database process start time must be canonical clock ticks: ${detectedStartTime}`,
    );
  }
  if (process.platform === 'linux' && detectedStartTime === undefined) {
    throw new Error(`Unable to bind the E2E database to Linux process ${pid}.`);
  }
  const startTimeComponent = detectedStartTime ?? 'unknown';
  const prefix = path.join(
    baseDirectory,
    `commerce-ops-desk-playwright-${pid}-${port}-${startTimeComponent}-`,
  );
  const directory = mkdtempSync(prefix);
  chmodSync(directory, Number(privateDirectoryMode));
  const canonicalDirectory = realpathSync(directory);
  const directoryMetadata = lstatSync(canonicalDirectory, { bigint: true });
  const ownerUid = currentOwnerUid(directoryMetadata.uid);
  const identity = parseRunDirectoryName(path.basename(canonicalDirectory));
  if (
    canonicalDirectory !== directory ||
    !identity ||
    identity.startTimeTicks !== detectedStartTime ||
    !directoryMetadata.isDirectory() ||
    directoryMetadata.uid !== ownerUid ||
    permissions(directoryMetadata) !== privateDirectoryMode
  ) {
    throw new Error(`Unable to establish a private E2E run directory: ${directory}`);
  }

  const databasePath = path.join(canonicalDirectory, databaseFilename);
  const openFlags =
    constants.O_CREAT |
    constants.O_EXCL |
    constants.O_WRONLY |
    constants.O_NOFOLLOW;
  const descriptor = openSync(databasePath, openFlags, Number(privateFileMode));
  closeSync(descriptor);
  chmodSync(databasePath, Number(privateFileMode));
  const databaseMetadata = lstatSync(databasePath, { bigint: true });
  if (
    !databaseMetadata.isFile() ||
    databaseMetadata.uid !== ownerUid ||
    permissions(databaseMetadata) !== privateFileMode
  ) {
    throw new Error(`Unable to establish a private E2E database: ${databasePath}`);
  }
  return { databasePath, directory, directoryMetadata, identity, ownerUid };
}

function recordE2EDatabase(
  environment: Environment,
  database: RecordedE2EDatabase,
): void {
  environment[E2E_DATABASE_PATH_ENVIRONMENT_VARIABLE] = database.databasePath;
  environment[E2E_DATABASE_EXPECTED_DIRECTORY_ENVIRONMENT_VARIABLE] =
    database.directory;
  environment[E2E_DATABASE_DIRECTORY_DEVICE_ENVIRONMENT_VARIABLE] =
    database.directoryMetadata.dev.toString();
  environment[E2E_DATABASE_DIRECTORY_INODE_ENVIRONMENT_VARIABLE] =
    database.directoryMetadata.ino.toString();
  environment[E2E_DATABASE_DIRECTORY_OWNER_ENVIRONMENT_VARIABLE] =
    database.ownerUid.toString();
}

export function detectProcessStartTime(
  pid: number,
  readProcessStat: ReadProcessStat = (candidate) =>
    readFileSync(`/proc/${candidate}/stat`, 'utf8'),
): string | undefined {
  if (process.platform !== 'linux' || !Number.isSafeInteger(pid) || pid < 1) {
    return undefined;
  }

  let stat: string;
  try {
    stat = readProcessStat(pid).trim();
  } catch {
    return undefined;
  }

  const commandEnd = stat.lastIndexOf(')');
  if (!stat.startsWith(`${pid} (`) || commandEnd < `${pid} (`.length) {
    return undefined;
  }
  // starttime is field 22; field 3 is the first token after the parenthesized comm.
  const fieldsAfterCommand = stat.slice(commandEnd + 1).trim().split(/\s+/);
  const startTimeTicks = fieldsAfterCommand[19];
  return startTimeTicks && processStartTimePattern.test(startTimeTicks)
    ? startTimeTicks
    : undefined;
}

export function detectProcessLiveness(
  pid: number,
  signalProcess: SignalProcess = (candidate) => process.kill(candidate, 0),
): ProcessLiveness {
  try {
    signalProcess(pid);
    return 'alive';
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'ESRCH' ? 'dead' : 'unknown';
  }
}

function isStaleProcessInstance(
  identity: RunDirectoryIdentity,
  liveness: ProcessLivenessProbe,
  processStartTime: ProcessStartTimeProbe,
): boolean {
  const startTimeBeforeSignal = processStartTime(identity.pid);
  const processState = liveness(identity.pid);
  const startTimeAfterSignal = processStartTime(identity.pid);

  if (processState === 'dead') {
    return startTimeAfterSignal === undefined;
  }
  return (
    processState === 'alive' &&
    identity.startTimeTicks !== undefined &&
    startTimeBeforeSignal !== undefined &&
    startTimeBeforeSignal === startTimeAfterSignal &&
    startTimeAfterSignal !== identity.startTimeTicks
  );
}

function claimRunDirectory(
  baseDirectory: string,
  candidate: string,
  identity: RunDirectoryIdentity,
  expectedMetadata: BigIntStats,
  ownerUid: bigint,
): string | undefined {
  const startTimeComponent = identity.startTimeTicks ?? 'unknown';
  const claimToken = randomBytes(16).toString('hex');
  const claimedDirectory = path.join(
    baseDirectory,
    `commerce-ops-desk-playwright-${identity.pid}-${identity.port}-${startTimeComponent}-${claimToken}`,
  );

  try {
    renameSync(candidate, claimedDirectory);
  } catch {
    return undefined;
  }
  return validatePrivateDirectory(
    claimedDirectory,
    expectedMetadata,
    ownerUid,
  )
    ? claimedDirectory
    : undefined;
}

interface OpenedRunDirectory {
  anchored: boolean;
  descriptor: number;
  reference: string;
}

function openRunDirectory(
  directory: string,
  expectedMetadata: BigIntStats,
  ownerUid: bigint,
): OpenedRunDirectory | undefined {
  let descriptor: number;
  try {
    descriptor = openSync(
      directory,
      constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
    );
  } catch {
    return undefined;
  }

  try {
    const descriptorMetadata = fstatSync(descriptor, { bigint: true });
    if (
      !descriptorMetadata.isDirectory() ||
      !sameIdentity(descriptorMetadata, expectedMetadata) ||
      descriptorMetadata.uid !== ownerUid ||
      permissions(descriptorMetadata) !== privateDirectoryMode
    ) {
      closeSync(descriptor);
      return undefined;
    }

    if (process.platform === 'linux') {
      // Node has no openat/unlinkat API. Keep child operations anchored to the
      // verified directory descriptor through procfs instead of its mutable name.
      const reference = `/proc/self/fd/${descriptor}`;
      if (!validateAnchoredDirectory(reference, expectedMetadata, ownerUid)) {
        closeSync(descriptor);
        return undefined;
      }
      return { anchored: true, descriptor, reference };
    }
    return { anchored: false, descriptor, reference: directory };
  } catch {
    closeSync(descriptor);
    return undefined;
  }
}

export function cleanupStaleE2EDatabases(
  directory: string,
  liveness: ProcessLivenessProbe = detectProcessLiveness,
  processStartTime: ProcessStartTimeProbe = detectProcessStartTime,
): void {
  const baseDirectory = canonicalBaseDirectory(directory);
  const baseMetadata = lstatSync(baseDirectory, { bigint: true });
  const ownerUid = currentOwnerUid(baseMetadata.uid);

  for (const entry of readdirSync(baseDirectory)) {
    const identity = parseRunDirectoryName(entry);
    if (!identity) continue;
    const candidate = path.join(baseDirectory, entry);

    try {
      const directoryMetadata = lstatSync(candidate, { bigint: true });
      if (
        !directoryMetadata.isDirectory() ||
        directoryMetadata.uid !== ownerUid ||
        permissions(directoryMetadata) !== privateDirectoryMode
      ) {
        continue;
      }

      if (
        !isStaleProcessInstance(identity, liveness, processStartTime) ||
        !validateDatabaseGroup(
          candidate,
          directoryMetadata,
          ownerUid,
          false,
        )
      ) {
        continue;
      }

      const claimedDirectory = claimRunDirectory(
        baseDirectory,
        candidate,
        identity,
        directoryMetadata,
        ownerUid,
      );
      if (!claimedDirectory) continue;
      const openedDirectory = openRunDirectory(
        claimedDirectory,
        directoryMetadata,
        ownerUid,
      );
      if (!openedDirectory) continue;

      try {
        if (!isStaleProcessInstance(identity, liveness, processStartTime)) {
          continue;
        }
        const group = validateDatabaseGroup(
          openedDirectory.reference,
          directoryMetadata,
          ownerUid,
          false,
          openedDirectory.anchored,
        );
        if (group) {
          removeValidatedGroup(
            openedDirectory.reference,
            group,
            ownerUid,
            openedDirectory.anchored,
            claimedDirectory,
          );
        }
      } finally {
        closeSync(openedDirectory.descriptor);
      }
    } catch {
      // Anything that cannot be proven safe stays in place.
    }
  }
}

export function prepareE2EDatabase({
  directory,
  environment = process.env,
  liveness = detectProcessLiveness,
  pid,
  port,
  processStartTime = detectProcessStartTime,
}: PrepareE2EDatabaseOptions): PreparedE2EDatabase {
  validatePidAndPort(pid, port);
  const baseDirectory = canonicalBaseDirectory(directory);
  const hasRecordedLifecycle = lifecycleEnvironmentVariables.some(
    (name) => environment[name] !== undefined,
  );

  if (hasRecordedLifecycle) {
    const recordedDatabase = recordedE2EDatabase(environment);
    if (
      path.dirname(recordedDatabase.directory) !== baseDirectory ||
      recordedDatabase.identity.port !== port
    ) {
      throw new Error(
        `Refusing to reuse an E2E database outside ${baseDirectory}: ` +
          recordedDatabase.databasePath,
      );
    }
    return { databasePath: recordedDatabase.databasePath, ownsLifecycle: false };
  }

  cleanupStaleE2EDatabases(baseDirectory, liveness, processStartTime);
  const database = createPrivateDatabase(baseDirectory, pid, port, processStartTime);
  recordE2EDatabase(environment, database);
  return { databasePath: database.databasePath, ownsLifecycle: true };
}

export function cleanupPreparedE2EDatabase(
  environment: Environment = process.env,
): void {
  const database = recordedE2EDatabase(environment);
  const refuseCleanup = (): never => {
    throw new Error(
      `Refusing to clean an E2E database whose identity changed: ${database.databasePath}`,
    );
  };
  if (
    !validateDatabaseGroup(
      database.directory,
      database.directoryMetadata,
      database.ownerUid,
      true,
    )
  ) {
    refuseCleanup();
  }

  const claimedDirectory =
    claimRunDirectory(
      path.dirname(database.directory),
      database.directory,
      database.identity,
      database.directoryMetadata,
      database.ownerUid,
    ) ?? refuseCleanup();
  const openedDirectory =
    openRunDirectory(
      claimedDirectory,
      database.directoryMetadata,
      database.ownerUid,
    ) ?? refuseCleanup();

  try {
    const group = validateDatabaseGroup(
      openedDirectory.reference,
      database.directoryMetadata,
      database.ownerUid,
      true,
      openedDirectory.anchored,
    );
    if (
      !group ||
      !removeValidatedGroup(
        openedDirectory.reference,
        group,
        database.ownerUid,
        openedDirectory.anchored,
        claimedDirectory,
      )
    ) {
      refuseCleanup();
    }
  } finally {
    closeSync(openedDirectory.descriptor);
  }
}
