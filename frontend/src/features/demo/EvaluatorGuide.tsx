interface EvaluatorGuideProps {
  variant: 'entry' | 'compact';
}

export const FICTIONAL_TEXT_RULE =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';

const evaluatorSteps = [
  {
    title: 'Create an event',
    detail:
      'Use “Enter as Manager”, find “Synthetic provider”, then select “Deliver new failure” to create a payment.failed case.',
  },
  {
    title: 'Test the boundary',
    detail:
      'Select “Replay same event”, “Tamper after signing”, and “Send stale signature” to compare idempotent replay with authentication rejection.',
  },
  {
    title: 'Assign the case',
    detail:
      'Open the generated case, choose Demo Agent under “Assign to agent”, then select “Update assignment”.',
  },
  {
    title: 'Work as Agent',
    detail:
      'Use “Switch to Agent”, enter fictional text in “Internal note”, and select “Add note”. Then choose a “Resolution reason” and select “Resolve case”.',
  },
  {
    title: 'Verify the trail',
    detail: 'Inspect “Event provenance” and the ordered “Accountable timeline”.',
  },
] as const;

export function EvaluatorGuide({ variant }: EvaluatorGuideProps) {
  const headingId = `${variant}-evaluator-guide-title`;

  return (
    <aside
      className={`evaluator-guide evaluator-guide-${variant}`}
      aria-labelledby={headingId}
    >
      <div className="evaluator-guide-header">
        <p className="evaluator-guide-label">Evaluation path</p>
        <h2 id={headingId}>Five-step evaluator guide</h2>
        <p className="evaluator-guide-intro">
          A short journey using fictional data. No account is needed, and nothing here
          can affect a live store or payment.
        </p>
      </div>

      <ol className="evaluator-guide-list" role="list">
        {evaluatorSteps.map(({ title, detail }, index) => (
          <li className="evaluator-guide-step" key={title}>
            <span className="evaluator-step-index" aria-hidden="true">
              {String(index + 1).padStart(2, '0')}
            </span>
            <span className="evaluator-step-copy">
              <strong>{title}</strong>
              <span>{detail}</span>
            </span>
          </li>
        ))}
      </ol>
    </aside>
  );
}
