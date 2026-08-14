import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import App from './App.jsx';

describe('App', () => {
  it('switches between Trend Radar and Publish tabs', () => {
    render(<App />);
    expect(screen.getByText('Trend Radar')).toBeDefined();
    fireEvent.click(screen.getByText('Yayın'));
    expect(screen.getByText('Yayın Yönetimi')).toBeDefined();
  });
});
