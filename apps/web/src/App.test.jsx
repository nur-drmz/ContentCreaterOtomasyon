import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { describe, it, expect, afterEach } from 'vitest';
import App from './App.jsx';

describe('App', () => {
  afterEach(cleanup);

  it('switches between Trend Radar and Publish tabs', () => {
    render(<App />);
    expect(screen.getByRole('button', { name: 'Trend Radar' })).toBeDefined();
    fireEvent.click(screen.getByRole('button', { name: 'Yayın' }));
    expect(screen.getByText('Yayın Yönetimi')).toBeDefined();
  });

  it('opens the Render Atolyesi tab', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: 'Render Atolyesi' }));
    expect(screen.getByText('Render Atolyesi', { selector: 'h2' })).toBeDefined();
    expect(screen.getByRole('button', { name: 'EDL Olustur' })).toBeDefined();
  });
});
