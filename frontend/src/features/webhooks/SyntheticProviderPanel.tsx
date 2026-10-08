import { useEffect, useRef, useState } from 'react';

import {
  deliverWebhookEnvelope,
  requestWebhookEnvelope,
  WebhookApiError,
  type WebhookDeliveryResult,
  type WebhookEnvelope,
} from '../../api/webhooks';
import './SyntheticProviderPanel.css';

interface SyntheticProviderPanelProps {
  csrfToken: string;
  disabled?: boolean;
  onCommitted(result: WebhookDeliveryResult): Promise<void>;
}

type ScenarioAction = 'deliver' | 'replay' | 'tamper' | 'stale';

type PanelState =
  | { kind: 'idle' }
  | {
      kind: 'busy';
      action: ScenarioAction;
      stage: 'preparing' | 'delivering' | 'refreshing';
    }
  | { kind: 'success'; result: WebhookDeliveryResult }
  | { kind: 'replay'; result: WebhookDeliveryResult }
  | { kind: 'authentication-rejected' }
  | { kind: 'validation-rejected' }
  | { kind: 'conflict' }
  | { kind: 'ingress-limited'; retryAfter: number | null }
  | { kind: 'workspace-limited' }
  | { kind: 'network-unknown' }
  | { kind: 'service-unknown'; retryAfter: number | null }
  | { kind: 'preparation-error' }
  | { kind: 'error' }
  | { kind: 'committed-refresh-error'; result: WebhookDeliveryResult }
  | {
      kind: 'committed-refreshing';
      action: ScenarioAction;
      result: WebhookDeliveryResult;
    };

function tamperOneAsciiByte(body: string): string {
  const replacement = body[0] === '{' ? '[' : body[0] === 'a' ? 'b' : 'a';
  return `${replacement}${body.slice(1)}`;
}

function deliveryErrorState(error: unknown, deliveryStarted: boolean): PanelState {
  if (!(error instanceof WebhookApiError)) return { kind: 'error' };
  if (!deliveryStarted) return { kind: 'preparation-error' };
  if (error.code === 'webhook_network_error') return { kind: 'network-unknown' };
  if (error.status === 503 || error.code === 'webhook_service_unavailable') {
    return { kind: 'service-unknown', retryAfter: error.retryAfter };
  }
  if (error.status === 401 || error.code === 'webhook_authentication_failed') {
    return { kind: 'authentication-rejected' };
  }
  if (error.code === 'webhook_ingress_rate_limited') {
    return { kind: 'ingress-limited', retryAfter: error.retryAfter };
  }
  if (error.code === 'demo_webhook_limit_reached') {
    return { kind: 'workspace-limited' };
  }
  if (error.status === 409 || error.code === 'webhook_event_conflict') {
    return { kind: 'conflict' };
  }
  if (
    error.status === 413 ||
    error.status === 415 ||
    error.status === 422 ||
    error.code === 'request_body_too_large' ||
    error.code === 'request_validation_failed' ||
    error.code === 'webhook_media_type_unsupported' ||
    error.code === 'webhook_payload_invalid'
  ) {
    return { kind: 'validation-rejected' };
  }
  return { kind: 'error' };
}

function busyLabel(state: Extract<PanelState, { kind: 'busy' }>): string {
  if (state.stage === 'preparing') return 'Preparing event…';
  if (state.stage === 'refreshing') return 'Refreshing workspace…';
  if (state.action === 'replay') return 'Replaying event…';
  if (state.action === 'tamper') return 'Sending tampered event…';
  if (state.action === 'stale') return 'Sending stale event…';
  return 'Delivering event…';
}

function ResultSummary({
  result,
  replayed,
}: {
  result: WebhookDeliveryResult;
  replayed: boolean;
}) {
  return (
    <div className="synthetic-result">
      <p>
        {replayed
          ? 'Exact replay returned the same case.'
          : 'A new payment failure was committed.'}
      </p>
      <dl className="synthetic-result-meta">
        <div>
          <dt>Provider event</dt>
          <dd>{result.event_id}</dd>
        </div>
        <div>
          <dt>Event type</dt>
          <dd>payment.failed</dd>
        </div>
        <div>
          <dt>Outcome</dt>
          <dd>{replayed ? 'No duplicate business effect' : 'New business effect'}</dd>
        </div>
      </dl>
    </div>
  );
}

function Feedback({
  state,
  refreshDisabled,
  onRetryCommittedRefresh,
}: {
  state: PanelState;
  refreshDisabled: boolean;
  onRetryCommittedRefresh(result: WebhookDeliveryResult): void;
}) {
  if (state.kind === 'idle') {
    return <p className="synthetic-feedback-placeholder">Choose a scenario to begin.</p>;
  }
  if (state.kind === 'busy') {
    return <p role="status">{busyLabel(state)}</p>;
  }
  if (state.kind === 'committed-refreshing') {
    return <p role="status">Refreshing the committed case…</p>;
  }
  if (state.kind === 'success') {
    return <ResultSummary result={state.result} replayed={false} />;
  }
  if (state.kind === 'replay') {
    return <ResultSummary result={state.result} replayed />;
  }
  if (state.kind === 'committed-refresh-error') {
    return (
      <div className="synthetic-alert" role="alert">
        <p>The event was committed, but the workspace did not refresh.</p>
        <button
          className="synthetic-refresh-button"
          type="button"
          disabled={refreshDisabled}
          onClick={() => onRetryCommittedRefresh(state.result)}
        >
          Refresh workspace
        </button>
      </div>
    );
  }
  if (state.kind === 'authentication-rejected') {
    return (
      <p className="synthetic-alert" role="alert">
        Authentication rejected. The signed request was not accepted, and no business
        change was confirmed.
      </p>
    );
  }
  if (state.kind === 'validation-rejected') {
    return (
      <p className="synthetic-alert" role="alert">
        Payload validation rejected the request. No business change was confirmed.
      </p>
    );
  }
  if (state.kind === 'conflict') {
    return (
      <p className="synthetic-alert" role="alert">
        The event conflicts with stored data. Review the event before trying another
        scenario.
      </p>
    );
  }
  if (state.kind === 'ingress-limited') {
    const wait = state.retryAfter === null ? '' : ` Wait ${state.retryAfter} seconds.`;
    return (
      <p className="synthetic-alert" role="alert">
        Ingress rate limit reached.{wait}
      </p>
    );
  }
  if (state.kind === 'workspace-limited') {
    return (
      <p className="synthetic-alert" role="alert">
        Workspace event limit reached. Reset the workspace to continue.
      </p>
    );
  }
  if (state.kind === 'network-unknown') {
    return (
      <p className="synthetic-alert" role="alert">
        Delivery outcome is unknown because the network response was lost. Nothing was
        resent automatically.
      </p>
    );
  }
  if (state.kind === 'service-unknown') {
    const wait = state.retryAfter === null ? '' : ` Retry after ${state.retryAfter} seconds.`;
    return (
      <p className="synthetic-alert" role="alert">
        The service could not confirm the commit. Nothing was resent automatically.{wait}
      </p>
    );
  }
  if (state.kind === 'preparation-error') {
    return (
      <p className="synthetic-alert" role="alert">
        The signed event could not be prepared. No webhook delivery was started.
      </p>
    );
  }
  return (
    <p className="synthetic-alert" role="alert">
      The simulator could not complete this scenario. No automatic retry was attempted.
    </p>
  );
}

export function SyntheticProviderPanel({
  csrfToken,
  disabled = false,
  onCommitted,
}: SyntheticProviderPanelProps) {
  const freshEnvelope = useRef<WebhookEnvelope | null>(null);
  const lastAction = useRef<ScenarioAction>('deliver');
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const activeController = useRef<AbortController | null>(null);
  const [state, setState] = useState<PanelState>({ kind: 'idle' });
  const busy = state.kind === 'busy' || state.kind === 'committed-refreshing';
  const scenarioLocked = busy || disabled || state.kind === 'committed-refresh-error';

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current = false;
      freshEnvelope.current = null;
      activeController.current?.abort();
      activeController.current = null;
    };
  }, []);

  function requestIsActive(controller: AbortController): boolean {
    return (
      mounted.current &&
      !controller.signal.aborted &&
      activeController.current === controller
    );
  }

  async function refreshCommitted(
    result: WebhookDeliveryResult,
    action: ScenarioAction,
    controller: AbortController,
  ): Promise<void> {
    if (!requestIsActive(controller)) return;
    setState({ kind: 'busy', action, stage: 'refreshing' });
    try {
      await onCommitted(result);
      if (!requestIsActive(controller)) return;
      setState(result.replayed ? { kind: 'replay', result } : { kind: 'success', result });
    } catch {
      if (!requestIsActive(controller)) return;
      setState({ kind: 'committed-refresh-error', result });
    }
  }

  async function runScenario(action: ScenarioAction): Promise<void> {
    if (inFlight.current || scenarioLocked) return;
    inFlight.current = true;
    const controller = new AbortController();
    activeController.current = controller;
    lastAction.current = action;
    let selectedEnvelope: WebhookEnvelope | null = null;
    let deliveryStarted = false;
    try {
      if (action === 'deliver' || action === 'stale') {
        setState({ kind: 'busy', action, stage: 'preparing' });
        selectedEnvelope = await requestWebhookEnvelope(
          action === 'stale' ? 'stale' : 'fresh',
          csrfToken,
          controller.signal,
        );
        if (!requestIsActive(controller)) return;
        if (action === 'deliver') freshEnvelope.current = selectedEnvelope;
      } else {
        selectedEnvelope = freshEnvelope.current;
      }
      if (selectedEnvelope === null) {
        setState({ kind: 'preparation-error' });
        return;
      }

      const outboundEnvelope =
        action === 'tamper'
          ? { ...selectedEnvelope, body: tamperOneAsciiByte(selectedEnvelope.body) }
          : selectedEnvelope;
      setState({ kind: 'busy', action, stage: 'delivering' });
      deliveryStarted = true;
      const result = await deliverWebhookEnvelope(outboundEnvelope, controller.signal);
      if (!requestIsActive(controller)) return;
      await refreshCommitted(result, action, controller);
    } catch (error) {
      if (!requestIsActive(controller)) return;
      setState(deliveryErrorState(error, deliveryStarted));
    } finally {
      if (activeController.current === controller) {
        activeController.current = null;
        inFlight.current = false;
      }
    }
  }

  async function retryCommittedRefresh(result: WebhookDeliveryResult): Promise<void> {
    if (inFlight.current || disabled) return;
    inFlight.current = true;
    const controller = new AbortController();
    activeController.current = controller;
    const action = lastAction.current;
    setState({ kind: 'committed-refreshing', action, result });
    try {
      await onCommitted(result);
      if (!requestIsActive(controller)) return;
      setState(result.replayed ? { kind: 'replay', result } : { kind: 'success', result });
    } catch {
      if (!requestIsActive(controller)) return;
      setState({ kind: 'committed-refresh-error', result });
    } finally {
      if (activeController.current === controller) {
        activeController.current = null;
        inFlight.current = false;
      }
    }
  }

  return (
    <section
      className="synthetic-provider-panel"
      aria-labelledby="synthetic-provider-title"
      aria-busy={busy}
    >
      <div className="synthetic-provider-heading">
        <div>
          <p className="synthetic-provider-eyebrow">External event proof</p>
          <h2 id="synthetic-provider-title">Synthetic provider</h2>
        </div>
        <span className="synthetic-provider-badge">Demo only</span>
      </div>
      <p className="synthetic-provider-boundary">
        No real store or payment processor is connected. These controls send synthetic
        data through the same signed webhook boundary.
      </p>

      <div className="synthetic-provider-actions">
        <button
          className="synthetic-action synthetic-action-primary"
          type="button"
          disabled={scenarioLocked}
          onClick={() => void runScenario('deliver')}
        >
          {state.kind === 'busy' && state.action === 'deliver'
            ? busyLabel(state)
            : 'Deliver new failure'}
        </button>
        <button
          className="synthetic-action"
          type="button"
          disabled={scenarioLocked || freshEnvelope.current === null}
          onClick={() => void runScenario('replay')}
        >
          {state.kind === 'busy' && state.action === 'replay'
            ? busyLabel(state)
            : 'Replay same event'}
        </button>
        <button
          className="synthetic-action"
          type="button"
          disabled={scenarioLocked || freshEnvelope.current === null}
          onClick={() => void runScenario('tamper')}
        >
          {state.kind === 'busy' && state.action === 'tamper'
            ? busyLabel(state)
            : 'Tamper after signing'}
        </button>
        <button
          className="synthetic-action"
          type="button"
          disabled={scenarioLocked}
          onClick={() => void runScenario('stale')}
        >
          {state.kind === 'busy' && state.action === 'stale'
            ? busyLabel(state)
            : 'Send stale signature'}
        </button>
      </div>

      <div className="synthetic-feedback" aria-live="polite" aria-atomic="true">
        <Feedback
          state={state}
          refreshDisabled={disabled}
          onRetryCommittedRefresh={(result) => void retryCommittedRefresh(result)}
        />
      </div>
    </section>
  );
}
