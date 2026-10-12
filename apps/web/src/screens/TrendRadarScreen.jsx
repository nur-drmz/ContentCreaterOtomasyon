import React, { useState } from 'react';

const API_ROOT = import.meta.env.VITE_API_ROOT ?? '';

export default function TrendRadarScreen() {
  const [terms, setTerms] = useState('');
  const [urls, setUrls] = useState('');
  const [projectId, setProjectId] = useState('prj_1');
  const [state, setState] = useState(null);

  async function discover() {
    const payload = {
      terms: terms.split(',').map(s => s.trim()).filter(Boolean),
      manual_urls: urls.split('\n').map(s => s.trim()).filter(Boolean),
    };
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/source-candidates`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    setState(data);
  }

  return (
    <section className="screen">
      <h2>Trend Radar</h2>
      <label>
        Proje ID
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
      </label>
      <label>
        Arama terimleri (virgülle)
        <input value={terms} onChange={(e) => setTerms(e.target.value)} />
      </label>
      <label>
        Elle URL'ler (satır satır)
        <textarea value={urls} onChange={(e) => setUrls(e.target.value)} rows={4} />
      </label>
      <button onClick={discover}>Adayları getir</button>
      <pre>{state ? JSON.stringify(state, null, 2) : 'Henüz aday yok.'}</pre>
    </section>
  );
}
