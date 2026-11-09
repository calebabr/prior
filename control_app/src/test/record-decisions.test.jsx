import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp, stubApi, STATUS } from './helpers.jsx';
import { isMakerKey } from '../utils/strategyConfig.js';

const HELP = 'Saves extra detail about each quoting decision for later analysis. Does not change any orders.';

function cfg(withField) {
  return {
    config: { run_budget: 25, max_order: 8, ...(withField ? { record_decisions: false } : {}), cells: [[0.3, 0.45, 6, 12, 'no']] },
    schema: {
      run_budget: { type: 'number', min: 1, max: 500 },
      max_order: { type: 'number', min: 1 },
      ...(withField ? { record_decisions: { type: 'bool', label: 'Record Decisions', help: HELP } } : {}),
    },
    ceilings: {},
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('Record Decisions setting', () => {
  it('is a Maker key', () => {
    expect(isMakerKey('record_decisions')).toBe(true);
  });

  it('is shown with its help line when the payload includes it', async () => {
    stubApi({ 'GET /api/status': STATUS, 'GET /api/config': cfg(true) });
    renderApp('/settings');
    expect(await screen.findByLabelText(/record decisions/i)).not.toBeChecked();
    expect(screen.getByText(HELP)).toBeInTheDocument();
  });

  it('is hidden when the payload does not include it', async () => {
    stubApi({ 'GET /api/status': STATUS, 'GET /api/config': cfg(false) });
    renderApp('/settings');
    await screen.findByLabelText(/run budget/i);
    expect(screen.queryByLabelText(/record decisions/i)).toBeNull();
  });

  it('saves through the 2FA flow with only the changed field', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      'GET /api/status': STATUS,
      'GET /api/config': cfg(true),
      'PUT /api/config': () => ({ config: { ...cfg(true).config, record_decisions: true }, version: 8 }),
    });
    renderApp('/settings');
    await user.click(await screen.findByLabelText(/record decisions/i));
    await user.click(screen.getByRole('button', { name: /save/i }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    const btns = screen.getAllByRole('button', { name: 'Save' });
    await user.click(btns[btns.length - 1]);
    await screen.findByText(/Saved as version 8/);
    const put = calls.find((c) => c.key === 'PUT /api/config');
    expect(put.headers['X-2FA-Code']).toBe('123456');
    expect(put.body).toEqual({ record_decisions: true });
  });
});
