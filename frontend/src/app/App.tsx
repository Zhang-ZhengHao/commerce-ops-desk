import './App.css';

const demoBoundaries = [
  {
    index: '01',
    title: 'Synthetic data only',
    detail: 'Fictional orders and events. No live customer or payment records.',
  },
  {
    index: '02',
    title: 'Single-node SQLite demo',
    detail: 'A deliberately scoped sandbox, separate from the PostgreSQL reference path.',
  },
  {
    index: '03',
    title: '4-hour expiry target',
    detail: 'Planned expiry boundary; automated cleanup is not enabled yet.',
  },
] as const;

const targetFlow = [
  ['01', 'Accept', 'Signed commerce event'],
  ['02', 'Triage', 'Manager owns the queue'],
  ['03', 'Resolve', 'Agent records the outcome'],
  ['04', 'Trace', 'Audit history stays visible'],
] as const;

export function App() {
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
        <section className="hero" aria-labelledby="entry-title">
          <div className="hero-copy">
            <p className="eyebrow">Ecommerce exception operations</p>
            <h1 id="entry-title">CommerceOps Desk</h1>
            <p className="hero-statement">
              Turn ecommerce exceptions into owned, auditable work.
            </p>
            <p className="hero-detail">
              Explore how payment, refund, fulfilment, and processing exceptions move
              from intake to a clear next action—without connecting a live store.
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
                <button className="button button-primary" type="button" disabled>
                  Enter as Manager
                </button>
                <button className="button button-secondary" type="button" disabled>
                  Enter as Agent
                </button>
              </div>
              <p id="demo-availability" className="availability-note">
                Role workspaces connect in the next implementation slice.
              </p>
            </div>
          </div>

          <aside className="workflow-card" aria-labelledby="workflow-title">
            <div className="workflow-card-header">
              <div>
                <p className="panel-kicker">Target workflow</p>
                <h2 id="workflow-title">One accountable path</h2>
              </div>
              <span className="preview-label">Preview</span>
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
            <p className="panel-kicker">Public sandbox boundaries</p>
            <h2 id="boundaries-title">Clear by design</h2>
          </div>

          <ul className="boundary-list">
            {demoBoundaries.map(({ index, title, detail }) => (
              <li key={index} className="boundary-item">
                <span className="boundary-index" aria-hidden="true">
                  {index}
                </span>
                <div>
                  <h3>{title}</h3>
                  <p>{detail}</p>
                </div>
              </li>
            ))}
          </ul>
        </section>
      </main>

      <footer className="site-footer">
        <p>Built as a verifiable full-stack reference—not a connected merchant system.</p>
      </footer>
    </div>
  );
}
