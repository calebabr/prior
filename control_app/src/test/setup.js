import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup, configure } from '@testing-library/react';

// Default 1 s findBy/waitFor starves when 40 jsdom files run in parallel; a longer cap only affects failing waits.
configure({ asyncUtilTimeout: 5000 });
// CPU-heavy tests (terminology property sweep, full-page renders) exceed the 5 s default under parallel load.
vi.setConfig({ testTimeout: 30000 });

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.removeItem('prior.stop'); // the last Stop result outlives a page on purpose (services/stopResult.js)
});

if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  });
}

window.scrollTo = () => {};
