export interface DashboardSummary {
  open: number;
  approaching_sla: number;
  high_severity: number;
  resolved: number;
}

export interface DashboardRuleCount {
  rule_key: string;
  case_type: string;
  count: number;
}

export interface Dashboard {
  generated_at: string;
  summary: DashboardSummary;
  by_rule: DashboardRuleCount[];
}

export interface CaseOrder {
  id: string;
  order_number: string;
  amount_minor: number;
  currency: string;
  payment_status: string;
  fulfillment_status: string;
}

export interface CaseActor {
  membership_id: string;
  display_name: string;
}

export interface SeededDemoCaseSource {
  kind: 'seeded_demo';
}

export interface SyntheticWebhookCaseSource {
  kind: 'synthetic_webhook';
  provider: 'synthetic';
  event_type: 'payment.failed';
  external_event_id: string;
  received_at: string;
}

export type CaseSource = SeededDemoCaseSource | SyntheticWebhookCaseSource;

export interface CaseSummary {
  id: string;
  rule_key: string;
  case_type: string;
  severity: string;
  status: string;
  due_at: string;
  updated_at: string;
  version: number;
  resolution_reason: string | null;
  resolved_at: string | null;
  source: CaseSource;
  order: CaseOrder;
  assignee: CaseActor | null;
}

export interface CaseNote {
  id: string;
  body: string;
  author: CaseActor;
  created_at: string;
}

export interface AuditEvent {
  id: string;
  action: string;
  object_type: string;
  object_id: string;
  actor: CaseActor | null;
  changes: Record<string, unknown>;
  created_at: string;
}

export interface CaseDetail extends CaseSummary {
  created_at: string;
  resolution_reasons: string[];
  notes: CaseNote[];
  audit_events: AuditEvent[];
}

export interface CasePage {
  items: CaseSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface AgentPage {
  items: CaseActor[];
}

export interface CaseFilters {
  status?: string;
  severity?: string;
}

export interface CaseCommandContext {
  csrfToken: string;
  idempotencyKey: string;
}

export interface CaseCommandResult {
  case_id: string;
  version: number;
}

export class OperationsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'OperationsApiError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

async function apiError(response: Response): Promise<OperationsApiError> {
  let message = `Request failed with status ${response.status}.`;
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === 'string' && body.detail.trim()) {
      message = body.detail;
    }
  } catch {
    // Keep the stable status-based message for malformed upstream responses.
  }
  return new OperationsApiError(message, response.status);
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await apiError(response);
  try {
    return (await response.json()) as T;
  } catch {
    throw new OperationsApiError('The operations API returned an unreadable response.', 502);
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    method: 'GET',
  });
  return readJson<T>(response);
}

async function postCaseCommand(
  path: string,
  body: Record<string, unknown>,
  context: CaseCommandContext,
): Promise<CaseCommandResult> {
  const response = await fetch(path, {
    body: JSON.stringify(body),
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': context.idempotencyKey,
      'X-CSRF-Token': context.csrfToken,
    },
    method: 'POST',
  });
  return readJson<CaseCommandResult>(response);
}

export function getDashboard(): Promise<Dashboard> {
  return getJson('/api/dashboard');
}

export function getCases(filters: CaseFilters = {}): Promise<CasePage> {
  const query = new URLSearchParams({ page: '1', page_size: '20', sort: 'due_at' });
  if (filters.status) query.set('status', filters.status);
  if (filters.severity) query.set('severity', filters.severity);
  return getJson(`/api/cases?${query.toString()}`);
}

export function getCase(caseId: string): Promise<CaseDetail> {
  return getJson(`/api/cases/${encodeURIComponent(caseId)}`);
}

export function getAgents(): Promise<AgentPage> {
  return getJson('/api/agents');
}

export function assignCase(
  caseId: string,
  assigneeId: string,
  version: number,
  context: CaseCommandContext,
): Promise<CaseCommandResult> {
  return postCaseCommand(
    `/api/cases/${encodeURIComponent(caseId)}/assignment`,
    { assignee_id: assigneeId, version },
    context,
  );
}

export function addCaseNote(
  caseId: string,
  body: string,
  version: number,
  context: CaseCommandContext,
): Promise<CaseCommandResult> {
  return postCaseCommand(
    `/api/cases/${encodeURIComponent(caseId)}/notes`,
    { body, version },
    context,
  );
}

export function resolveCase(
  caseId: string,
  reason: string,
  version: number,
  context: CaseCommandContext,
): Promise<CaseCommandResult> {
  return postCaseCommand(
    `/api/cases/${encodeURIComponent(caseId)}/resolution`,
    { reason, version },
    context,
  );
}
