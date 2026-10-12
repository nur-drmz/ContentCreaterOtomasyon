import React, { useState } from 'react';

const API_ROOT = import.meta.env.VITE_API_ROOT ?? '';

export default function SceneEditorScreen() {
  const [projectId, setProjectId] = useState('prj_1');
  const [sourcePath, setSourcePath] = useState('source.mp4');
  const [sourceDuration, setSourceDuration] = useState(60);
  const [title, setTitle] = useState('Sahne Edit');
  const [edl, setEdl] = useState(null);
  const [preview, setPreview] = useState(null);
  const [render, setRender] = useState(null);

  async function buildEdl() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_path: sourcePath,
        source_duration: Number(sourceDuration),
        title,
      }),
    });
    const data = await res.json();
    setEdl(data.edl);
  }

  async function trimClip() {
    if (!edl) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips/0`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start: 5, end: 30 }),
    });
    const data = await res.json();
    setEdl(data.edl);
  }

  async function setCrop() {
    if (!edl) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips/0`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ crop: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 } }),
    });
    const data = await res.json();
    setEdl(data.edl);
  }

  async function addClip() {
    if (!edl) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_path: sourcePath,
        source_start_sec: 0,
        source_end_sec: 15,
      }),
    });
    const data = await res.json();
    setEdl(data.edl);
  }

  async function addBRoll() {
    if (!edl) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips/0`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        add_b_roll: {
          media_path: 'broll.mp4',
          start_sec: 0,
          duration_sec: 5,
          opacity: 0.7,
        },
      }),
    });
    const data = await res.json();
    setEdl(data.edl);
  }

  async function buildPreview() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_name: 'preview.mp4' }),
    });
    const data = await res.json();
    setPreview(data.preview_command);
  }

  async function renderPreview() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_name: 'preview.mp4' }),
    });
    const data = await res.json();
    setRender(data);
  }

  return (
    <section className="screen">
      <h2>Sahne Editörü</h2>
      <label>
        Proje ID
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
      </label>
      <label>
        Kaynak yolu
        <input value={sourcePath} onChange={(e) => setSourcePath(e.target.value)} />
      </label>
      <label>
        Kaynak süresi (sn)
        <input type="number" value={sourceDuration} onChange={(e) => setSourceDuration(e.target.value)} />
      </label>
      <label>
        Başlık
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <button onClick={buildEdl}>EDL Oluştur</button>
      <button onClick={addClip} disabled={!edl}>Klip Ekle</button>
      <button onClick={trimClip} disabled={!edl}>5-30 sn Kırp</button>
      <button onClick={setCrop} disabled={!edl}>Orta Kırp</button>
      <button onClick={addBRoll} disabled={!edl}>B-roll Ekle</button>
      <button onClick={buildPreview} disabled={!edl}>Preview Komutu Üret</button>
      <button onClick={renderPreview} disabled={!edl}>Gerçek ffmpeg Render Al</button>
      <pre>{edl ? JSON.stringify(edl, null, 2) : 'EDL yok.'}</pre>
      {preview && <pre>{preview.join(' ')}</pre>}
      {render && (
        <div>
          <p>returncode: {render.returncode}</p>
          <pre>{render.stderr || render.stdout}</pre>
        </div>
      )}
    </section>
  );
}
