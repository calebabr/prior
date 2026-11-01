import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import Modal from '../components/Modal.jsx';

// The shared dialog: fixed header (title + dismiss) and a separate scrolling body, so the title never scrolls away
// or sits under the status bar (layout-collisions.e2e.js checks the geometry with simulated safe-area insets).
describe('Modal chrome', () => {
  it('renders the title and a dismiss button in a header, with the content in a scroll body', () => {
    const onClose = vi.fn();
    const { container } = render(
      <Modal title="Fri Oct 30" onClose={onClose}>
        <p>body text</p>
      </Modal>,
    );
    const head = container.querySelector('.modal-head');
    expect(head).toHaveTextContent('Fri Oct 30');
    expect(container.querySelector('.modal-scroll')).toHaveTextContent('body text');
    expect(head.contains(screen.getByRole('button', { name: 'Dismiss dialog' }))).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss dialog' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('has no dismiss button while non-dismissable (onClose undefined)', () => {
    render(
      <Modal title="Working" onClose={undefined}>
        <p>x</p>
      </Modal>,
    );
    expect(screen.queryByRole('button', { name: 'Dismiss dialog' })).toBeNull();
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Working');
  });
});
