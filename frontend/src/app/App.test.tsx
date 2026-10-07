import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from './App';

describe('CommerceOps Desk entry shell', () => {
  it('states the product name and operational value', () => {
    render(<App />);

    expect(
      screen.getByRole('heading', { level: 1, name: 'CommerceOps Desk' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/turn ecommerce exceptions into owned, auditable work/i),
    ).toBeInTheDocument();
  });

  it('discloses every public demo boundary', () => {
    render(<App />);

    expect(screen.getByText(/synthetic data only/i)).toBeInTheDocument();
    expect(screen.getByText(/single-node SQLite demo/i)).toBeInTheDocument();
    expect(screen.getByText(/expires after 4 hours/i)).toBeInTheDocument();
  });

  it('offers accessible Manager and Agent entry actions', () => {
    render(<App />);

    expect(
      screen.getByRole('button', { name: /enter as manager/i }),
    ).toBeVisible();
    expect(
      screen.getByRole('button', { name: /enter as agent/i }),
    ).toBeVisible();
  });
});
