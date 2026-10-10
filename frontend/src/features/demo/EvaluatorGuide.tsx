interface EvaluatorGuideProps {
  variant: 'entry' | 'compact';
}

export const FICTIONAL_TEXT_RULE =
  'Use fictional text only. Do not enter personal, customer, credential, or confidential data.';

const evaluatorSteps = [
  {
    title: 'Create an event',
    detail:
      'Enter as Manager, open the synthetic provider panel, and deliver a fresh payment.failed event.',
  },
  {
    title: 'Test the boundary',
    detail:
      'Replay the exact event, then tamper with one signed byte to compare idempotent success with authentication rejection.',
  },
  {
    title: 'Assign the case',
    detail: 'Open the generated case and assign it to Demo Agent.',
  },
  {
    title: 'Work as Agent',
    detail:
      'Switch role, add a fictional internal note, and resolve with an allowed reason.',
  },
  {
    title: 'Verify the trail',
    detail: 'Inspect safe provenance and the ordered audit history.',
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
