import type { CaseSource } from '../../api/operations';

interface CaseSourceProps {
  source: CaseSource;
}

function sourceLabel(source: CaseSource): string {
  return source.kind === 'synthetic_webhook'
    ? 'Synthetic webhook'
    : 'Seeded demo data';
}

function formatReceivedAt(value: string): string {
  const receivedAt = new Date(value);
  if (Number.isNaN(receivedAt.getTime())) return 'Time unavailable';
  return new Intl.DateTimeFormat('en-US', {
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    month: 'short',
    timeZone: 'UTC',
    timeZoneName: 'short',
    year: 'numeric',
  }).format(receivedAt);
}

export function CaseSourceBadge({ source }: CaseSourceProps) {
  return (
    <span className={`case-source-badge case-source-${source.kind}`}>
      {sourceLabel(source)}
    </span>
  );
}

export function CaseProvenance({ source }: CaseSourceProps) {
  return (
    <section
      className="case-provenance"
      aria-labelledby="case-provenance-title"
    >
      <div className="detail-section-heading case-provenance-heading">
        <h3 id="case-provenance-title">Event provenance</h3>
        <CaseSourceBadge source={source} />
      </div>

      {source.kind === 'seeded_demo' ? (
        <p className="case-provenance-boundary">
          Created from the workspace&apos;s seeded sample data.
        </p>
      ) : (
        <dl className="case-provenance-details">
          <div>
            <dt>Provider</dt>
            <dd>Synthetic</dd>
          </div>
          <div>
            <dt>Event type</dt>
            <dd>{source.event_type}</dd>
          </div>
          <div>
            <dt>Provider event</dt>
            <dd className="case-source-id">{source.external_event_id}</dd>
          </div>
          <div>
            <dt>Received</dt>
            <dd>
              <time dateTime={source.received_at}>
                {formatReceivedAt(source.received_at)}
              </time>
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}
