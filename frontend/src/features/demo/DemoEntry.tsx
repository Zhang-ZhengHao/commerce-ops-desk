import type { DemoRole } from '../../api/session';

type EntryState =
  | { kind: 'checking' }
  | { kind: 'ready' }
  | { kind: 'recovery-error'; message: string }
  | { kind: 'creating'; role: DemoRole }
  | { kind: 'create-error'; role: DemoRole; message: string };

interface DemoEntryProps {
  state: EntryState;
  onStart(role: DemoRole): void;
  onRetryRecovery(): void;
  onRetryCreate(): void;
}

const demoBoundaries = [
  {
    title: 'Synthetic data only',
    detail: 'Fictional orders and events. No live customer or payment records.',
  },
  {
    title: 'Single-node SQLite demo',
    detail: 'A deliberately scoped sandbox; the PostgreSQL path is compiled offline only.',
  },
  {
    title: '4-hour expiry',
    detail: 'Each workspace has a server-enforced expiry and a dedicated data boundary.',
  },
] as const;

const targetFlow = [
  ['01', 'Review', 'Synthetic exception queue'],
  ['02', 'Triage', 'Manager owns the queue'],
  ['03', 'Resolve', 'Agent records the outcome'],
  ['04', 'Trace', 'Audit history stays visible'],
] as const;

function roleName(role: DemoRole): string {
  return role === 'manager' ? 'Manager' : 'Agent';
}

export function DemoEntry({
  state,
  onStart,
  onRetryRecovery,
  onRetryCreate,
}: DemoEntryProps) {
  const busy = state.kind === 'checking' || state.kind === 'creating';
  const recoveryBlocked = state.kind === 'recovery-error';

  return (
    <>
      <section className="hero" aria-labelledby="entry-title">
        <div className="hero-copy">
          <h1 id="entry-title">
            Turn ecommerce exceptions into accountable work.
          </h1>
          <p className="hero-detail">
            Create a temporary Manager or Agent workspace to inspect secure session,
            role, and tenant boundaries without a live store.
          </p>

          <div
            className="role-actions"
            role="group"
            aria-labelledby="role-actions-label"
            aria-describedby="demo-availability"
          >
            <p id="role-actions-label" className="role-actions-label">
              Choose a workspace view
            </p>
            <div className="role-buttons">
              <button
                className="button button-primary"
                type="button"
                disabled={busy || recoveryBlocked}
                onClick={() => onStart('manager')}
              >
                {state.kind === 'creating' && state.role === 'manager'
                  ? 'Creating Manager workspace…'
                  : 'Enter as Manager'}
              </button>
              <button
                className="button button-secondary"
                type="button"
                disabled={busy || recoveryBlocked}
                onClick={() => onStart('agent')}
              >
                {state.kind === 'creating' && state.role === 'agent'
                  ? 'Creating Agent workspace…'
                  : 'Enter as Agent'}
              </button>
            </div>

            {state.kind === 'checking' && (
              <p id="demo-availability" className="availability-note" role="status">
                Checking for an active demo…
              </p>
            )}
            {state.kind === 'ready' && (
              <p id="demo-availability" className="availability-note">
                No sign-up or live store connection required.
              </p>
            )}
            {state.kind === 'creating' && (
              <p id="demo-availability" className="availability-note" role="status">
                Creating a secure {roleName(state.role)} workspace…
              </p>
            )}
            {state.kind === 'recovery-error' && (
              <div id="demo-availability" className="inline-error" role="alert">
                <p>Could not check for an active demo. {state.message}</p>
                <button className="text-button" type="button" onClick={onRetryRecovery}>
                  Try again
                </button>
              </div>
            )}
            {state.kind === 'create-error' && (
              <div id="demo-availability" className="inline-error" role="alert">
                <p>{state.message}</p>
                <button className="text-button" type="button" onClick={onRetryCreate}>
                  Retry {roleName(state.role)} entry
                </button>
              </div>
            )}
          </div>
        </div>

        <aside className="workflow-card" aria-labelledby="workflow-title">
          <div className="workflow-card-header">
            <h2 id="workflow-title">Operational workflow</h2>
            <p>Available in the temporary I03 workspace with synthetic case data.</p>
          </div>

          <ol className="workflow-list">
            {targetFlow.map(([index, title, detail]) => (
              <li key={index} className="workflow-step">
                <span className="step-index" aria-hidden="true">
                  {index}
                </span>
                <span className="step-copy">
                  <strong>{title}</strong>
                  <span>{detail}</span>
                </span>
              </li>
            ))}
          </ol>
        </aside>
      </section>

      <section className="boundaries" aria-labelledby="boundaries-title">
        <div className="section-heading">
          <h2 id="boundaries-title">Built for a safe public demo</h2>
          <p>
            The sandbox is intentionally separate from live commerce systems.
          </p>
        </div>

        <ul className="boundary-list">
          {demoBoundaries.map(({ title, detail }) => (
            <li key={title} className="boundary-item">
              <h3>{title}</h3>
              <p>{detail}</p>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
