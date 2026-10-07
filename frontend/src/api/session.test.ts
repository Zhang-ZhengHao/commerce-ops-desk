import { afterEach, describe, expect, it, vi } from 'vitest';

import { createCommandKey } from './session';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('demo command keys', () => {
  it('creates a UUID when randomUUID is unavailable outside a secure context', () => {
    const cryptoWithOptionalUuid = globalThis.crypto as unknown as {
      randomUUID?: Crypto['randomUUID'];
    };
    const ownDescriptor = Object.getOwnPropertyDescriptor(
      cryptoWithOptionalUuid,
      'randomUUID',
    );
    Object.defineProperty(cryptoWithOptionalUuid, 'randomUUID', {
      configurable: true,
      value: undefined,
    });

    try {
      expect(createCommandKey()).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
    } finally {
      if (ownDescriptor) {
        Object.defineProperty(cryptoWithOptionalUuid, 'randomUUID', ownDescriptor);
      } else {
        delete cryptoWithOptionalUuid.randomUUID;
      }
    }
  });
});
