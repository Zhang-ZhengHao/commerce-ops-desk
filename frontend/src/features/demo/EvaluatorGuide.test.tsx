import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EvaluatorGuide } from './EvaluatorGuide';

describe('EvaluatorGuide', () => {
  it('presents the approved five-step journey as instructions, not tracked progress', () => {
    render(<EvaluatorGuide variant="entry" />);

    const guide = screen.getByRole('complementary', {
      name: /five-step evaluator guide/i,
    });
    const orderedList = within(guide).getByRole('list');

    expect(guide.tagName).toBe('ASIDE');
    expect(guide).not.toHaveAttribute('tabindex');
    expect(orderedList.tagName).toBe('OL');
    expect(orderedList).toHaveAttribute('role', 'list');
    expect(within(orderedList).getAllByRole('listitem')).toHaveLength(5);

    for (const title of [
      'Create an event',
      'Test the boundary',
      'Assign the case',
      'Work as Agent',
      'Verify the trail',
    ]) {
      expect(within(orderedList).getByText(title)).toBeVisible();
    }

    for (const visibleControlLabel of [
      'Enter as Manager',
      'Synthetic provider',
      'Deliver new failure',
      'Replay same event',
      'Tamper after signing',
      'Send stale signature',
      'Assign to agent',
      'Update assignment',
      'Switch to Agent',
      'Internal note',
      'Add note',
      'Resolution reason',
      'Resolve case',
      'Event provenance',
      'Accountable timeline',
    ]) {
      expect(orderedList).toHaveTextContent(visibleControlLabel);
    }
    expect(orderedList).toHaveTextContent(/payment\.failed/i);
    expect(orderedList).toHaveTextContent(/Demo Agent/i);
    expect(orderedList).toHaveTextContent(/fictional/i);

    expect(within(guide).queryByRole('button')).not.toBeInTheDocument();
    expect(within(guide).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(guide).queryByRole('progressbar')).not.toBeInTheDocument();
  });
});
