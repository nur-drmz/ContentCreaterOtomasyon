import React, { useEffect, useRef, useState } from 'react';

const API_ROOT = import.meta.env.VITE_API_ROOT ?? '';

const PRESETS = [
  { value: 'active_word_yellow', label: 'Aktif Kelime Sarı' },
  { value: 'classic_white', label: 'Klasik Beyaz' },
  { value: 'lower_third', label: 'Alt Band' },
];

const ALIGNMENTS = [
  { value: 'left', label: 'Sol' },
  { value: 'center', label: 'Orta' },
  { value: 'right', label: 'Sağ' },
];

function deepCopy(obj) {
  return JSON.parse(JSON.stringify(obj));
}

export default function RenderAtelierScreen() {
  const [projectId, setProjectId] = useState('prj_1');
  const [sourcePath, setSourcePath] = useState('source.mp4');
  const [sourceDuration, setSourceDuration] = useState(60);
  const [edl, setEdl] = useState(null);
  const [clipIdx, setClipIdx] = useState(0);
  const [selected, setSelected] = useState(null);
  const [preview, setPreview] = useState(null);
  const [render, setRender] = useState(null);
  const [revNote, setRevNote] = useState('');
  const [saving, setSaving] = useState(false);
  const canvasRef = useRef(null);
  const drag = useRef(null);

  async function loadEdl() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl`, {
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res.ok) {
      setEdl(null);
      return;
    }
    const data = await res.json();
    setEdl(data.edl);
    setClipIdx(0);
  }

  async function buildEdl() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_path: sourcePath,
        source_duration: Number(sourceDuration),
        title: 'Atolye Proje',
      }),
    });
    const data = await res.json();
    setEdl(data.edl);
    setClipIdx(0);
  }

  async function patchClip(index, clip) {
    setSaving(true);
    const payload = {
      start: clip.source_start_sec,
      end: clip.source_end_sec,
      crop: clip.crop,
      zoom: clip.zoom,
      caption_style: clip.caption_style,
      b_roll: clip.b_roll,
    };
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips/${index}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    setEdl(data.edl);
    setSaving(false);
  }

  async function addClip() {
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_path: sourcePath,
        source_start_sec: 0,
        source_end_sec: Number(sourceDuration),
      }),
    });
    const data = await res.json();
    setEdl(data.edl);
    setClipIdx(0);
  }

  async function addBRoll() {
    if (!edl || !edl.clips[clipIdx]) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/clips/${clipIdx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        add_b_roll: {
          media_path: 'broll.mp4',
          start_sec: 0,
          duration_sec: 5,
          opacity: 0.7,
          position_x: 0.5,
          position_y: 0.5,
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

  async function requestRevision() {
    if (!revNote.trim()) return;
    const res = await fetch(`${API_ROOT}/api/projects/${projectId}/edl/revision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: revNote }),
    });
    const data = await res.json();
    setEdl(data.edl);
    setRevNote('');
  }

  function currentClip() {
    if (!edl || !edl.clips[clipIdx]) return null;
    return edl.clips[clipIdx];
  }

  function updateClip(mutator) {
    const clip = currentClip();
    if (!clip) return;
    const next = deepCopy(clip);
    mutator(next);
    patchClip(clipIdx, next);
  }

  function onMouseDown(type, index, e) {
    e.preventDefault();
    e.stopPropagation();
    if (!canvasRef.current) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    setSelected({ type, index });
    const clip = currentClip();
    drag.current = { type, index, x, y, rect };
    if (type === 'crop') {
      drag.current.initial = { ...clip.crop };
    } else if (type === 'caption') {
      drag.current.initial = { ...clip.caption_style };
    } else if (type === 'broll') {
      drag.current.initial = { ...clip.b_roll[index] };
    }
  }

  useEffect(() => {
    function onMove(e) {
      if (!drag.current || !edl) return;
      const { type, index, x, y, rect, initial } = drag.current;
      const dx = (e.clientX - x - rect.left) / rect.width;
      const dy = (e.clientY - y - rect.top) / rect.height;
      const clip = currentClip();
      if (!clip) return;
      const nextEdl = deepCopy(edl);
      const nextClip = nextEdl.clips[clipIdx];
      if (type === 'crop') {
        nextClip.crop.x = Math.min(1 - nextClip.crop.w, Math.max(0, initial.x + dx));
        nextClip.crop.y = Math.min(1 - nextClip.crop.h, Math.max(0, initial.y + dy));
      } else if (type === 'caption') {
        nextClip.caption_style.position_x = Math.min(1, Math.max(0, initial.position_x + dx));
        nextClip.caption_style.position_y = Math.min(1, Math.max(0, initial.position_y + dy));
      } else if (type === 'broll') {
        nextClip.b_roll[index].position_x = Math.min(1, Math.max(0, initial.position_x + dx));
        nextClip.b_roll[index].position_y = Math.min(1, Math.max(0, initial.position_y + dy));
      }
      setEdl(nextEdl);
    }
    function onUp(e) {
      if (!drag.current || !edl) return;
      const { type, index } = drag.current;
      const clip = currentClip();
      if (clip) {
        patchClip(clipIdx, deepCopy(clip));
      }
      drag.current = null;
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [edl, clipIdx, projectId]);

  function input(field, value, onChange, type = 'text') {
    return (
      <label key={field} className="prop">
        {field}
        <input
          type={type}
          value={value}
          onChange={(e) => onChange(type === 'number' ? e.target.valueAsNumber : e.target.value)}
        />
      </label>
    );
  }

  function renderCaptionPanel(clip) {
    const s = clip.caption_style;
    return (
      <>
        <h4>Altyazi Stili</h4>
        {input('Metin', s.sample_text, (v) => updateClip((c) => (c.caption_style.sample_text = v)))}
        {input('Yazi Rengi (ASS)', s.word_color, (v) => updateClip((c) => (c.caption_style.word_color = v)))}
        {input('Aktif Renk (ASS)', s.active_color, (v) => updateClip((c) => (c.caption_style.active_color = v)))}
        {input('Kutu Rengi (ASS)', s.box_color, (v) => updateClip((c) => (c.caption_style.box_color = v)))}
        {input('Cerceve Rengi (ASS)', s.outline_color, (v) => updateClip((c) => (c.caption_style.outline_color = v)))}
        {input('Yazi Boyutu', s.font_size, (v) => updateClip((c) => (c.caption_style.font_size = Number(v))), 'number')}
        {input('Cerceve Kalinligi', s.border_width, (v) => updateClip((c) => (c.caption_style.border_width = Number(v))), 'number')}
        {input('Font', s.font, (v) => updateClip((c) => (c.caption_style.font = v)))}
        <label className="prop">Hiza
          <select value={s.alignment} onChange={(e) => updateClip((c) => (c.caption_style.alignment = e.target.value))}>
            {ALIGNMENTS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
        </label>
        <label className="prop">Preset
          <select value={s.preset} onChange={(e) => updateClip((c) => (c.caption_style.preset = e.target.value))}>
            {PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </label>
        <label className="prop">
          <input type="checkbox" checked={s.shadow} onChange={(e) => updateClip((c) => (c.caption_style.shadow = e.target.checked))} />
          Golge
        </label>
        <label className="prop">
          <input type="checkbox" checked={s.uppercase} onChange={(e) => updateClip((c) => (c.caption_style.uppercase = e.target.checked))} />
          Buyuk Harf
        </label>
      </>
    );
  }

  function renderCropPanel(clip) {
    const c = clip.crop;
    return (
      <>
        <h4>Crop</h4>
        {input('X', c.x, (v) => updateClip((c) => (c.crop.x = Number(v))), 'number')}
        {input('Y', c.y, (v) => updateClip((c) => (c.crop.y = Number(v))), 'number')}
        {input('W', c.w, (v) => updateClip((c) => (c.crop.w = Number(v))), 'number')}
        {input('H', c.h, (v) => updateClip((c) => (c.crop.h = Number(v))), 'number')}
      </>
    );
  }

  function renderZoomPanel(clip) {
    const z = clip.zoom;
    return (
      <>
        <h4>Zoom</h4>
        {input('Scale', z.scale, (v) => updateClip((c) => (c.zoom.scale = Number(v))), 'number')}
        {input('Anchor X', z.anchor_x, (v) => updateClip((c) => (c.zoom.anchor_x = Number(v))), 'number')}
        {input('Anchor Y', z.anchor_y, (v) => updateClip((c) => (c.zoom.anchor_y = Number(v))), 'number')}
      </>
    );
  }

  function renderClipPanel(clip) {
    return (
      <>
        <h4>Klip</h4>
        {input('Baslangic (sn)', clip.source_start_sec, (v) => updateClip((c) => (c.source_start_sec = Number(v))), 'number')}
        {input('Bitis (sn)', clip.source_end_sec, (v) => updateClip((c) => (c.source_end_sec = Number(v))), 'number')}
        <label className="prop">Notlar
          <textarea value={clip.notes || ''} onChange={(e) => updateClip((c) => (c.notes = e.target.value))} />
        </label>
      </>
    );
  }

  function renderBrollPanel(clip, index) {
    const b = clip.b_roll[index];
    return (
      <>
        <h4>B-roll {index + 1}</h4>
        {input('Medya', b.media_path, (v) => updateClip((c) => (c.b_roll[index].media_path = v)))}
        {input('Baslangic (sn)', b.start_sec, (v) => updateClip((c) => (c.b_roll[index].start_sec = Number(v))), 'number')}
        {input('Sure (sn)', b.duration_sec, (v) => updateClip((c) => (c.b_roll[index].duration_sec = Number(v))), 'number')}
        {input('Opaklik', b.opacity, (v) => updateClip((c) => (c.b_roll[index].opacity = Number(v))), 'number')}
        {input('X', b.position_x, (v) => updateClip((c) => (c.b_roll[index].position_x = Number(v))), 'number')}
        {input('Y', b.position_y, (v) => updateClip((c) => (c.b_roll[index].position_y = Number(v))), 'number')}
      </>
    );
  }

  function renderProperties() {
    if (!edl) return <p>EDL yok.</p>;
    const clip = currentClip();
    if (!clip) return <p>Klip yok.</p>;
    if (selected?.type === 'caption') return renderCaptionPanel(clip);
    if (selected?.type === 'crop') return renderCropPanel(clip);
    if (selected?.type === 'zoom') return renderZoomPanel(clip);
    if (selected?.type === 'broll') return renderBrollPanel(clip, selected.index);
    return renderClipPanel(clip);
  }

  function renderCanvas() {
    if (!edl || !edl.clips[clipIdx]) return null;
    const clip = edl.clips[clipIdx];
    const c = clip.crop;
    const s = clip.caption_style;
    return (
      <div
        className="atelier-canvas"
        ref={canvasRef}
        style={{ position: 'relative', width: 360, height: 640, background: '#222' }}
      >
        <div
          className="canvas-hint"
          style={{ position: 'absolute', top: 8, left: 8, color: 'white', fontSize: 12 }}
        >
          9:16 onizleme alani
        </div>
        <div
          className="crop-overlay"
          style={{
            position: 'absolute',
            left: `${c.x * 100}%`,
            top: `${c.y * 100}%`,
            width: `${c.w * 100}%`,
            height: `${c.h * 100}%`,
            border: selected?.type === 'crop' ? '2px solid #00d2ff' : '2px dashed rgba(255,255,255,0.6)',
            background: 'rgba(255,255,255,0.05)',
            cursor: 'move',
          }}
          onMouseDown={(e) => onMouseDown('crop', 0, e)}
        />
        <div
          className="caption-overlay"
          style={{
            position: 'absolute',
            left: `${s.position_x * 100}%`,
            top: `${s.position_y * 100}%`,
            transform: 'translate(-50%, -50%)',
            color: 'white',
            fontSize: 18,
            fontFamily: 'sans-serif',
            padding: 4,
            borderRadius: 4,
            border: selected?.type === 'caption' ? '2px solid #00d2ff' : '2px dashed rgba(255,255,255,0.3)',
            background: 'rgba(0,0,0,0.5)',
            cursor: 'move',
            whiteSpace: 'nowrap',
            userSelect: 'none',
          }}
          onMouseDown={(e) => onMouseDown('caption', 0, e)}
        >
          {s.sample_text}
        </div>
        {clip.b_roll.map((b, i) => (
          <div
            key={i}
            className="broll-overlay"
            style={{
              position: 'absolute',
              left: `${b.position_x * 100}%`,
              top: `${b.position_y * 100}%`,
              transform: 'translate(-50%, -50%)',
              width: 72,
              height: 120,
              background: `rgba(0, 200, 255, ${b.opacity})`,
              border: selected?.type === 'broll' && selected.index === i ? '2px solid #ffdd00' : '1px solid white',
              cursor: 'move',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 10,
              color: 'white',
            }}
            onMouseDown={(e) => onMouseDown('broll', i, e)}
          >
            B{i + 1}
          </div>
        ))}
      </div>
    );
  }

  return (
    <section className="screen atelier">
      <h2>Render Atolyesi</h2>
      <div className="atelier-toolbar">
        <label>
          Proje ID
          <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
        </label>
        <button onClick={loadEdl}>EDL Yükle</button>
        <label>
          Kaynak yolu
          <input value={sourcePath} onChange={(e) => setSourcePath(e.target.value)} />
        </label>
        <label>
          Sure (sn)
          <input type="number" value={sourceDuration} onChange={(e) => setSourceDuration(e.target.value)} />
        </label>
        <button onClick={buildEdl}>EDL Olustur</button>
        <button onClick={addClip} disabled={!edl}>Klip Ekle</button>
        <button onClick={addBRoll} disabled={!edl}>B-roll Ekle</button>
        <button onClick={buildPreview} disabled={!edl || saving}>Preview Komutu</button>
        <button onClick={renderPreview} disabled={!edl || saving}>Render Al</button>
      </div>
      {edl && (
        <div className="atelier-main">
          <div className="atelier-left">
            {edl.clips.length > 1 && (
              <label>
                Klip
                <select value={clipIdx} onChange={(e) => setClipIdx(Number(e.target.value))}>
                  {edl.clips.map((c, i) => (
                    <option key={i} value={i}>{c.clip_id || i}</option>
                  ))}
                </select>
              </label>
            )}
            <p className="hint">Kareyi sürükleyin, alt yaziyi/b-roll'lari hareket ettirin.</p>
            {renderCanvas()}
            {edl.revision_notes && (
              <div className="revision-note">
                <strong>Revizyon Talebi ({edl.revision_requested_by}):</strong>
                <p>{edl.revision_notes}</p>
              </div>
            )}
          </div>
          <div className="atelier-right">
            <h3>Özellikler</h3>
            {edl && (
              <>
                <label className="prop">Baslik
                  <input value={edl.title} onChange={(e) => {
                    const next = deepCopy(edl);
                    next.title = e.target.value;
                    setEdl(next);
                  }} />
                </label>
                <label className="prop">Render Modu
                  <select value={edl.render_mode} onChange={(e) => {
                    const next = deepCopy(edl);
                    next.render_mode = e.target.value;
                    setEdl(next);
                  }}>
                    <option value="center_crop">Center Crop</option>
                    <option value="blurred_background">Bulanik Arka Plan</option>
                  </select>
                </label>
                <label className="prop">Muzik
                  <input value={edl.music_track || ''} onChange={(e) => {
                    const next = deepCopy(edl);
                    next.music_track = e.target.value;
                    setEdl(next);
                  }} />
                </label>
              </>
            )}
            {renderProperties()}
            <hr />
            <h4>Revizyon Talebi</h4>
            <textarea value={revNote} onChange={(e) => setRevNote(e.target.value)} placeholder="Videonun yeniden duzenlenmesi icin not..." />
            <button onClick={requestRevision} disabled={!revNote.trim()}>Revizyon Talebi Gönder</button>
          </div>
        </div>
      )}
      {preview && <pre className="atelier-command">{preview.join(' ')}</pre>}
      {render && (
        <div className="atelier-render">
          <p>returncode: {render.returncode}</p>
          <pre>{render.stderr || render.stdout}</pre>
        </div>
      )}
    </section>
  );
}
