import { useState, useSyncExternalStore } from 'react';
import { useStatus } from '../context/StatusContext.jsx';
import { APP_VERSION, isOlder } from '../utils/version.js';
import { subscribe, getReady, applyUpdate } from '../utils/updateSignal.js';

// Version handshake: the server reports the oldest app it supports (min_app_version on /api/status). That is the
// stronger "update required" message. Otherwise, a waiting new bundle shows a softer, dismissible prompt.
// Informational only. It never hides data or blocks an action, and Stop stays in the header.
export default function UpdateBanner({ appVersion = APP_VERSION }) {
  const { status } = useStatus();
  const newBundle = useSyncExternalStore(subscribe, getReady, getReady);
  const [dismissed, setDismissed] = useState(false);
  const min = status && status.min_app_version;
  if (isOlder(appVersion, min)) {
    return (
      <div className="banner banner-update" role="status">
        <span>Update available, reopen the app. You can keep using it meanwhile; Stop still works.</span>
      </div>
    );
  }
  if (!newBundle || dismissed) return null;
  return (
    <div className="banner banner-update" role="status">
      <button type="button" className="banner-tap" onClick={applyUpdate}>
        A new version is ready. Close and reopen the app to update (tap to reload).
      </button>
      <button type="button" className="banner-dismiss" aria-label="Dismiss update notice" onClick={() => setDismissed(true)}>
        Dismiss
      </button>
    </div>
  );
}
