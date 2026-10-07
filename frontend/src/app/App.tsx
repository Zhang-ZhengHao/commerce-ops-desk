import { useCallback, useEffect, useState } from 'react';

import {
  createCommandKey,
  createDemoWorkspace,
  restoreDemoSession,
  switchDemoRole,
  type DemoRole,
  type DemoSession,
} from '../api/session';
import { DemoEntry } from '../features/demo/DemoEntry';
import {
  DemoWorkspace,
  type RoleOperation,
} from '../features/demo/DemoWorkspace';
import './App.css';

type EntryState =
  | { kind: 'checking' }
  | { kind: 'ready' }
  | { kind: 'recovery-error'; message: string }
  | { kind: 'creating'; role: DemoRole }
  | { kind: 'create-error'; role: DemoRole; message: string };

interface PendingCommand {
  role: DemoRole;
  idempotencyKey: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Please try again.';
}

export function App() {
  const [entryState, setEntryState] = useState<EntryState>({ kind: 'checking' });
  const [session, setSession] = useState<DemoSession | null>(null);
  const [pendingCreate, setPendingCreate] = useState<PendingCommand | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<PendingCommand | null>(null);
  const [roleOperation, setRoleOperation] = useState<RoleOperation>({ kind: 'idle' });

  const recoverSession = useCallback(async () => {
    setEntryState({ kind: 'checking' });
    try {
      const restored = await restoreDemoSession();
      setSession(restored);
      setEntryState({ kind: 'ready' });
    } catch (error) {
      setEntryState({ kind: 'recovery-error', message: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    void recoverSession();
  }, [recoverSession]);

  const runCreate = useCallback(async (command: PendingCommand) => {
    setPendingCreate(command);
    setEntryState({ kind: 'creating', role: command.role });
    try {
      const created = await createDemoWorkspace(command.role, command.idempotencyKey);
      setSession(created);
      setPendingCreate(null);
      setEntryState({ kind: 'ready' });
    } catch (error) {
      setEntryState({
        kind: 'create-error',
        role: command.role,
        message: errorMessage(error),
      });
    }
  }, []);

  const startDemo = useCallback(
    (role: DemoRole) => {
      void runCreate({ role, idempotencyKey: createCommandKey() });
    },
    [runCreate],
  );

  const retryCreate = useCallback(() => {
    if (pendingCreate) void runCreate(pendingCreate);
  }, [pendingCreate, runCreate]);

  const runSwitch = useCallback(
    async (command: PendingCommand, recoveredSession?: DemoSession) => {
      const activeSession = recoveredSession ?? session;
      if (!activeSession) return;

      setPendingSwitch(command);
      setRoleOperation({ kind: 'switching', role: command.role });
      try {
        const switched = await switchDemoRole(
          command.role,
          activeSession.csrf_token,
          command.idempotencyKey,
        );
        setSession(switched);
        setPendingSwitch(null);
        setRoleOperation({ kind: 'idle' });
      } catch (error) {
        setRoleOperation({
          kind: 'error',
          role: command.role,
          message: errorMessage(error),
        });
      }
    },
    [session],
  );

  const startRoleSwitch = useCallback(
    (role: DemoRole) => {
      void runSwitch({ role, idempotencyKey: createCommandKey() });
    },
    [runSwitch],
  );

  const retryRoleSwitch = useCallback(() => {
    if (!pendingSwitch) return;

    void (async () => {
      setRoleOperation({ kind: 'switching', role: pendingSwitch.role });
      try {
        const restored = await restoreDemoSession();
        if (!restored) {
          // The server may have committed the switch while the response headers,
          // including the replacement cookie, were lost. Replaying with the old
          // cookie, CSRF token, and command key lets the API recover that receipt.
          await runSwitch(pendingSwitch);
          return;
        }

        setSession(restored);
        if (restored.identity.role === pendingSwitch.role) {
          setPendingSwitch(null);
          setRoleOperation({ kind: 'idle' });
          return;
        }

        await runSwitch(pendingSwitch, restored);
      } catch (error) {
        setRoleOperation({
          kind: 'error',
          role: pendingSwitch.role,
          message: errorMessage(error),
        });
      }
    })();
  }, [pendingSwitch, runSwitch]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>

      <header className="site-header" aria-label="Product header">
        <a className="brand" href="#main-content" aria-label="CommerceOps Desk home">
          <span className="brand-mark" aria-hidden="true">
            C/O
          </span>
          <span>CommerceOps Desk</span>
        </a>
        <span className="environment-label">
          <span className="environment-dot" aria-hidden="true" />
          Demo environment
        </span>
      </header>

      <main id="main-content" className="main-content">
        {session ? (
          <DemoWorkspace
            session={session}
            roleOperation={roleOperation}
            onSwitch={startRoleSwitch}
            onRetrySwitch={retryRoleSwitch}
          />
        ) : (
          <DemoEntry
            state={entryState}
            onStart={startDemo}
            onRetryRecovery={() => void recoverSession()}
            onRetryCreate={retryCreate}
          />
        )}
      </main>

      <footer className="site-footer">
        <p>A verifiable full-stack reference. It does not connect to a live merchant system.</p>
      </footer>
    </div>
  );
}
