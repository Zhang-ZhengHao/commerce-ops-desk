export type WebhookScenario = 'fresh' | 'stale';

export interface WebhookEnvelope {
  path: string;
  body: string;
  timestamp: string;
  event_id: string;
  signature: string;
}

export interface WebhookDeliveryResult {
  status: 'processed';
  event_id: string;
  case_id: string;
  replayed: boolean;
}

export class WebhookApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null,
  ) {
    super('The synthetic webhook request could not be completed.');
  }
}

const WEBHOOK_PATH =
  /^\/api\/webhooks\/synthetic\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const EVENT_ID = /^evt_[A-Za-z0-9]{8,64}$/;
const CASE_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TIMESTAMP = /^[1-9][0-9]{0,9}$/;
const SIGNATURE = /^v1=[0-9a-f]{64}$/;

const SAFE_ERROR_CODES = new Set([
  'demo_webhook_limit_reached',
  'request_body_too_large',
  'request_validation_failed',
  'webhook_authentication_failed',
  'webhook_event_conflict',
  'webhook_ingress_rate_limited',
  'webhook_media_type_unsupported',
  'webhook_payload_invalid',
  'webhook_service_unavailable',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length && expected.every((key) => key in value);
}

function isAscii(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) > 0x7f) return false;
  }
  return value.length > 0;
}

function isWebhookEnvelope(value: unknown): value is WebhookEnvelope {
  if (!isRecord(value)) return false;
  if (!hasExactKeys(value, ['path', 'body', 'timestamp', 'event_id', 'signature'])) {
    return false;
  }
  return (
    typeof value.path === 'string' &&
    WEBHOOK_PATH.test(value.path) &&
    typeof value.body === 'string' &&
    isAscii(value.body) &&
    typeof value.timestamp === 'string' &&
    TIMESTAMP.test(value.timestamp) &&
    typeof value.event_id === 'string' &&
    EVENT_ID.test(value.event_id) &&
    typeof value.signature === 'string' &&
    SIGNATURE.test(value.signature)
  );
}

function isWebhookDeliveryResult(
  value: unknown,
  envelope: WebhookEnvelope,
  responseStatus: number,
): value is WebhookDeliveryResult {
  if (!isRecord(value)) return false;
  if (!hasExactKeys(value, ['status', 'event_id', 'case_id', 'replayed'])) {
    return false;
  }
  if (
    value.status !== 'processed' ||
    typeof value.event_id !== 'string' ||
    !EVENT_ID.test(value.event_id) ||
    value.event_id !== envelope.event_id ||
    typeof value.case_id !== 'string' ||
    !CASE_ID.test(value.case_id) ||
    typeof value.replayed !== 'boolean'
  ) {
    return false;
  }
  return value.replayed ? responseStatus === 200 : responseStatus === 201;
}

function retryAfterFrom(response: Response): number | null {
  const value = response.headers.get('Retry-After');
  if (value === null || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) ? seconds : null;
}

async function errorFrom(response: Response): Promise<WebhookApiError> {
  let code = 'webhook_request_failed';
  try {
    const body: unknown = await response.json();
    if (
      isRecord(body) &&
      isRecord(body.detail) &&
      typeof body.detail.code === 'string' &&
      SAFE_ERROR_CODES.has(body.detail.code)
    ) {
      code = body.detail.code;
    }
  } catch {
    // Keep a stable local code; never retain an unreadable upstream response.
  }
  return new WebhookApiError(response.status, code, retryAfterFrom(response));
}

async function safeFetch(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch {
    throw new WebhookApiError(0, 'webhook_network_error', null);
  }
}

async function responseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new WebhookApiError(502, 'webhook_response_invalid', null);
  }
}

export async function requestWebhookEnvelope(
  scenario: WebhookScenario,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<WebhookEnvelope> {
  const response = await safeFetch('/api/demo/webhooks/envelope', {
    body: JSON.stringify({ scenario }),
    cache: 'no-store',
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrfToken,
    },
    method: 'POST',
    signal,
  });
  if (!response.ok) throw await errorFrom(response);
  if (response.status !== 200) {
    throw new WebhookApiError(502, 'webhook_response_invalid', null);
  }
  const body = await responseJson(response);
  if (!isWebhookEnvelope(body)) {
    throw new WebhookApiError(502, 'webhook_response_invalid', null);
  }
  return body;
}

export async function deliverWebhookEnvelope(
  envelope: WebhookEnvelope,
  signal?: AbortSignal,
): Promise<WebhookDeliveryResult> {
  const response = await safeFetch(envelope.path, {
    body: envelope.body,
    cache: 'no-store',
    credentials: 'omit',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Webhook-Event-Id': envelope.event_id,
      'X-Webhook-Signature': envelope.signature,
      'X-Webhook-Timestamp': envelope.timestamp,
    },
    method: 'POST',
    signal,
  });
  if (!response.ok) throw await errorFrom(response);
  const body = await responseJson(response);
  if (!isWebhookDeliveryResult(body, envelope, response.status)) {
    throw new WebhookApiError(502, 'webhook_response_invalid', null);
  }
  return body;
}
