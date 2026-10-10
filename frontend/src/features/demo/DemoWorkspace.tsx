import { useCallback, useEffect, useRef, useState } from 'react';

import {
  addCaseNote,
  assignCase,
  getAgents,
  getCase,
  getCases,
  getDashboard,
  OperationsApiError,
  resolveCase,
  type AgentPage,
  type CaseCommandResult,
  type CaseDetail,
  type CasePage,
  type Dashboard,
} from '../../api/operations';
import { createCommandKey, type DemoRole, type DemoSession } from '../../api/session';
import type { WebhookDeliveryResult } from '../../api/webhooks';
import {
  CaseProvenance,
  CaseSourceBadge,
} from '../cases/CaseProvenance';
import { SyntheticProviderPanel } from '../webhooks/SyntheticProviderPanel';
import { EvaluatorGuide, FICTIONAL_TEXT_RULE } from './EvaluatorGuide';

export type RoleOperation =
  | { kind: 'idle' }
  | { kind: 'switching'; role: DemoRole }
  | { kind: 'error'; role: DemoRole; message: string };

export type ResetOperation =
  | { kind: 'idle' }
  | { kind: 'confirming' }
  | { kind: 'resetting' }
  | { kind: 'checking' }
  | { kind: 'error'; message: string }
  | { kind: 'verification-error'; message: string };

interface DemoWorkspaceProps {
  session: DemoSession;
  roleOperation: RoleOperation;
  resetOperation: ResetOperation;
  onSwitch(role: DemoRole): void;
  onRetrySwitch(): void;
  onRequestReset(): void;
  onCancelReset(): void;
  onConfirmReset(): void;
  onRetryResetCheck(): void;
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

function formatLabel(value: string): string {
  const label = value.replaceAll('_', ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function formatMoney(amountMinor: number, currency: string): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
  }).format(amountMinor / 100);
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Time unavailable';
  return new Intl.DateTimeFormat('en-US', {
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    month: 'short',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(date);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Please try again.';
}

type OperationsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string; status: number | null }
  | {
      kind: 'ready';
      dashboard: Dashboard;
      cases: CasePage;
      agents: AgentPage | null;
    };

type DetailState =
  | { kind: 'idle' }
  | { kind: 'loading'; caseId: string }
  | { kind: 'error'; caseId: string; message: string }
  | { kind: 'ready'; detail: CaseDetail };

type CommandKind = 'assignment' | 'note' | 'resolution';

type CommandState =
  | { kind: 'idle' }
  | { kind: 'saving'; command: CommandKind }
  | { kind: 'recovering'; command: CommandKind; caseId: string }
  | {
      kind: 'error';
      command: CommandKind;
      caseId: string;
      message: string;
      status: number | null;
    }
  | {
      kind: 'committed-error';
      command: CommandKind;
      caseId: string;
      message: string;
    };

interface QueueFilters {
  status: string;
  severity: string;
}

type QueueState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string };

const summaryCards: ReadonlyArray<{
  key: keyof Dashboard['summary'];
  label: string;
}> = [
  { key: 'open', label: 'Open cases' },
  { key: 'approaching_sla', label: 'Approaching SLA' },
  { key: 'high_severity', label: 'High severity' },
  { key: 'resolved', label: 'Resolved' },
];

function auditDescription(event: CaseDetail['audit_events'][number]): string {
  const actor = event.actor?.display_name ?? 'System';
  if (event.action === 'case.assigned') return `Assigned by ${actor}`;
  if (event.action === 'case.resolved') return `Resolved by ${actor}`;
  if (event.action === 'case.note_added') return `Note added by ${actor}`;
  return `${formatLabel(event.action)} by ${actor}`;
}

type TimelineItem =
  | {
      kind: 'note';
      record: CaseDetail['notes'][number];
      caseVersion: number | null;
      sourceOrder: number;
    }
  | {
      kind: 'audit';
      record: CaseDetail['audit_events'][number];
      caseVersion: number | null;
      sourceOrder: number;
    };

function auditCaseVersion(event: CaseDetail['audit_events'][number]): number | null {
  const version = event.changes.version;
  return typeof version === 'number' && Number.isInteger(version) && version > 0
    ? version
    : null;
}

function accountableTimeline(detail: CaseDetail): TimelineItem[] {
  const noteVersions = new Map<string, number>();
  detail.audit_events.forEach((event) => {
    const noteId = event.changes.note_id;
    const version = auditCaseVersion(event);
    if (event.action === 'case.note_added' && typeof noteId === 'string' && version !== null) {
      noteVersions.set(noteId, version);
    }
  });

  const items: TimelineItem[] = [
    ...detail.notes.map((note, index) => ({
      kind: 'note' as const,
      record: note,
      caseVersion: noteVersions.get(note.id) ?? null,
      sourceOrder: index,
    })),
    ...detail.audit_events.map((event, index) => ({
      kind: 'audit' as const,
      record: event,
      caseVersion: auditCaseVersion(event),
      sourceOrder: detail.notes.length + index,
    })),
  ];

  return items.sort((left, right) => {
    const leftTime = Date.parse(left.record.created_at);
    const rightTime = Date.parse(right.record.created_at);
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
      return leftTime - rightTime;
    }
    if (Number.isFinite(leftTime) !== Number.isFinite(rightTime)) {
      return Number.isFinite(leftTime) ? -1 : 1;
    }
    if (
      left.caseVersion !== null &&
      right.caseVersion !== null &&
      left.caseVersion !== right.caseVersion
    ) {
      return left.caseVersion - right.caseVersion;
    }
    if (left.record.created_at !== right.record.created_at) {
      return left.record.created_at.localeCompare(right.record.created_at);
    }
    return left.sourceOrder - right.sourceOrder;
  });
}

function errorStatus(error: unknown): number | null {
  return error instanceof OperationsApiError ? error.status : null;
}

export function DemoWorkspace({
  session,
  roleOperation,
  resetOperation,
  onSwitch,
  onRetrySwitch,
  onRequestReset,
  onCancelReset,
  onConfirmReset,
  onRetryResetCheck,
}: DemoWorkspaceProps) {
  const currentRole = session.identity.role;
  const resetBusy = resetOperation.kind === 'resetting' || resetOperation.kind === 'checking';
  const sessionMutationBusy =
    roleOperation.kind === 'switching' ||
    resetBusy ||
    resetOperation.kind === 'confirming';
  const [operations, setOperations] = useState<OperationsState>({ kind: 'loading' });
  const [detail, setDetail] = useState<DetailState>({ kind: 'idle' });
  const [command, setCommand] = useState<CommandState>({ kind: 'idle' });
  const [assignmentId, setAssignmentId] = useState('');
  const [noteBody, setNoteBody] = useState('');
  const [resolutionReason, setResolutionReason] = useState('');
  const [filters, setFilters] = useState<QueueFilters>({ status: '', severity: '' });
  const filtersRef = useRef<QueueFilters>({ status: '', severity: '' });
  const [queueState, setQueueState] = useState<QueueState>({ kind: 'idle' });

  const loadOperations = useCallback(async () => {
    setOperations({ kind: 'loading' });
    setDetail({ kind: 'idle' });
    setCommand({ kind: 'idle' });
    const initialFilters = { status: '', severity: '' };
    filtersRef.current = initialFilters;
    setFilters(initialFilters);
    setQueueState({ kind: 'idle' });
    try {
      const [dashboard, cases, agents] = await Promise.all([
        getDashboard(),
        getCases(),
        currentRole === 'manager' ? getAgents() : Promise.resolve(null),
      ]);
      setOperations({ kind: 'ready', dashboard, cases, agents });
    } catch (error) {
      setOperations({
        kind: 'error',
        message: errorMessage(error),
        status: errorStatus(error),
      });
    }
  }, [currentRole]);

  useEffect(() => {
    void loadOperations();
  }, [loadOperations, session.workspace.id, session.identity.membership_id]);

  const openCase = useCallback(async (caseId: string) => {
    setDetail({ kind: 'loading', caseId });
    setCommand({ kind: 'idle' });
    try {
      const loaded = await getCase(caseId);
      setAssignmentId(loaded.assignee?.membership_id ?? '');
      setResolutionReason(loaded.resolution_reasons[0] ?? '');
      setDetail({ kind: 'ready', detail: loaded });
    } catch (error) {
      setDetail({ kind: 'error', caseId, message: errorMessage(error) });
    }
  }, []);

  const loadFilteredCases = useCallback(async (nextFilters: QueueFilters) => {
    filtersRef.current = nextFilters;
    setFilters(nextFilters);
    setQueueState({ kind: 'loading' });
    setDetail({ kind: 'idle' });
    try {
      const cases = await getCases({
        status: nextFilters.status || undefined,
        severity: nextFilters.severity || undefined,
      });
      setOperations((current) =>
        current.kind === 'ready' ? { ...current, cases } : current,
      );
      setQueueState({ kind: 'idle' });
    } catch (error) {
      setQueueState({ kind: 'error', message: errorMessage(error) });
    }
  }, []);

  const commandContext = useCallback(
    () => ({
      csrfToken: session.csrf_token,
      idempotencyKey: createCommandKey(),
    }),
    [session.csrf_token],
  );

  const updateLocalCase = useCallback((updated: CaseDetail) => {
    setDetail({ kind: 'ready', detail: updated });
    setOperations((current) => {
      if (current.kind !== 'ready') return current;
      return {
        ...current,
        cases: {
          ...current.cases,
          items: current.cases.items.map((item) =>
            item.id === updated.id ? updated : item,
          ),
        },
      };
    });
    setAssignmentId(updated.assignee?.membership_id ?? '');
    if (!updated.resolution_reasons.includes(resolutionReason)) {
      setResolutionReason(updated.resolution_reasons[0] ?? '');
    }
  }, [resolutionReason]);

  const refreshOperationalSnapshot = useCallback(async () => {
    try {
      const [dashboard, cases] = await Promise.all([
        getDashboard(),
        getCases({
          status: filters.status || undefined,
          severity: filters.severity || undefined,
        }),
      ]);
      setOperations((current) =>
        current.kind === 'ready' ? { ...current, dashboard, cases } : current,
      );
    } catch {
      // The accepted command response remains authoritative for the open case.
      // A later filter change or full retry will recover the surrounding snapshot.
    }
  }, [filters]);

  const refreshCommittedWebhook = useCallback(async (
    result: WebhookDeliveryResult,
  ) => {
    const activeFilters = filtersRef.current;
    const [dashboard, cases, loadedDetail] = await Promise.all([
      getDashboard(),
      getCases({
        status: activeFilters.status || undefined,
        severity: activeFilters.severity || undefined,
      }),
      getCase(result.case_id),
    ]);

    setOperations((current) =>
      current.kind === 'ready'
        ? { ...current, dashboard, cases }
        : current,
    );
    setDetail({ kind: 'ready', detail: loadedDetail });
    setAssignmentId(loadedDetail.assignee?.membership_id ?? '');
    setResolutionReason(loadedDetail.resolution_reasons[0] ?? '');
    setNoteBody('');
    setCommand({ kind: 'idle' });
    setQueueState({ kind: 'idle' });
  }, []);

  const runCommand = useCallback(
    async (
      kind: CommandKind,
      caseId: string,
      operation: () => Promise<CaseCommandResult>,
      clearNote = false,
    ) => {
      setCommand({ kind: 'saving', command: kind });
      try {
        const result = await operation();
        void refreshOperationalSnapshot();
        let updated: CaseDetail;
        try {
          updated = await getCase(result.case_id);
        } catch (error) {
          setCommand({
            kind: 'committed-error',
            command: kind,
            caseId: result.case_id,
            message: errorMessage(error),
          });
          return;
        }
        updateLocalCase(updated);
        if (clearNote) setNoteBody('');
        setCommand({ kind: 'idle' });
      } catch (error) {
        setCommand({
          kind: 'error',
          command: kind,
          caseId,
          message: errorMessage(error),
          status: error instanceof OperationsApiError ? error.status : null,
        });
      }
    },
    [refreshOperationalSnapshot, updateLocalCase],
  );

  const recoverCommittedCommand = useCallback(async (
    committed: Extract<CommandState, { kind: 'committed-error' }>,
  ) => {
    setCommand({
      kind: 'recovering',
      command: committed.command,
      caseId: committed.caseId,
    });
    try {
      const updated = await getCase(committed.caseId);
      updateLocalCase(updated);
      if (committed.command === 'note') setNoteBody('');
      setCommand({ kind: 'idle' });
    } catch (error) {
      setCommand({
        ...committed,
        message: errorMessage(error),
      });
    }
  }, [updateLocalCase]);

  const saving =
    command.kind === 'saving' ||
    command.kind === 'recovering' ||
    command.kind === 'committed-error';

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

      <EvaluatorGuide variant="compact" />

      {operations.kind === 'loading' && (
        <div className="operations-loading" role="status">
          <span className="loading-mark" aria-hidden="true" />
          <p>Loading operations data…</p>
        </div>
      )}

      {operations.kind === 'error' && operations.status === 403 && (
        <div className="inline-error operations-error access-error" role="alert">
          <h2>Access changed</h2>
          <p>Your active role cannot load this workspace. Switch roles or reload access.</p>
          <button className="text-button" type="button" onClick={() => void loadOperations()}>
            Reload access
          </button>
        </div>
      )}

      {operations.kind === 'error' && operations.status !== 403 && (
        <div className="inline-error operations-error" role="alert">
          <p>Could not load the operations workspace. {operations.message}</p>
          <button className="text-button" type="button" onClick={() => void loadOperations()}>
            Try again
          </button>
        </div>
      )}

      {operations.kind === 'ready' && (
        <>
          <section className="operations-overview" aria-labelledby="operations-overview-title">
            <div className="operations-section-heading">
              <div>
                <p className="eyebrow">Live workspace</p>
                <h2 id="operations-overview-title">Operations overview</h2>
              </div>
              <p>Updated {formatDateTime(operations.dashboard.generated_at)}</p>
            </div>
            <dl className="summary-grid">
              {summaryCards.map(({ key, label }) => (
                <div className="summary-card" key={key}>
                  <dt>{label}</dt>
                  <dd>{operations.dashboard.summary[key]}</dd>
                </div>
              ))}
            </dl>
          </section>

          {currentRole === 'manager' && (
            <SyntheticProviderPanel
              key={`${session.workspace.id}:${session.identity.membership_id}`}
              csrfToken={session.csrf_token}
              disabled={sessionMutationBusy}
              onCommitted={refreshCommittedWebhook}
            />
          )}

          <div className="operations-layout">
            <section className="exception-queue" aria-labelledby="exception-queue-title">
              <div className="operations-section-heading queue-heading">
                <div>
                  <p className="eyebrow">Priority order</p>
                  <h2 id="exception-queue-title">Exception queue</h2>
                </div>
                <span className="queue-count">{operations.cases.total} total</span>
              </div>

              <div className="queue-filters" aria-label="Queue filters">
                <label>
                  <span>Filter by status</span>
                  <select
                    value={filters.status}
                    disabled={queueState.kind === 'loading'}
                    onChange={(event) => void loadFilteredCases({
                      ...filters,
                      status: event.target.value,
                    })}
                  >
                    <option value="">All statuses</option>
                    <option value="open">Open</option>
                    <option value="assigned">Assigned</option>
                    <option value="resolved">Resolved</option>
                  </select>
                </label>
                <label>
                  <span>Filter by severity</span>
                  <select
                    value={filters.severity}
                    disabled={queueState.kind === 'loading'}
                    onChange={(event) => void loadFilteredCases({
                      ...filters,
                      severity: event.target.value,
                    })}
                  >
                    <option value="">All severities</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                  </select>
                </label>
              </div>

              {queueState.kind === 'loading' && (
                <p className="queue-state" role="status">Updating exception queue…</p>
              )}
              {queueState.kind === 'error' && (
                <div className="inline-error queue-state" role="alert">
                  <p>Could not update the queue. {queueState.message}</p>
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => void loadFilteredCases(filters)}
                  >
                    Try filters again
                  </button>
                </div>
              )}

              {queueState.kind !== 'loading' && operations.cases.items.length === 0 ? (
                <div className="empty-state">
                  {filters.status || filters.severity ? (
                    <>
                      <h3>No cases match these filters</h3>
                      <p>Clear the filters to return to the full exception queue.</p>
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => void loadFilteredCases({ status: '', severity: '' })}
                      >
                        Clear filters
                      </button>
                    </>
                  ) : (
                    <>
                      <h3>No exceptions yet</h3>
                      <p>This workspace has no cases in the current queue.</p>
                    </>
                  )}
                </div>
              ) : queueState.kind !== 'loading' ? (
                <ul className="case-list">
                  {operations.cases.items.map((item) => (
                    <li key={item.id}>
                      <button
                        className={`case-row${
                          detail.kind !== 'idle' &&
                          'caseId' in detail &&
                          detail.caseId === item.id
                            ? ' case-row-selected'
                            : detail.kind === 'ready' && detail.detail.id === item.id
                              ? ' case-row-selected'
                              : ''
                        }`}
                        type="button"
                        aria-label={`Open ${item.order.order_number}`}
                        onClick={() => void openCase(item.id)}
                      >
                        <span className="case-row-topline">
                          <strong>{item.order.order_number}</strong>
                          <span className={`severity-badge severity-${item.severity}`}>
                            {formatLabel(item.severity)}
                          </span>
                        </span>
                        <span className="case-rule">{formatLabel(item.rule_key)}</span>
                        <CaseSourceBadge source={item.source} />
                        <span className="case-row-meta">
                          <span>{formatLabel(item.status)}</span>
                          <span>Due {formatDateTime(item.due_at)}</span>
                        </span>
                        <span className="case-assignee">
                          {item.assignee?.display_name ?? 'Unassigned'}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>

            <section className="case-detail-shell" aria-label="Case detail">
              {detail.kind === 'idle' && (
                <div className="detail-placeholder">
                  <span className="detail-placeholder-mark" aria-hidden="true">CO</span>
                  <h2>Select a case</h2>
                  <p>Choose an exception to inspect its order and accountable timeline.</p>
                </div>
              )}
              {detail.kind === 'loading' && (
                <div className="detail-placeholder" role="status">
                  <span className="loading-mark" aria-hidden="true" />
                  <p>Loading case details…</p>
                </div>
              )}
              {detail.kind === 'error' && (
                <div className="inline-error detail-error" role="alert">
                  <p>Could not load this case. {detail.message}</p>
                  <button className="text-button" type="button" onClick={() => void openCase(detail.caseId)}>
                    Try again
                  </button>
                </div>
              )}
              {detail.kind === 'ready' && (
                <article className="case-detail" aria-labelledby="case-detail-title">
                  <header className="case-detail-header">
                    <div>
                      <p className="eyebrow">{formatLabel(detail.detail.case_type)} exception</p>
                      <h2 id="case-detail-title">Case {detail.detail.order.order_number}</h2>
                    </div>
                    <span className="status-badge">{formatLabel(detail.detail.status)}</span>
                  </header>

                  <section className="order-card" aria-labelledby="order-title">
                    <div className="detail-section-heading">
                      <h3 id="order-title">Order</h3>
                      <strong>
                        {formatMoney(
                          detail.detail.order.amount_minor,
                          detail.detail.order.currency,
                        )}
                      </strong>
                    </div>
                    <dl className="order-details">
                      <div><dt>Payment</dt><dd>{formatLabel(detail.detail.order.payment_status)}</dd></div>
                      <div><dt>Fulfillment</dt><dd>{formatLabel(detail.detail.order.fulfillment_status)}</dd></div>
                      <div><dt>Assignee</dt><dd>{detail.detail.assignee?.display_name ?? 'Unassigned'}</dd></div>
                      <div><dt>Due</dt><dd>{formatDateTime(detail.detail.due_at)}</dd></div>
                    </dl>
                  </section>

                  <CaseProvenance source={detail.detail.source} />

                  <section className="case-actions" aria-labelledby="case-actions-title">
                    <div className="detail-section-heading">
                      <h3 id="case-actions-title">Work this case</h3>
                      <span>Version {detail.detail.version}</span>
                    </div>

                    {currentRole === 'manager' && operations.agents && (
                      <form
                        className="action-form assignment-form"
                        onSubmit={(event) => {
                          event.preventDefault();
                          if (!assignmentId) return;
                          void runCommand(
                            'assignment',
                            detail.detail.id,
                            () => assignCase(
                              detail.detail.id,
                              assignmentId,
                              detail.detail.version,
                              commandContext(),
                            ),
                          );
                        }}
                      >
                        <label htmlFor="case-assignee">Assign to agent</label>
                        <div className="action-row">
                          <select
                            id="case-assignee"
                            value={assignmentId}
                            disabled={saving}
                            onChange={(event) => setAssignmentId(event.target.value)}
                          >
                            <option value="" disabled>Select an agent</option>
                            {operations.agents.items.map((agent) => (
                              <option key={agent.membership_id} value={agent.membership_id}>
                                {agent.display_name}
                              </option>
                            ))}
                          </select>
                          <button
                            className="button button-secondary button-compact"
                            type="submit"
                            disabled={saving || !assignmentId}
                          >
                            {command.kind === 'saving' && command.command === 'assignment'
                              ? 'Updating…'
                              : 'Update assignment'}
                          </button>
                        </div>
                      </form>
                    )}

                    <form
                      className="action-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        const body = noteBody.trim();
                        if (!body) return;
                        void runCommand(
                          'note',
                          detail.detail.id,
                          () => addCaseNote(
                            detail.detail.id,
                            body,
                            detail.detail.version,
                            commandContext(),
                          ),
                          true,
                        );
                      }}
                    >
                      <label htmlFor="case-note">Internal note</label>
                      <p
                        id="case-note-fictional-rule"
                        className="fictional-data-rule fictional-data-rule-note"
                      >
                        {FICTIONAL_TEXT_RULE}
                      </p>
                      <textarea
                        id="case-note"
                        aria-describedby="case-note-fictional-rule"
                        value={noteBody}
                        rows={3}
                        maxLength={1000}
                        disabled={saving}
                        placeholder="Record what changed and what happens next."
                        onChange={(event) => setNoteBody(event.target.value)}
                      />
                      <div className="form-footer">
                        <span>{noteBody.length}/1000</span>
                        <button
                          className="button button-secondary button-compact"
                          type="submit"
                          disabled={saving || !noteBody.trim()}
                        >
                          {command.kind === 'saving' && command.command === 'note'
                            ? 'Adding…'
                            : 'Add note'}
                        </button>
                      </div>
                    </form>

                    {detail.detail.status !== 'resolved' &&
                      detail.detail.resolution_reasons.length > 0 && (
                        <form
                          className="action-form resolution-form"
                          onSubmit={(event) => {
                            event.preventDefault();
                            if (!resolutionReason) return;
                            void runCommand(
                              'resolution',
                              detail.detail.id,
                              () => resolveCase(
                                detail.detail.id,
                                resolutionReason,
                                detail.detail.version,
                                commandContext(),
                              ),
                            );
                          }}
                        >
                          <label htmlFor="resolution-reason">Resolution reason</label>
                          <div className="action-row">
                            <select
                              id="resolution-reason"
                              value={resolutionReason}
                              disabled={saving}
                              onChange={(event) => setResolutionReason(event.target.value)}
                            >
                              {detail.detail.resolution_reasons.map((reason) => (
                                <option key={reason} value={reason}>{formatLabel(reason)}</option>
                              ))}
                            </select>
                            <button
                              className="button button-primary button-compact"
                              type="submit"
                              disabled={saving || !resolutionReason}
                            >
                              {command.kind === 'saving' && command.command === 'resolution'
                                ? 'Resolving…'
                                : 'Resolve case'}
                            </button>
                          </div>
                        </form>
                      )}

                    {command.kind === 'error' && (
                      <div className="inline-error command-error" role="alert">
                        <p>{command.message}</p>
                        {command.status === 409 && (
                          <button
                            className="text-button"
                            type="button"
                            onClick={() => void openCase(command.caseId)}
                          >
                            Refresh case
                          </button>
                        )}
                      </div>
                    )}

                    {command.kind === 'committed-error' && (
                      <div className="inline-error command-error" role="alert">
                        <p>
                          The change was saved, but the latest case could not be loaded.
                          {' '}{command.message}
                        </p>
                        <button
                          className="text-button"
                          type="button"
                          onClick={() => void recoverCommittedCommand(command)}
                        >
                          Refresh case
                        </button>
                      </div>
                    )}

                    {command.kind === 'recovering' && (
                      <p className="availability-note" role="status">
                        Loading the saved change…
                      </p>
                    )}
                  </section>

                  <section className="timeline-section" aria-labelledby="timeline-title">
                    <div className="detail-section-heading">
                      <h3 id="timeline-title">Accountable timeline</h3>
                      <span>{detail.detail.audit_events.length + detail.detail.notes.length} events</span>
                    </div>
                    <ol className="timeline-list">
                      {accountableTimeline(detail.detail).map((item) =>
                        item.kind === 'note' ? (
                          <li key={`note:${item.record.id}`}>
                            <span className="timeline-dot" aria-hidden="true" />
                            <div>
                              <strong>Note by {item.record.author.display_name}</strong>
                              <p>{item.record.body}</p>
                              <time dateTime={item.record.created_at}>
                                {formatDateTime(item.record.created_at)}
                              </time>
                            </div>
                          </li>
                        ) : (
                          <li key={`audit:${item.record.id}`}>
                            <span className="timeline-dot" aria-hidden="true" />
                            <div>
                              <strong>{auditDescription(item.record)}</strong>
                              <time dateTime={item.record.created_at}>
                                {formatDateTime(item.record.created_at)}
                              </time>
                            </div>
                          </li>
                        ),
                      )}
                    </ol>
                  </section>
                </article>
              )}
            </section>
          </div>
        </>
      )}

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
                  disabled={current || sessionMutationBusy}
                  onClick={() => onSwitch(role)}
                >
                  {roleOperation.kind === 'switching' && roleOperation.role === role
                    ? `Switching to ${roleName(role)}…`
                    : label}
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
          <li>In-memory CSRF token rotated after role changes and resets</li>
          <li>Server-side membership checks and automatic workspace expiry</li>
        </ul>
        <div className="reset-control">
          {resetOperation.kind === 'idle' && (
            <>
              <p>Restore the original sample orders and exception cases.</p>
              <button
                className="button button-secondary"
                type="button"
                disabled={roleOperation.kind === 'switching'}
                onClick={onRequestReset}
              >
                Reset demo data
              </button>
            </>
          )}

          {resetOperation.kind === 'confirming' && (
            <div className="reset-confirmation" aria-labelledby="reset-confirmation-title">
              <h3 id="reset-confirmation-title">Reset this workspace?</h3>
              <p>
                This replaces the current workspace and removes its demo changes.
                The original sample cases will be restored.
              </p>
              <div className="reset-actions">
                <button
                  className="button button-secondary button-compact"
                  type="button"
                  onClick={onCancelReset}
                >
                  Cancel
                </button>
                <button
                  className="button button-primary button-compact"
                  type="button"
                  onClick={onConfirmReset}
                >
                  Reset workspace
                </button>
              </div>
            </div>
          )}

          {resetOperation.kind === 'resetting' && (
            <p className="availability-note" role="status">Resetting workspace…</p>
          )}

          {resetOperation.kind === 'checking' && (
            <p className="availability-note" role="status">
              Checking the active workspace…
            </p>
          )}

          {resetOperation.kind === 'error' && (
            <div className="inline-error reset-error" role="alert">
              <p>{resetOperation.message} The current workspace is still active.</p>
              <button className="text-button" type="button" onClick={onRequestReset}>
                Try reset again
              </button>
            </div>
          )}

          {resetOperation.kind === 'verification-error' && (
            <div className="inline-error reset-error" role="alert">
              <p>{resetOperation.message}</p>
              <button className="text-button" type="button" onClick={onRetryResetCheck}>
                Check workspace state
              </button>
            </div>
          )}
        </div>
      </article>
    </section>
  );
}
