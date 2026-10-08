import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WebhookDeliveryResult, WebhookEnvelope } from '../../api/webhooks';
import { SyntheticProviderPanel } from './SyntheticProviderPanel';

const envelope: WebhookEnvelope = {
  path: '/api/webhooks/synthetic/891a8728-df4b-4f54-b7f8-4afea330835c',
  body: '{"type":"payment.failed","occurred_at":"2026-10-07T12:00:00Z","data":{"order":{"id":"syn_order_A1B2C3D4","number":"DEMO-1045","amount_minor":12900,"currency":"USD"}}}',
  timestamp: '1791374400',
  event_id: 'evt_A1B2C3D4',
  signature: `v1=${'a'.repeat(64)}`,
};

const staleEnvelope: WebhookEnvelope = {
  ...envelope,
  body: envelope.body.replace('A1B2C3D4', 'E5F6G7H8'),
  timestamp: '1791374099',
  event_id: 'evt_E5F6G7H8',
  signature: `v1=${'b'.repeat(64)}`,
};

const delivery: WebhookDeliveryResult = {
  status: 'processed',
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

function errorResponse(code: string, status: number, retryAfter?: number): Response {
  return jsonResponse(
    { detail: { code, message: 'Safe server message.' } },
    status,
    retryAfter === undefined ? undefined : { 'Retry-After': String(retryAfter) },
  );
}

function installFetch(...responses: Array<Response | Error>) {
  let index = 0;
  const fetchMock = vi.fn<typeof fetch>(async () => {
    const response = responses[index++];
    if (response instanceof Error) throw response;
    if (!response) throw new Error('Missing synthetic webhook test response.');
    return response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderPanel(
  onCommitted: (result: WebhookDeliveryResult) => Promise<void> = vi
    .fn()
    .mockResolvedValue(undefined),
  disabled = false,
) {
  return render(
    <SyntheticProviderPanel
      csrfToken="manager-csrf-token"
      disabled={disabled}
      onCommitted={onCommitted}
    />,
  );
}

function callsTo(fetchMock: ReturnType<typeof installFetch>, path: string) {
  return fetchMock.mock.calls.filter(([input]) => String(input) === path);
}

function ingressCalls(fetchMock: ReturnType<typeof installFetch>) {
  return fetchMock.mock.calls.filter(([input]) =>
    String(input).startsWith('/api/webhooks/synthetic/'),
  );
}

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('SyntheticProviderPanel', () => {
  it('starts with an explicit synthetic boundary and only enables scenarios with prerequisites', () => {
    renderPanel();

    const panel = screen.getByRole('region', { name: /synthetic provider/i });
    expect(within(panel).getByText(/no real store or payment processor is connected/i)).toBeVisible();
    expect(
      within(panel).getByRole('button', { name: /deliver new failure/i }),
    ).toBeEnabled();
    expect(
      within(panel).getByRole('button', { name: /replay same event/i }),
    ).toBeDisabled();
    expect(
      within(panel).getByRole('button', { name: /tamper after signing/i }),
    ).toBeDisabled();
    expect(
      within(panel).getByRole('button', { name: /send stale signature/i }),
    ).toBeEnabled();
  });

  it('disables every scenario while the surrounding session is changing', () => {
    renderPanel(undefined, true);

    expect(screen.getByRole('button', { name: /deliver new failure/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /replay same event/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /tamper after signing/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /send stale signature/i })).toBeDisabled();
  });

  it('delivers a fresh event, refreshes its committed case, and exposes no envelope material', async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn().mockResolvedValue(undefined);
    const fetchMock = installFetch(
      jsonResponse(envelope),
      jsonResponse(delivery, 201),
    );
    const beforeUrl = window.location.href;
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    const feedback = await screen.findByText(/new payment failure was committed/i);
    expect(feedback).toBeVisible();
    expect(screen.getByText(envelope.event_id)).toBeVisible();
    expect(screen.getByText('payment.failed')).toBeVisible();
    expect(screen.getByText(/new business effect/i)).toBeVisible();
    expect(onCommitted).toHaveBeenCalledOnce();
    expect(onCommitted).toHaveBeenCalledWith(delivery);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: /replay same event/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /tamper after signing/i })).toBeEnabled();

    const renderedText = document.body.textContent ?? '';
    expect(renderedText).not.toContain(envelope.path);
    expect(renderedText).not.toContain(envelope.body);
    expect(renderedText).not.toContain(envelope.timestamp);
    expect(renderedText).not.toContain(envelope.signature);
    expect(window.localStorage).toHaveLength(0);
    expect(window.sessionStorage).toHaveLength(0);
    expect(window.location.href).toBe(beforeUrl);
    expect(logSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it('replays the cached envelope byte-for-byte without asking the signer again', async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn().mockResolvedValue(undefined);
    const fetchMock = installFetch(
      jsonResponse(envelope),
      jsonResponse(delivery, 201),
      jsonResponse({ ...delivery, replayed: true }, 200),
    );

    renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    await screen.findByText(/new payment failure was committed/i);
    await user.click(screen.getByRole('button', { name: /replay same event/i }));

    expect(await screen.findByText(/exact replay returned the same case/i)).toBeVisible();
    expect(screen.getByText(/no duplicate business effect/i)).toBeVisible();
    expect(callsTo(fetchMock, '/api/demo/webhooks/envelope')).toHaveLength(1);
    const deliveries = ingressCalls(fetchMock);
    expect(deliveries).toHaveLength(2);
    expect(deliveries[1][1]?.body).toBe(envelope.body);
    expect(deliveries[1][1]?.headers).toEqual(deliveries[0][1]?.headers);
    expect(onCommitted).toHaveBeenLastCalledWith({ ...delivery, replayed: true });
  });

  it('changes exactly one ASCII byte for tampering without mutating the replay cache', async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn().mockResolvedValue(undefined);
    const fetchMock = installFetch(
      jsonResponse(envelope),
      jsonResponse(delivery, 201),
      errorResponse('webhook_authentication_failed', 401),
      jsonResponse({ ...delivery, replayed: true }, 200),
    );

    renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    await screen.findByText(/new payment failure was committed/i);
    await user.click(screen.getByRole('button', { name: /tamper after signing/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/authentication rejected/i);
    const tamperedBody = String(ingressCalls(fetchMock)[1][1]?.body);
    expect(tamperedBody).toHaveLength(envelope.body.length);
    expect(
      [...tamperedBody].filter((character, index) => character !== envelope.body[index]),
    ).toHaveLength(1);
    expect([...tamperedBody].every((character) => character.charCodeAt(0) <= 0x7f)).toBe(true);

    await user.click(screen.getByRole('button', { name: /replay same event/i }));
    expect(await screen.findByText(/exact replay returned the same case/i)).toBeVisible();
    expect(ingressCalls(fetchMock)[2][1]?.body).toBe(envelope.body);
    expect(callsTo(fetchMock, '/api/demo/webhooks/envelope')).toHaveLength(1);
  });

  it('uses a temporary stale envelope without replacing the fresh replay cache', async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn().mockResolvedValue(undefined);
    const fetchMock = installFetch(
      jsonResponse(envelope),
      jsonResponse(delivery, 201),
      jsonResponse(staleEnvelope),
      errorResponse('webhook_authentication_failed', 401),
      jsonResponse({ ...delivery, replayed: true }, 200),
    );

    renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    await screen.findByText(/new payment failure was committed/i);
    await user.click(screen.getByRole('button', { name: /send stale signature/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/authentication rejected/i);
    await user.click(screen.getByRole('button', { name: /replay same event/i }));

    expect(await screen.findByText(/exact replay returned the same case/i)).toBeVisible();
    const signerCalls = callsTo(fetchMock, '/api/demo/webhooks/envelope');
    expect(signerCalls).toHaveLength(2);
    expect(signerCalls.map(([, init]) => init?.body)).toEqual([
      JSON.stringify({ scenario: 'fresh' }),
      JSON.stringify({ scenario: 'stale' }),
    ]);
    expect(ingressCalls(fetchMock)[1][1]?.body).toBe(staleEnvelope.body);
    expect(ingressCalls(fetchMock)[2][1]?.body).toBe(envelope.body);
  });

  it.each([
    {
      code: 'webhook_authentication_failed',
      status: 401,
      copy: /authentication rejected/i,
    },
    {
      code: 'webhook_payload_invalid',
      status: 422,
      copy: /payload validation rejected/i,
    },
    {
      code: 'webhook_ingress_rate_limited',
      status: 429,
      copy: /ingress rate limit reached.*17 seconds/i,
      retryAfter: 17,
    },
    {
      code: 'demo_webhook_limit_reached',
      status: 429,
      copy: /workspace event limit reached.*reset the workspace/i,
    },
  ])('shows the $code outcome without automatically resending', async ({
    code,
    status,
    copy,
    retryAfter,
  }) => {
    const user = userEvent.setup();
    const fetchMock = installFetch(
      jsonResponse(envelope),
      errorResponse(code, status, retryAfter),
    );

    renderPanel();
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    {
      response: errorResponse('webhook_service_unavailable', 503, 1),
      copy: /service could not confirm the commit.*nothing was resent automatically/i,
    },
    {
      response: new TypeError('network details must not be rendered'),
      copy: /network response was lost.*nothing was resent automatically/i,
    },
  ])('treats an unconfirmed delivery as unknown and never retries it', async ({
    response,
    copy,
  }) => {
    const user = userEvent.setup();
    const fetchMock = installFetch(jsonResponse(envelope), response);

    renderPanel();
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/network details must not be rendered/i)).not.toBeInTheDocument();
  });

  it('retries only the committed refresh after a successful delivery', async () => {
    const user = userEvent.setup();
    const onCommitted = vi
      .fn<(result: WebhookDeliveryResult) => Promise<void>>()
      .mockRejectedValueOnce(new Error('refresh failed'))
      .mockResolvedValueOnce(undefined);
    const fetchMock = installFetch(
      jsonResponse(envelope),
      jsonResponse(delivery, 201),
    );

    renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/event was committed, but the workspace did not refresh/i);
    expect(screen.getByRole('button', { name: /deliver new failure/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /replay same event/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /tamper after signing/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /send stale signature/i })).toBeDisabled();
    await user.click(within(alert).getByRole('button', { name: /refresh workspace/i }));

    expect(await screen.findByText(/new payment failure was committed/i)).toBeVisible();
    expect(onCommitted).toHaveBeenCalledTimes(2);
    expect(onCommitted).toHaveBeenNthCalledWith(1, delivery);
    expect(onCommitted).toHaveBeenNthCalledWith(2, delivery);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(ingressCalls(fetchMock)).toHaveLength(1);
  });

  it('announces its local busy state and prevents overlapping scenarios', async () => {
    const user = userEvent.setup();
    let resolveEnvelope!: (response: Response) => void;
    const pendingEnvelope = new Promise<Response>((resolve) => {
      resolveEnvelope = resolve;
    });
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockReturnValueOnce(pendingEnvelope)
        .mockResolvedValueOnce(jsonResponse(delivery, 201)),
    );

    renderPanel();
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));

    expect(screen.getByRole('status')).toHaveTextContent(/preparing event/i);
    expect(screen.getByRole('button', { name: /preparing event/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /send stale signature/i })).toBeDisabled();

    resolveEnvelope(jsonResponse(envelope));
    expect(await screen.findByText(/new payment failure was committed/i)).toBeVisible();
  });

  it('cancels a pending signer and never delivers from an unmounted workspace', async () => {
    const user = userEvent.setup();
    const onCommitted = vi.fn().mockResolvedValue(undefined);
    let resolveEnvelope!: (response: Response) => void;
    const pendingEnvelope = new Promise<Response>((resolve) => {
      resolveEnvelope = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>().mockReturnValue(pendingEnvelope);
    vi.stubGlobal('fetch', fetchMock);

    const { unmount } = renderPanel(onCommitted);
    await user.click(screen.getByRole('button', { name: /deliver new failure/i }));
    const signerSignal = fetchMock.mock.calls[0][1]?.signal;
    unmount();

    expect(signerSignal).toBeInstanceOf(AbortSignal);
    expect(signerSignal?.aborted).toBe(true);
    await act(async () => {
      resolveEnvelope(jsonResponse(envelope));
      await pendingEnvelope;
    });
    expect(ingressCalls(fetchMock)).toHaveLength(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onCommitted).not.toHaveBeenCalled();
  });
});
