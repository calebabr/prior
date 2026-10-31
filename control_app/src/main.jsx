import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import { initTheme } from './utils/theme.js';
import './index.css';
import { startUpdateChecks } from './utils/updateSignal.js';

// The service worker is registered by vite-plugin-pwa (registerType 'autoUpdate'); startUpdateChecks hooks its
// update events and also checks the deployed bundle, so the update banner can appear.

initTheme();
startUpdateChecks();

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
