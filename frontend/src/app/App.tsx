import { useCallback, useEffect, useState } from 'react';

import {
  createCommandKey,
  createDemoWorkspace,
  resetDemoWorkspace,
  restoreDemoSession,
  switchDemoRole,
  type DemoRole,
  type DemoSession,
} from '../api/session';
import { DemoEntry } from '../features/demo/DemoEntry';
import {
  DemoWorkspace,
  type ResetOperation,
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

interface PendingResetRecovery {
  previousWorkspaceId: string;
  message: string;
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
  const [resetOperation, setResetOperation] = useState<ResetOperation>({ kind: 'idle' });
  const [pendingResetRecovery, setPendingResetRecovery] =
    useState<PendingResetRecovery | null>(null);

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
      if (
        resetOperation.kind === 'confirming' ||
        resetOperation.kind === 'resetting' ||
        resetOperation.kind === 'checking'
      ) {
        return;
      }
      setResetOperation({ kind: 'idle' });
      setPendingResetRecovery(null);
      void runSwitch({ role, idempotencyKey: createCommandKey() });
    },
    [resetOperation.kind, runSwitch],
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

  const verifyResetOutcome = useCallback(async (pending: PendingResetRecovery) => {
    setResetOperation({ kind: 'checking' });
    try {
      const restored = await restoreDemoSession();
      if (!restored) {
        setSession(null);
        setEntryState({ kind: 'ready' });
        setPendingResetRecovery(null);
        setResetOperation({ kind: 'idle' });
        return;
      }

      setSession(restored);
      if (restored.workspace.id !== pending.previousWorkspaceId) {
        setPendingResetRecovery(null);
        setResetOperation({ kind: 'idle' });
        return;
      }

      setResetOperation({ kind: 'error', message: pending.message });
    } catch (error) {
      setResetOperation({
        kind: 'verification-error',
        message: `${pending.message} Could not verify the active workspace. ${errorMessage(error)}`,
      });
    }
  }, []);

  const confirmReset = useCallback(() => {
    if (
      !session ||
      roleOperation.kind === 'switching' ||
      resetOperation.kind !== 'confirming'
    ) {
      return;
    }
    const previousWorkspaceId = session.workspace.id;
    setPendingSwitch(null);
    setRoleOperation({ kind: 'idle' });
    setResetOperation({ kind: 'resetting' });
    void (async () => {
      try {
        const reset = await resetDemoWorkspace(session.csrf_token);
        setSession(reset);
        setPendingResetRecovery(null);
        setResetOperation({ kind: 'idle' });
      } catch (error) {
        const pending = {
          previousWorkspaceId,
          message: errorMessage(error),
        };
        setPendingResetRecovery(pending);
        await verifyResetOutcome(pending);
      }
    })();
  }, [resetOperation.kind, roleOperation.kind, session, verifyResetOutcome]);

  const requestReset = useCallback(() => {
    if (roleOperation.kind === 'switching') return;
    setResetOperation({ kind: 'confirming' });
  }, [roleOperation.kind]);

  const cancelReset = useCallback(() => {
    setResetOperation({ kind: 'idle' });
  }, []);

  const retryResetCheck = useCallback(() => {
    if (pendingResetRecovery) void verifyResetOutcome(pendingResetRecovery);
  }, [pendingResetRecovery, verifyResetOutcome]);

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
            key={`${session.workspace.id}:${session.identity.membership_id}`}
            session={session}
            roleOperation={roleOperation}
            resetOperation={resetOperation}
            onSwitch={startRoleSwitch}
            onRetrySwitch={retryRoleSwitch}
            onRequestReset={requestReset}
            onCancelReset={cancelReset}
            onConfirmReset={confirmReset}
            onRetryResetCheck={retryResetCheck}
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
