import { cleanupPreparedE2EDatabase } from './database-lifecycle';

export default function globalTeardown(): void {
  cleanupPreparedE2EDatabase();
}
