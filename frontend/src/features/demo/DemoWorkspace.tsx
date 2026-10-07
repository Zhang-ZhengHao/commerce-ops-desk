import type { DemoRole, DemoSession } from '../../api/session';

export type RoleOperation =
  | { kind: 'idle' }
  | { kind: 'switching'; role: DemoRole }
  | { kind: 'error'; role: DemoRole; message: string };

interface DemoWorkspaceProps {
  session: DemoSession;
  roleOperation: RoleOperation;
  onSwitch(role: DemoRole): void;
  onRetrySwitch(): void;
}

function roleName(role: DemoRole): string {
  return role === 'manager' ? 'Manager' : 'Agent';
}

function formattedExpiry(value: string): string {
  const expiresAt = new Date(value);
  if (Number.isNaN(expiresAt.getTime())) return 'Expiry unavailable';

  return new Intl.DateTimeFormat('en-US', {
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    month: 'short',
    timeZone: 'UTC',
    timeZoneName: 'short',
    year: 'numeric',
  }).format(expiresAt);
}

export function DemoWorkspace({
  session,
  roleOperation,
  onSwitch,
  onRetrySwitch,
}: DemoWorkspaceProps) {
  const currentRole = session.identity.role;
  const busy = roleOperation.kind === 'switching';

  return (
    <section className="workspace" aria-labelledby="workspace-title">
      <div className="workspace-heading">
        <div>
          <p className="workspace-meta">{session.workspace.name}</p>
          <h1 id="workspace-title" className="workspace-title">
            {roleName(currentRole)} workspace
          </h1>
          <p className="workspace-lead">
            Review the server-issued identity, switch permission views, and verify the
            active session boundary.
          </p>
        </div>
        <span className="role-badge">{roleName(currentRole)} access</span>
      </div>

      <div className="workspace-grid">
        <article className="identity-card" aria-labelledby="identity-title">
          <h2 id="identity-title">Server-issued identity</h2>
          <p className="identity-name">{session.identity.display_name}</p>
          <dl className="identity-details">
            <div>
              <dt>Access</dt>
              <dd>{roleName(currentRole)}</dd>
            </div>
            <div>
              <dt>Workspace expires</dt>
              <dd>{formattedExpiry(session.workspace.expires_at)}</dd>
            </div>
          </dl>
        </article>

        <article className="role-card" aria-labelledby="role-switch-title">
          <h2 id="role-switch-title">Switch demo role</h2>
          <p>
            The server rotates the session and CSRF token each time. Role state is not
            trusted from the browser.
          </p>
          <div className="role-buttons role-switch-buttons">
            {session.available_roles.map((role) => {
              const current = role === currentRole;
              const label = current
                ? `Current: ${roleName(role)}`
                : `Switch to ${roleName(role)}`;
              return (
                <button
                  className={current ? 'button button-secondary' : 'button button-primary'}
                  type="button"
                  key={role}
                  disabled={current || busy}
                  onClick={() => onSwitch(role)}
                >
                  {busy && roleOperation.role === role ? `Switching to ${roleName(role)}…` : label}
                </button>
              );
            })}
          </div>

          {roleOperation.kind === 'switching' && (
            <p className="availability-note" role="status">
              Rotating the secure session…
            </p>
          )}
          {roleOperation.kind === 'error' && (
            <div className="inline-error" role="alert">
              <p>{roleOperation.message}</p>
              <button className="text-button" type="button" onClick={onRetrySwitch}>
                Retry role switch
              </button>
            </div>
          )}
        </article>
      </div>

      <article className="session-card" aria-labelledby="session-protections-title">
        <div>
          <h2 id="session-protections-title">Session protections</h2>
          <p>Controls applied to this temporary workspace.</p>
        </div>
        <ul className="protection-list">
          <li>HttpOnly session cookie with strict same-site handling</li>
          <li>In-memory CSRF token rotated after every role change</li>
          <li>Server-side membership checks and automatic workspace expiry</li>
        </ul>
      </article>
    </section>
  );
}
