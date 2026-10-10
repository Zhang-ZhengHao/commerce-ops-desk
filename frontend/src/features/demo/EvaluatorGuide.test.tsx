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

    expect(orderedList).toHaveTextContent(/enter as Manager/i);
    expect(orderedList).toHaveTextContent(/fresh payment\.failed event/i);
    expect(orderedList).toHaveTextContent(/tamper with one signed byte/i);
    expect(orderedList).toHaveTextContent(/assign it to Demo Agent/i);
    expect(orderedList).toHaveTextContent(/fictional internal note/i);
    expect(orderedList).toHaveTextContent(/safe provenance/i);
    expect(orderedList).toHaveTextContent(/ordered audit history/i);

    expect(within(guide).queryByRole('button')).not.toBeInTheDocument();
    expect(within(guide).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(guide).queryByRole('progressbar')).not.toBeInTheDocument();
  });
});
