import React, { useState } from 'react';
import TrendRadarScreen from './screens/TrendRadarScreen.jsx';
import PublishScreen from './screens/PublishScreen.jsx';
import SceneEditorScreen from './screens/SceneEditorScreen.jsx';

export default function App() {
  const [tab, setTab] = useState('trend');

  return (
    <div className="app">
      <nav className="tabs">
        <button onClick={() => setTab('trend')} aria-pressed={tab === 'trend'}>
          Trend Radar
        </button>
        <button onClick={() => setTab('editor')} aria-pressed={tab === 'editor'}>
          Sahne Editörü
        </button>
        <button onClick={() => setTab('publish')} aria-pressed={tab === 'publish'}>
          Yayın
        </button>
      </nav>
      <main>
        {tab === 'trend' && <TrendRadarScreen />}
        {tab === 'editor' && <SceneEditorScreen />}
        {tab === 'publish' && <PublishScreen />}
      </main>
    </div>
  );
}
