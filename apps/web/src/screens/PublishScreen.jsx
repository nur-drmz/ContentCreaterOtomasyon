import React, { useState } from 'react';

const API_ROOT = import.meta.env.VITE_API_ROOT ?? '';

export default function PublishScreen() {
  const [projectId, setProjectId] = useState('prj_1');
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);

  async function fetchState() {
    setError(null);
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/publish/state`);
    const data = await res.json();
    if (!res.ok) {
      setError(data.detail || 'Bilinmeyen hata');
      return;
    }
    setState(data.publish_state);
  }

  async function generateMetadata() {
    setError(null);
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/metadata/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok) {
      setError(data.detail || 'Bilinmeyen hata');
      return;
    }
    alert(`Metadata işi başlatıldı: ${data.job.job_id}`);
  }

  return (
    <section className="screen">
      <h2>Yayın Yönetimi</h2>
      <label>
        Proje ID
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
      </label>
      <button onClick={fetchState}>Yayın durumunu getir</button>
      <button onClick={generateMetadata}>Metadata üret</button>
      {error && <p className="error">{error}</p>}
      <pre>{state ? JSON.stringify(state, null, 2) : 'Durum yüklenmedi.'}</pre>
    </section>
  );
}
