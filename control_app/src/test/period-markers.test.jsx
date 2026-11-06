import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MarkerList } from '../components/PeriodMarkers.jsx';

const periods = [{ id: 'p1', name: 'Period 1' }];
const markers = [
  { ts: '2026-11-04T12:00:00+00:00', kind: 'release', label: 'Release approved', detail: '2.0.0 (1234567)', changes_trading: true, period_id: 'p1' },
  { ts: '2026-11-03T12:00:00+00:00', kind: 'stop', label: 'Bot stopped', detail: '', changes_trading: false, period_id: null },
];

describe('MarkerList', () => {
  it('renders markers with the trading tag only where flagged', () => {
    render(<MarkerList markers={markers} periods={periods} />);
    expect(screen.getByText('Release approved')).toBeTruthy();
    expect(screen.getByText('2.0.0 (1234567)')).toBeTruthy();
    expect(screen.getAllByText('Trading behaviour changed')).toHaveLength(1);
    expect(screen.getByText('Period 1')).toBeTruthy();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });
  it('shows an empty state', () => {
    render(<MarkerList markers={undefined} periods={[]} />);
    expect(screen.getByTestId('markers-empty')).toBeTruthy();
  });
});
