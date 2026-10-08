import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  deliverWebhookEnvelope,
  requestWebhookEnvelope,
  WebhookApiError,
  type WebhookEnvelope,
} from './webhooks';

const envelope: WebhookEnvelope = {
  path: '/api/webhooks/synthetic/891a8728-df4b-4f54-b7f8-4afea330835c',
  body: '{"type":"payment.failed","occurred_at":"2026-10-07T12:00:00Z","data":{"order":{"id":"syn_order_A1B2C3D4","number":"DEMO-1045","amount_minor":12900,"currency":"USD"}}}',
  timestamp: '1791374400',
  event_id: 'evt_A1B2C3D4',
  signature: `v1=${'a'.repeat(64)}`,
};

const delivery = {
  status: 'processed' as const,
  event_id: envelope.event_id,
  case_id: 'c6cd4685-7848-4f9f-b4ed-8b0f3d5cb358',
  replayed: false,
};

function jsonResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('synthetic webhook API client', () => {
  it('requests a no-store signed envelope with the session cookie and CSRF token', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(envelope));
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestWebhookEnvelope('fresh', 'csrf-token')).resolves.toEqual(envelope);

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith('/api/demo/webhooks/envelope', {
      body: JSON.stringify({ scenario: 'fresh' }),
      cache: 'no-store',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': 'csrf-token',
      },
      method: 'POST',
    });
  });

  it('forwards a caller cancellation signal to signer and delivery requests', async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(envelope))
      .mockResolvedValueOnce(jsonResponse(delivery, 201));
    vi.stubGlobal('fetch', fetchMock);

    await requestWebhookEnvelope('fresh', 'csrf-token', controller.signal);
    await deliverWebhookEnvelope(envelope, controller.signal);

    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
    expect(fetchMock.mock.calls[1][1]?.signal).toBe(controller.signal);
  });

  it('delivers the exact body string without browser credentials', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(delivery, 201));
    vi.stubGlobal('fetch', fetchMock);

    await expect(deliverWebhookEnvelope(envelope)).resolves.toEqual(delivery);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [path, request] = fetchMock.mock.calls[0];
    expect(path).toBe(envelope.path);
    expect(request).toMatchObject({
      body: envelope.body,
      cache: 'no-store',
      credentials: 'omit',
      method: 'POST',
    });
    expect(request?.body).toBe(envelope.body);
    const headers = new Headers(request?.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('X-Webhook-Timestamp')).toBe(envelope.timestamp);
    expect(headers.get('X-Webhook-Event-Id')).toBe(envelope.event_id);
    expect(headers.get('X-Webhook-Signature')).toBe(envelope.signature);
    expect(headers.has('Cookie')).toBe(false);
    expect(headers.has('X-CSRF-Token')).toBe(false);
  });

  it.each([
    { ...envelope, extra: 'not-allowed' },
    { ...envelope, path: '/api/webhooks/synthetic/not-a-uuid' },
    { ...envelope, body: 'snowman: ☃' },
    { ...envelope, timestamp: '01791374400' },
    { ...envelope, event_id: 'evt_short' },
    { ...envelope, signature: 'v1=ABCDEF' },
  ])('rejects an invalid envelope success contract', async (invalidEnvelope) => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(invalidEnvelope)),
    );

    await expect(requestWebhookEnvelope('fresh', 'csrf-token')).rejects.toMatchObject({
      status: 502,
      code: 'webhook_response_invalid',
      retryAfter: null,
    });
  });

  it.each([
    { ...delivery, extra: 'not-allowed' },
    { ...delivery, status: 'accepted' },
    { ...delivery, event_id: 'evt_DIFFERENT1' },
    { ...delivery, case_id: 'not-a-uuid' },
    { ...delivery, replayed: 'false' },
  ])('rejects an invalid delivery success contract', async (invalidDelivery) => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(invalidDelivery, 201)),
    );

    await expect(deliverWebhookEnvelope(envelope)).rejects.toMatchObject({
      status: 502,
      code: 'webhook_response_invalid',
      retryAfter: null,
    });
  });

  it('requires the HTTP status to agree with the replay flag', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ ...delivery, replayed: true }, 201)),
    );

    await expect(deliverWebhookEnvelope(envelope)).rejects.toMatchObject({
      status: 502,
      code: 'webhook_response_invalid',
    });
  });

  it('retains only a safe error code, status, and canonical Retry-After value', async () => {
    const canary = 'RAW_BODY_OR_SIGNATURE_CANARY';
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(
          {
            detail: {
              code: 'webhook_ingress_rate_limited',
              message: canary,
            },
            raw_body: canary,
          },
          429,
          { 'Retry-After': '17' },
        ),
      ),
    );

    const error = await deliverWebhookEnvelope(envelope).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(WebhookApiError);
    expect(error).toMatchObject({
      status: 429,
      code: 'webhook_ingress_rate_limited',
      retryAfter: 17,
    });
    expect(error).not.toHaveProperty('body');
    expect(error).not.toHaveProperty('response');
    expect(JSON.stringify(error)).not.toContain(canary);
    expect(String(error)).not.toContain(canary);
  });

  it.each(['17.5', '-1', '+1', 'unknown'])(
    'discards a non-canonical Retry-After value: %s',
    async (retryAfter) => {
      vi.stubGlobal(
        'fetch',
        vi.fn<typeof fetch>().mockResolvedValue(
          jsonResponse(
            {
              detail: {
                code: 'webhook_service_unavailable',
                message: 'Temporarily unavailable.',
              },
            },
            503,
            { 'Retry-After': retryAfter },
          ),
        ),
      );

      await expect(deliverWebhookEnvelope(envelope)).rejects.toMatchObject({
        status: 503,
        code: 'webhook_service_unavailable',
        retryAfter: null,
      });
    },
  );

  it('normalizes a network failure without retaining the thrown object', async () => {
    const canary = 'NETWORK_CANARY';
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockRejectedValue(new TypeError(canary)),
    );

    const error = await deliverWebhookEnvelope(envelope).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(WebhookApiError);
    expect(error).toMatchObject({
      status: 0,
      code: 'webhook_network_error',
      retryAfter: null,
    });
    expect(JSON.stringify(error)).not.toContain(canary);
    expect(String(error)).not.toContain(canary);
  });
});
