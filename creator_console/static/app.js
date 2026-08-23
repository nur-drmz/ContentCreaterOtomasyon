/* Creator Console frontend.
 *
 * DELIBERATELY THIN. Every decision the UI displays -- whether a phase may run,
 * whether an approval is still valid, which mode is recommended, whether a job
 * can be retried -- is computed on the SERVER and arrives as a plain boolean or
 * string in the view model. No pipeline logic is reimplemented here, and this
 * file never sees an API key: the server only ever sends `configured`/`missing`.
 *
 * State lives in the backend, so a browser refresh restores the exact same view.
 */

const api = {
  async request(method, path, { body, headers } = {}) {
    const requestId = `web-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
    const controller = new AbortController();
    const timeoutMs = 30000;
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const options = {
      method,
      headers: { 'X-Request-ID': requestId, ...(headers || {}) },
      signal: controller.signal,
    };
    if (body !== undefined && !(body instanceof ArrayBuffer) && !(body instanceof Blob)) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    } else if (body !== undefined) {
      options.body = body;
    }
    const started = performance.now();
    console.info(`[ui] ${requestId} START ${method} ${path}`);
    try {
      const response = await fetch(path, options);
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await response.json() : null;
      if (!response.ok) {
        const error = (payload && payload.error) || {};
        const failure = new Error(error.message || `HTTP ${response.status}`);
        failure.hint = error.hint || '';
        failure.code = error.code || `http_${response.status}`;
        failure.status = response.status;
        failure.debugId = error.debug_id || requestId;
        // Which form fields the server rejected, so the UI can mark them.
        failure.fields = error.fields || [];
        throw failure;
      }
      console.info(`[ui] ${requestId} END ${response.status} ${Math.round(performance.now() - started)}ms`);
      return payload;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        const failure = new Error(`Sunucu ${timeoutMs / 1000} saniye icinde yanit vermedi.`);
        failure.code = 'request_timeout';
        failure.debugId = requestId;
        failure.hint = `Sayfayi yenileyip tekrar dene. Debug istegi: ${requestId}`;
        console.error(`[ui] ${requestId} TIMEOUT ${method} ${path}`);
        throw failure;
      }
      console.error(`[ui] ${requestId} ERROR ${method} ${path}`, error);
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  },
  get(path) { return this.request('GET', path); },
  post(path, body, headers) { return this.request('POST', path, { body, headers }); },
};

function uploadSourceFile(file) {
  const uploadRules = ((state.project || {}).upload_limits || {});
  const limit = Number(uploadRules.max_bytes || 0);
  const supported = (uploadRules.supported_extensions || []).map(value => String(value).toLowerCase());
  if (!file || file.size <= 0) {
    const error = new Error('Secilen video bos veya tarayici tarafindan okunamiyor.');
    error.hint = 'Dosyanin bilgisayarda tamamen indirildigini ve boyutunun 0 olmadigini kontrol et.';
    return Promise.reject(error);
  }
  const dot = file.name.lastIndexOf('.');
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
  if (supported.length && !supported.includes(extension)) {
    const error = new Error(`Bu video uzantisi desteklenmiyor: ${extension || 'uzanti yok'}.`);
    error.hint = `Desteklenen kaynaklar: ${supported.join(', ')}`;
    return Promise.reject(error);
  }
  if (limit > 0 && file.size > limit) {
    const error = new Error(`Video yukleme sinirini asiyor (${formatBytes(file.size)}).`);
    error.hint = `Bu kurulumun siniri ${formatBytes(limit)}.`;
    return Promise.reject(error);
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    state.uploadRequest = xhr;
    state.uploadProgress = { loaded: 0, total: file.size, filename: file.name };
    let paintScheduled = false;
    const repaint = () => {
      if (paintScheduled) return;
      paintScheduled = true;
      requestAnimationFrame(() => { paintScheduled = false; render(); });
    };
    const cleanup = () => {
      state.uploadRequest = null;
      state.uploadProgress = null;
      repaint();
    };
    xhr.open('POST', `/api/projects/${state.projectId}/source`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Actor', projectActor());
    xhr.upload.onprogress = (event) => {
      state.uploadProgress = {
        loaded: event.loaded, total: event.lengthComputable ? event.total : file.size,
        filename: file.name,
      };
      repaint();
    };
    xhr.onload = () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) { payload = null; }
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
        return;
      }
      const info = (payload && payload.error) || {};
      const error = new Error(info.message || `HTTP ${xhr.status}`);
      error.hint = info.hint || '';
      error.code = info.code || `http_${xhr.status}`;
      error.status = xhr.status;
      error.fields = info.fields || [];
      reject(error);
    };
    xhr.onerror = () => {
      cleanup();
      const error = new Error('Video yuklenirken baglanti kesildi.');
      error.hint = 'Console acik kalirken yeniden dene; yarim dosya otomatik temizlenir.';
      reject(error);
    };
    xhr.onabort = () => {
      cleanup();
      const error = new Error('Video yuklemesi kullanici tarafindan durduruldu.');
      error.hint = 'Yarim dosya kaydedilmedi; istedigin zaman yeniden baslatabilirsin.';
      reject(error);
    };
    xhr.send(file);
    repaint();
  });
}

const state = {
  screen: 'projects',
  workspace: null,
  project: null,
  projectId: new URLSearchParams(location.search).get('project') || null,
  selectedMode: null,
  // Pre-plan language choice. The plan is the source of truth once it exists.
  selectedLanguage: 'en-US',
  selectedPreset: 'deep_storyteller',
  selectedMoments: new Set(),
  alerts: [],
  polling: null,
  autoBusy: false,
  longFormAutoBusy: false,
  // The LAST diagnostics report, fetched from the server (never recomputed in
  // the browser). Null until the Settings screen asks for it.
  diagnostics: null,
  diagnosticsBusy: false,
  // Transcript editing drafts. UI-local and kept in state on purpose: the
  // job poll re-renders every 1.5s and anything typed into a bare DOM node
  // would be wiped mid-sentence.
  transcriptDraft: null,
  longFormStudio: null,
  turboStudio: null,
  turboDraft: null,
  longFormDraft: null,
  revision: null,
  revisionStage: null,
  revisionNotes: '',
  transcriptFullDraft: null,
  glossaryDraft: null,
  // Which (project, slot) each draft above belongs to. A mismatch clears it.
  transcriptDraftOwner: null,
  transcriptFullDraftOwner: null,
  glossaryDraftOwner: null,
  uploadRequest: null,
  uploadProgress: null,
  // Manual run parameters are UI-local (they are inputs), but every RESULT and
  // the automation flow itself live on the server.
  runParams: {
    video_analysis: { focus_region: 'start', clip_duration_min: 10,
                      custom_start_min: 0,
                      whisper_model_size: 'small', scene_detector: 'content', top_n: 5 },
    tts: { voice: 'Kore', language_code: 'tr-TR', narration_direction: '' },
  },
};

function projectActor() {
  const record = (((state.project || {}).project || {}).data) || {};
  return String((state.project && state.project.project_actor)
                || record.operator_name || record.created_by || 'console').trim();
}

const SCREENS = [
  { key: 'projects',  label: 'Projeler',        needsProject: false },
  { key: 'history',   label: 'Kayitlar',        needsProject: false },
  { key: 'phase0',    label: 'Faz 0 · Strateji', needsProject: true },
  { key: 'longform',  label: 'Uzun Video',       needsProject: true },
  { key: 'turbo',     label: 'Turbo Studio',     needsProject: true },
  { key: 'jobs',      label: 'Is akisi',         needsProject: true },
  { key: 'moments',   label: 'Moment Explorer',  needsProject: true },
  { key: 'script',    label: 'Script Studio',    needsProject: true },
  { key: 'voice',     label: 'Voice Studio',     needsProject: true },
  { key: 'render',    label: 'Render Studio',    needsProject: true },
  { key: 'outputs',   label: 'Ciktilar',         needsProject: true },
  { key: 'settings',  label: 'Ayarlar',          needsProject: false },
];

/* ------------------------------------------------------- form primitives */
/* One field builder for the whole console, so "required" cannot be marked in
 * one form and forgotten in the next. It renders the orange asterisk, the
 * aria-required flag, the "Optional" tag and the inline error slot together --
 * they are one contract, not four independent decorations.
 *
 * Client validation is a COURTESY. The server rejects the same input on its own
 * (see `_required` in api.py); nothing here is load-bearing for correctness. */
function field(spec) {
  const { id, label, required = false, hint = '', control } = spec;
  return el('div', {
    class: `field${required ? ' field--required' : ''}`,
    'data-field': id,
  },
    el('label', { for: id },
      label,
      required
        ? el('span', { class: 'field__star', 'aria-hidden': 'true',
                       'data-testid': `req-${id}`, text: ' *' })
        : el('span', { class: 'field__optional', 'data-testid': `opt-${id}`,
                       text: ' (Opsiyonel)' })),
    control,
    hint ? el('div', { class: 'field__hint', text: hint }) : null,
    el('div', { class: 'field__error', id: `${id}-error`, 'data-testid': `err-${id}`,
                role: 'alert', hidden: true }));
}

function inputField(spec) {
  const { id, required = false, type = 'text', ...rest } = spec;
  const attrs = { id, type, ...rest.attrs };
  if (required) { attrs['aria-required'] = 'true'; attrs['aria-describedby'] = `${id}-error`; }
  return field({ ...spec, control: el('input', attrs) });
}

function textareaField(spec) {
  const { id, required = false } = spec;
  const attrs = { id, ...spec.attrs };
  if (required) { attrs['aria-required'] = 'true'; attrs['aria-describedby'] = `${id}-error`; }
  return field({ ...spec, control: el('textarea', attrs, spec.value || '') });
}

function requiredLegend() {
  return el('p', { class: 'field-legend', 'data-testid': 'required-legend' },
    el('span', { class: 'field__star', 'aria-hidden': 'true', text: '*' }),
    ' Zorunlu alan');
}

function clearFieldErrors(ids) {
  for (const id of ids) {
    const slot = document.getElementById(`${id}-error`);
    const input = document.getElementById(id);
    if (slot) { slot.hidden = true; slot.textContent = ''; }
    if (input) { input.removeAttribute('aria-invalid'); input.classList.remove('is-invalid'); }
  }
}

function markFieldError(id, message) {
  const slot = document.getElementById(`${id}-error`);
  const input = document.getElementById(id);
  if (slot) { slot.hidden = false; slot.textContent = message; }
  if (input) { input.setAttribute('aria-invalid', 'true'); input.classList.add('is-invalid'); }
}

/* Validate, show every error at once, and focus the FIRST invalid field.
 * `rules` is [{ id, label, required, min, max, minLength }].
 * Returns the trimmed values, or null when something is invalid -- so a caller
 * cannot accidentally proceed with a partial form. */
function validateFields(rules) {
  clearFieldErrors(rules.map(rule => rule.id));
  const values = {};
  const invalid = [];
  for (const rule of rules) {
    const input = document.getElementById(rule.id);
    if (!input) continue;
    const raw = String(input.value ?? '').trim();
    if (!raw) {
      if (rule.required) {
        markFieldError(rule.id, `${rule.label} zorunlu.`);
        invalid.push(rule.id);
      } else {
        // Empty AND optional means "not provided" -- never a silent default.
        values[rule.id] = null;
      }
      continue;
    }
    if (rule.minLength && raw.length < rule.minLength) {
      markFieldError(rule.id, `${rule.label} en az ${rule.minLength} karakter olmali.`);
      invalid.push(rule.id);
      continue;
    }
    if (rule.number) {
      const numeric = Number(raw);
      if (!Number.isFinite(numeric)) {
        markFieldError(rule.id, `${rule.label} sayi olmali.`);
        invalid.push(rule.id);
        continue;
      }
      if (rule.min !== undefined && numeric < rule.min
          || rule.max !== undefined && numeric > rule.max) {
        markFieldError(rule.id, `${rule.label} ${rule.min}-${rule.max} araliginda olmali.`);
        invalid.push(rule.id);
        continue;
      }
      values[rule.id] = numeric;
      continue;
    }
    values[rule.id] = raw;
  }
  if (invalid.length) {
    const first = document.getElementById(invalid[0]);
    if (first) { first.focus(); first.scrollIntoView({ block: 'center' }); }
    pushAlert('danger', 'Eksik veya gecersiz alan',
      `${invalid.length} alan duzeltilmeli.`, 'Yildizli (*) alanlar zorunludur.');
    return null;
  }
  return values;
}

/* The server names the field it rejected (closed catalog, never an echo of the
 * value). Surfacing it here is what makes server validation authoritative
 * WITHOUT the user having to hunt for what went wrong. */
function applyServerFieldErrors(error) {
  const fields = (error && error.fields) || [];
  if (!fields.length) return false;
  const aliases = { title: 'new-project-title', operator_name: 'new-project-operator' };
  const fieldId = (name) => document.getElementById(name) ? name : (aliases[name] || name);
  for (const item of fields) markFieldError(fieldId(item.name), `${item.label} zorunlu.`);
  const first = document.getElementById(fieldId(fields[0].name));
  if (first) { first.focus(); first.scrollIntoView({ block: 'center' }); }
  return true;
}

/* ------------------------------------------------------------------ dom */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const STATUS_ICONS = {
  queued: '◔', running: '◑', review_required: '⚑', failed: '✕', completed: '✓',
  approved: '✓', missing: '·', planned: '◇',
  // Batch slot states. Each carries its own mark so the board never relies on
  // colour alone to say what a Short is waiting for.
  needs_moment: '◇', ready_to_render: '▶', needs_review: '⚑', needs_revision: '✎',
};

function badge(status, label) {
  const key = String(status || 'missing');
  return el('span', { class: `badge badge--${key}`, 'data-icon': STATUS_ICONS[key] || '·' },
    label || key);
}

function alertBox(kind, title, message, hint) {
  return el('div', { class: `alert alert--${kind}`, role: kind === 'danger' ? 'alert' : 'status' },
    el('div', { class: 'alert__title', text: title }),
    message ? el('div', { text: message }) : null,
    hint ? el('div', { class: 'alert__hint', text: hint }) : null);
}

function pushAlert(kind, title, message, hint) {
  state.alerts = [{ kind, title, message, hint }];
  renderAlerts();
  document.getElementById('live-status').textContent = `${title}. ${message || ''}`;
}

function renderAlerts() {
  const host = document.getElementById('global-alerts');
  host.replaceChildren(...state.alerts.map(a => alertBox(a.kind, a.title, a.message, a.hint)));
}

async function guard(action, successTitle) {
  try {
    const result = await action();
    if (successTitle) pushAlert('ok', successTitle);
    return result;
  } catch (error) {
    // A field-level rejection is shown ON the field, not only in a banner: the
    // banner tells you something is wrong, the field tells you which one.
    if (applyServerFieldErrors(error)) {
      pushAlert('danger', 'Eksik zorunlu alan', error.message, error.hint);
      return null;
    }
    // Actionable by construction: the server always sends a hint with failures
    // the user can do something about.
    pushAlert('danger', 'Islem basarisiz', error.message, error.hint);
    return null;
  }
}

/* -------------------------------------------------------------- screens */
function screenProjects() {
  const ws = state.workspace;
  const projects = (ws && ws.projects) || [];
  const storage = (ws && ws.storage_policy) || {};
  return el('div', {},
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { text: 'Projeler' }),
        el('p', { class: 'subtitle',
          text: 'Aktif projeler hizli calisma diskinde, tamamlananlar G arsivinde tutulur.' })),
    ),
    el('div', { class: 'card storage-policy-card', 'data-testid': 'storage-policy' },
      el('div', { class: 'storage-policy-flow' },
        el('div', { class: 'storage-policy-node' },
          el('div', { class: 'dim', text: 'AKTIF CALISMA' }),
          el('strong', { text: storage.working_location_name || 'C surucusu / aktif calisma' }),
          el('div', { class: 'dim', text: 'Uzerinde calistigin proje burada acilir.' })),
        el('div', { class: 'storage-policy-arrow', text: 'Arsivle  →' }),
        el('div', { class: 'storage-policy-node' },
          el('div', { class: 'dim', text: 'UZUN SURELI SAKLAMA' }),
          el('strong', { text: storage.archive_location_name || 'G surucusu / Youtube kaynak videolar' }),
          el('div', { class: 'dim',
            text: storage.archive_available
              ? 'Arsiv diski bagli ve kullanima hazir.'
              : 'Arsiv diski su anda kullanilamiyor.' }))),
      el('p', { class: 'subtitle storage-policy-note',
        text: 'Arsivleme kopya birakmaz: proje aktif listeden cikar ve G diskine tasinir.' })),
    el('div', { class: 'card' },
      el('h2', { text: 'Yeni proje' }),
      requiredLegend(),
      inputField({ id: 'new-project-title', label: 'Proje adi', required: true,
                   attrs: { placeholder: 'Ornek: HOTD sezon analizi', maxlength: 200 } }),
      inputField({ id: 'new-project-operator', label: 'Ureten kisi', required: true,
                   hint: 'Bir kez kaydedilir; plan, secim, onay ve revizyonlarda otomatik kullanilir.',
                   attrs: { placeholder: 'Ornek: Bix', maxlength: 120,
                            autocomplete: 'name' } }),
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'create-project',
        onclick: async (event) => {
          const values = validateFields([
            { id: 'new-project-title', label: 'Proje adi', required: true },
            { id: 'new-project-operator', label: 'Ureten kisi', required: true }]);
          if (!values) return;
          const button = event.currentTarget;
          button.disabled = true;   // double-click guard on the client too
          const created = await guard(
            () => api.post('/api/projects', {
              title: values['new-project-title'],
              operator_name: values['new-project-operator'],
            }),
            'Proje olusturuldu');
          button.disabled = false;
          if (created) { state.projectId = created.project.project_id; await goto('phase0'); }
        },
      }, 'Proje olustur'),
    ),
    el('div', { class: 'card' },
      el('h2', { text: 'Mevcut projeler' }),
      projects.length === 0
        ? el('div', { class: 'empty', text: 'Henuz proje yok.' })
        : el('div', { class: 'table-scroll' }, el('table', {},
            el('thead', {}, el('tr', {},
              el('th', { text: 'Proje' }), el('th', { text: 'Kaynak' }),
              el('th', { text: 'Olusturuldu' }), el('th', { text: '' }))),
            el('tbody', {}, projects.map(project => el('tr', {},
              el('td', {}, el('button', {
                class: 'project-link', type: 'button', disabled: Boolean(project.problem),
                onclick: () => openProject(project.project_id),
              }, project.title),
                project.problem ? el('div', { class: 'dim', text: project.problem }) : null),
              el('td', {}, project.has_source ? badge('completed', 'yuklendi') : badge('missing', 'yok')),
              el('td', { class: 'dim', text: project.created_at || '-' }),
              el('td', {}, el('button', {
                class: 'btn btn--sm',
                disabled: Boolean(project.problem),
                onclick: () => openProject(project.project_id),
              }, 'Ac')))))))
    ));
}

function newRunRequestId(jobType) {
  const random = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${jobType}-${random}`;
}

function submitManualJob(jobType, params = {}) {
  return api.post(`/api/projects/${state.projectId}/jobs`, {
    job_type: jobType,
    params,
    run_request_id: newRunRequestId(jobType),
    submitted_by: projectActor(),
  });
}

async function automationAction(action) {
  if (!state.projectId || state.autoBusy) return null;
  state.autoBusy = true;
  render();
  try {
    return await guard(
      () => api.post(`/api/projects/${state.projectId}/automation`, {
        action, actor: projectActor(),
      }));
  } finally {
    // A network timeout or an unexpected refresh failure must never leave the
    // whole automation toolbar permanently disabled.
    state.autoBusy = false;
    render();
    await refresh();
  }
}


/* The pipeline workflow map: Kaynak → Degerlendirme → Plan → Analiz → Moment →
 * Script → Ses → Render → Inceleme.
 *
 * Purely presentational. Which step is current, which is skipped for the chosen
 * mode, what the next action is and why something is blocked ALL arrive already
 * decided from `/api/projects/:id` -- this function chooses colours and layout
 * and nothing else. Re-deriving any of it here would let the map drift out of
 * agreement with the pipeline it describes. */
const STEP_STATE_LABEL = {
  completed: 'tamamlandi', current: 'su an', waiting: 'bekliyor',
  blocked: 'engelli', failed: 'basarisiz', skipped: 'atlandi',
};
const STEP_STATE_MARK = {
  completed: '✓', current: '▸', waiting: '·', blocked: '!', failed: '✕', skipped: '–',
};

function workflowStep(step, index) {
  const skipped = step.state === 'skipped';
  return el('li', { class: 'wf__item' },
    el('button', {
      class: `wf-step wf-step--${step.state}`, type: 'button',
      'data-testid': `step-${step.key}`, 'data-state': step.state,
      'aria-current': step.state === 'current' ? 'step' : null,
      // Skipped steps stay focusable and explain themselves rather than going
      // dead: "why is this greyed out?" is the question they must answer.
      title: step.blocker || step.action || step.skipped_reason || step.long_label,
      onclick: () => goto(step.tab),
    },
      el('span', { class: 'wf-step__mark', 'aria-hidden': 'true',
                   text: STEP_STATE_MARK[step.state] || '·' }),
      el('span', { class: 'wf-step__text' },
        el('span', { class: 'wf-step__index', text: `${index + 1}` }),
        el('span', { class: 'wf-step__label', text: step.label }),
        el('span', { class: 'wf-step__state', text: STEP_STATE_LABEL[step.state] || step.state }),
        step.detail
          ? el('span', { class: 'wf-step__detail', text: step.detail }) : null,
        skipped
          ? el('span', { class: 'wf-step__detail', 'data-testid': `step-skip-${step.key}`,
                         text: 'bu modda yok' }) : null)));
}

function workflowMap() {
  const workflow = (state.project && state.project.workflow) || {};
  const steps = workflow.steps || [];
  if (!steps.length) return null;
  const currentState = workflow.current_state || 'completed';
  return el('section', { class: 'wf', 'aria-label': 'Uretim akis haritasi',
                         'data-testid': 'workflow-map' },
    el('div', { class: 'wf__head' },
      el('strong', { text: 'Uretim akisi' }),
      el('span', { class: 'wf__meta', 'data-testid': 'workflow-count',
                   text: `${workflow.done_count}/${workflow.total_count} adim `
                         + `(%${workflow.percent})` }),
      workflow.mode
        ? el('span', { class: 'wf__mode', 'data-testid': 'workflow-mode',
                       text: workflow.mode })
        : el('span', { class: 'wf__mode wf__mode--pending', 'data-testid': 'workflow-mode',
                       text: 'mod secilmedi' })),
    el('div', { class: 'wf__track' },
      el('div', { class: 'wf__fill', style: `width:${workflow.percent}%` })),
    el('ol', { class: 'wf__steps', 'data-testid': 'workflow-steps' },
      steps.map(workflowStep)),
    el('div', { class: `wf__now wf__now--${currentState}`, 'data-testid': 'workflow-now' },
      el('div', { class: 'wf__now-body' },
        el('div', { class: 'wf__now-title',
                    text: workflow.complete ? 'Tum adimlar tamamlandi.'
                                            : `Su an: ${workflow.current_label}` }),
        workflow.current_action
          ? el('div', { class: 'dim', 'data-testid': 'workflow-action',
                        text: workflow.current_action })
          : null,
        workflow.current_blocker
          ? el('div', { class: 'wf__blocker', 'data-testid': 'workflow-blocker',
                        text: `Engel: ${workflow.current_blocker}` })
          : null,
        workflow.current_repair
          ? el('div', { class: 'wf__repair', 'data-testid': 'workflow-repair',
                        text: `Cozum: ${workflow.current_repair}` })
          : null),
      workflow.complete ? null : el('button', {
        class: 'btn btn--sm', 'data-testid': 'workflow-goto',
        onclick: () => goto(workflow.current_tab),
      }, 'Bu adima git')),
    automationBar());
}

function preflightRow(check) {
  return el('div', { class: `preflight-row preflight-row--${check.status}` },
    badge(check.status === 'ok' ? 'completed'
          : check.status === 'warn' ? 'review_required' : 'failed', check.status),
    el('div', {},
      el('strong', { text: check.label }),
      el('div', { class: 'dim', text: check.detail })),
    el('span', { class: 'dim', text: '' }),
    check.fix ? el('div', { class: 'preflight-row__fix', text: `Cozum: ${check.fix}` }) : null);
}

function preflightPanel() {
  const preflight = (state.project && state.project.preflight)
    || (state.workspace && state.workspace.preflight) || {};
  const checks = preflight.checks || [];
  if (!checks.length) return null;
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Yerel ortam kontrolu' }),
      badge(preflight.ok ? 'completed' : 'failed',
            preflight.ok ? 'hazir' : `${preflight.fail_count} engel`)),
    el('div', { class: 'dim', 'data-testid': 'preflight-summary',
                style: 'margin-bottom:12px', text: preflight.summary || '' }),
    el('div', { class: 'preflight-list', 'data-testid': 'preflight-list' },
      checks.map(preflightRow)));
}

function preflightBanner() {
  const preflight = (state.project && state.project.preflight)
    || (state.workspace && state.workspace.preflight) || {};
  const failures = (preflight.checks || []).filter(check => check.status === 'fail');
  if (!failures.length) return null;
  return alertBox('danger', 'Yerel ortam eksik',
    failures.map(check => `${check.label}: ${check.detail}`).join(' · '),
    'Ayarlar sekmesindeki "Yerel ortam kontrolu" bolumunde cozum adimlari var.');
}

/* Diagnostics. Loaded lazily and never on page render: a run does real ffmpeg
 * work, so it happens only when the user asks for it. */
const DIAG_BADGE = {
  ready: 'completed', degraded: 'review_required', blocked: 'queued',
  not_configured: 'missing', failed: 'failed',
};
const DIAG_LABEL = {
  ready: 'calisiyor', degraded: 'kisitli', blocked: 'engelli',
  not_configured: 'yapilandirilmamis', failed: 'bozuk',
};

function diagnosticsRow(capability) {
  const status = capability.status;
  return el('div', {
    class: `diag-row diag-row--${status || 'unknown'}`,
    'data-testid': `diag-${capability.key}`, 'data-status': status || '',
  },
    badge(DIAG_BADGE[status] || 'missing', DIAG_LABEL[status] || 'calistirilmadi'),
    el('div', { class: 'diag-row__body' },
      el('strong', { text: capability.label }),
      capability.summary
        ? el('div', { class: 'dim', text: capability.summary })
        : el('div', { class: 'dim', text: 'Bu yetenek henuz test edilmedi.' }),
      capability.blocker
        ? el('div', { class: 'diag-row__blocker', 'data-testid': `diag-blocker-${capability.key}`,
                      text: `Engel: ${capability.blocker}` })
        : null,
      capability.repair
        ? el('div', { class: 'diag-row__repair', 'data-testid': `diag-repair-${capability.key}`,
                      text: `Cozum: ${capability.repair}` })
        : null),
    el('div', { class: 'diag-row__aside' },
      capability.duration_sec !== null && capability.duration_sec !== undefined
        ? el('span', { class: 'dim mono', text: `${capability.duration_sec}s` }) : null,
      capability.tab && status && status !== 'ready'
        ? el('button', { class: 'btn btn--sm', type: 'button',
                         'data-testid': `diag-open-${capability.key}`,
                         onclick: () => goto(capability.tab) }, 'Sekmeye git')
        : null));
}

function diagnosticsPanel() {
  const report = state.diagnostics;
  const capabilities = (report && report.capabilities) || [];
  const counts = (report && report.counts) || {};
  const neverRun = !report || !report.last_run_at;
  return el('div', { class: 'card', 'data-testid': 'diagnostics-panel' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Yetenek teshisi' }),
      report && report.overall
        ? badge(DIAG_BADGE[report.overall] || 'missing', DIAG_LABEL[report.overall] || report.overall)
        : badge('missing', 'calistirilmadi')),
    el('p', { class: 'dim', text: 'Her yetenek, gecici ve izole bir kopya uzerinde GERCEKTEN '
      + 'calistirilarak test edilir. Kendi projelerin okunmaz, degistirilmez; '
      + 'hicbir canli saglayici cagrilmaz, hicbir sey yuklenmez.' }),
    el('div', { class: 'row', style: 'margin:12px 0; align-items:center; gap:12px' },
      el('button', {
        class: 'btn btn--primary', type: 'button', 'data-testid': 'run-diagnostics',
        disabled: state.diagnosticsBusy,
        onclick: async (event) => {
          const button = event.currentTarget;
          state.diagnosticsBusy = true;
          button.disabled = true;
          button.textContent = 'Calisiyor... (bir kac dakika surebilir)';
          const result = await guard(() => api.post('/api/diagnostics', {}), 'Teshis tamamlandi');
          state.diagnosticsBusy = false;
          if (result) state.diagnostics = result.diagnostics;
          render();
        },
      }, state.diagnosticsBusy ? 'Calisiyor...' : 'Teshisi calistir'),
      el('span', { class: 'dim', 'data-testid': 'diagnostics-last-run',
        text: neverRun ? 'Son calisma: hic calistirilmadi'
                       : `Son calisma: ${report.last_run_at} (${report.duration_sec}s)` })),
    report && report.summary
      ? el('div', { class: 'dim', 'data-testid': 'diagnostics-summary',
                    style: 'margin-bottom:8px', text: report.summary }) : null,
    !neverRun
      ? el('div', { class: 'dim mono', 'data-testid': 'diagnostics-counts',
          text: Object.entries(counts).filter(([, n]) => n)
            .map(([key, n]) => `${DIAG_LABEL[key] || key}: ${n}`).join(' · ') })
      : null,
    report && report.live_provider_tests === false
      ? el('div', { class: 'dim', 'data-testid': 'diagnostics-live-note', style: 'margin-top:8px',
          text: 'Saglayici ERISILEBILIRLIGI ile ISLEVSELLIGI ayri raporlanir. Canli model '
              + 'cagrilari kapali oldugu icin Gemini/TTS "kisitli" gorunur; bu beklenen '
              + 'durumdur.' })
      : null,
    el('div', { class: 'diag-list', 'data-testid': 'diagnostics-list' },
      capabilities.map(diagnosticsRow)));
}

function automationBar() {
  const auto = (state.project && state.project.automation) || {};
  const stateKey = auto.state || 'idle';
  const badgeKind = { running: 'running', paused: 'queued', blocked: 'review_required',
                      completed: 'completed', cancelled: 'failed',
                      idle: 'missing' }[stateKey] || 'missing';
  const isGate = (auto.human_gates || []).includes(auto.blocker);
  return el('section', { class: 'automation-bar', 'aria-label': 'Otomatik akis' },
    el('div', {},
      el('div', { class: 'row' },
        el('strong', { text: 'Otomatik akis' }),
        badge(badgeKind, stateKey),
        isGate ? badge('review_required', 'insan onayi gerekli') : null),
      el('div', { class: 'dim', 'data-testid': 'automation-message',
                  text: auto.message || 'Otomatik akis baslatilmadi.' }),
      auto.blocker
        ? el('div', { class: 'dim', 'data-testid': 'automation-blocker',
                      text: `duraklama noktasi: ${auto.blocker}` })
        : null),
    el('div', { class: 'row' },
      auto.can_start ? el('button', {
        class: 'btn btn--primary', 'data-testid': 'automation-start',
        disabled: state.autoBusy,
        onclick: () => automationAction('start'),
      }, stateKey === 'idle' ? 'Otomatik akisi baslat' : 'Yeniden baslat') : null,
      auto.can_resume ? el('button', {
        class: 'btn', 'data-testid': 'automation-resume', disabled: state.autoBusy,
        onclick: () => automationAction('resume'),
      }, 'Devam et') : null,
      auto.can_pause ? el('button', {
        class: 'btn', 'data-testid': 'automation-pause', disabled: state.autoBusy,
        onclick: () => automationAction('pause'),
      }, 'Duraklat') : null,
      auto.can_cancel ? el('button', {
        class: 'btn btn--danger', 'data-testid': 'automation-cancel',
        disabled: state.autoBusy,
        onclick: () => automationAction('cancel'),
      }, 'Iptal') : null));
}

function projectLifecyclePanel() {
  const record = (((state.project || {}).project || {}).data) || {};
  if (!record.project_id) return null;
  const active = Number(((state.project || {}).job_summary || {}).active || 0);
  const revisions = (state.project && state.project.revisions) || [];
  const archive = (state.project && state.project.archive) || {};
  const disabled = active > 0;
  const archiveDisabled = disabled || !archive.configured || !archive.available;
  const runLifecycle = async (action, extra, successTitle) => {
    const result = await guard(() => api.post(
      `/api/projects/${state.projectId}/lifecycle`, {
        action, actor: projectActor(), ...extra,
      }), successTitle);
    if (result) await refresh();
    return result;
  };
  const destructiveValues = () => validateFields([
    { id: 'lifecycle-confirmation', label: 'Proje adi dogrulamasi', required: true },
  ]);
  return el('details', { class: 'card lifecycle-card', 'data-testid': 'project-lifecycle' },
    el('summary', {},
      el('span', { text: 'Proje islemleri ve onceki asamaya donus' }),
      revisions.length ? badge('review_required', `${revisions.length} revizyon`) : null),
    el('p', { class: 'subtitle lifecycle-lead',
              text: 'Geri donulen asama ve sonraki ciktilar kaybolmaz; proje icindeki '
                    + 'revizyon arsivine tasinir. Aktif is varken bu islemler kilitlidir.' }),
    el('p', { class: 'subtitle lifecycle-lead',
      text: `${((state.project || {}).storage_policy || {}).working_location_name || 'C aktif calisma'} → `
        + `${archive.location_name || 'G uzun sureli arsiv'}` }),
    disabled ? alertBox('warn', 'Proje su anda calisiyor',
      `${active} aktif is tamamlanmadan geri donme, arsivleme, sifirlama veya silme yapilamaz.`) : null,
    archive.configured && !archive.available
      ? alertBox('warn', 'Arsiv diski kullanilamiyor',
        'G arsiv klasorunu kullanabilmek icin diskin bagli ve yazilabilir oldugunu kontrol et.')
      : null,
    requiredLegend(),
    el('div', { class: 'lifecycle-section' },
      el('h3', { text: 'Bir asamaya geri don' }),
      el('p', { class: 'dim',
                text: 'Secilen asama yeniden acilir; ondan sonraki ses/render gibi '
                      + 'ciktilar aktif akistan kaldirilir.' }),
      field({ id: 'lifecycle-target', label: 'Geri donulecek asama', required: true,
        control: el('select', { id: 'lifecycle-target', 'aria-required': 'true',
                                'data-testid': 'lifecycle-target' },
          [
            ['assessment', 'Faz 0 degerlendirme'], ['plan', 'Isleme plani'],
            ['analysis', 'Video analizi'], ['moment', 'Moment secimi'],
            ['script', 'Script'], ['voice', 'Seslendirme'], ['render', 'Render'],
          ].map(([value, label]) => el('option', { value }, label))) }),
      el('button', {
        class: 'btn', disabled, 'data-testid': 'lifecycle-rewind',
        onclick: async (event) => {
          event.currentTarget.disabled = true;
          await runLifecycle('rewind', {
            target: document.getElementById('lifecycle-target').value,
          }, 'Proje onceki asamaya donduruldu');
        },
      }, 'Bu asamaya geri don')),
    el('div', { class: 'lifecycle-section lifecycle-section--danger' },
      el('h3', { text: 'Arsivle, sifirla veya sil' }),
      inputField({ id: 'lifecycle-confirmation', label: 'Proje adi dogrulamasi',
                   required: true,
                   hint: `Devam etmek icin proje adini birebir yaz: ${record.title}`,
                   attrs: { maxlength: 200, autocomplete: 'off',
                            placeholder: record.title || '' } }),
      el('div', { class: 'row' },
        el('button', {
          class: 'btn btn--primary', disabled: archiveDisabled,
          'data-testid': 'lifecycle-archive',
          title: archive.location_name || 'Arsiv konumu yapilandirilmamis',
          onclick: async (event) => {
            const values = destructiveValues();
            if (!values) return;
            event.currentTarget.disabled = true;
            const archived = await guard(() => api.post(
              `/api/projects/${state.projectId}/archive`, {
                confirmation: values['lifecycle-confirmation'], actor: projectActor(),
              }), 'Proje G arsivine tasindi');
            if (archived) {
              state.projectId = null; state.project = null; state.selectedMode = null;
              await goto('history');
            } else {
              event.currentTarget.disabled = archiveDisabled;
            }
          },
        }, 'Projeyi G arsivine tasi'),
        el('button', {
          class: 'btn', disabled, 'data-testid': 'lifecycle-reset-keep-source',
          onclick: async (event) => {
            const values = destructiveValues();
            if (!values) return;
            event.currentTarget.disabled = true;
            await runLifecycle('reset', {
              keep_source: true, confirmation: values['lifecycle-confirmation'],
            }, 'Proje kaynak korunarak sifirlandi');
          },
        }, 'Sifirla - kaynak videoyu koru'),
        el('button', {
          class: 'btn btn--danger', disabled, 'data-testid': 'lifecycle-reset-all',
          onclick: async (event) => {
            const values = destructiveValues();
            if (!values) return;
            event.currentTarget.disabled = true;
            await runLifecycle('reset', {
              keep_source: false, confirmation: values['lifecycle-confirmation'],
            }, 'Proje tamamen sifirlandi');
          },
        }, 'Tamamen sifirla'),
        el('button', {
          class: 'btn btn--danger', disabled, 'data-testid': 'lifecycle-delete',
          onclick: async (event) => {
            const values = destructiveValues();
            if (!values) return;
            event.currentTarget.disabled = true;
            const removed = await guard(() => api.request(
              'DELETE', `/api/projects/${state.projectId}`, {
                body: { confirmation: values['lifecycle-confirmation'],
                        actor: projectActor() },
              }), 'Proje silindi');
            if (removed) {
              state.projectId = null; state.project = null; state.selectedMode = null;
              await goto('projects');
            }
          },
        }, 'Projeyi sil'))));
}

function screenHistory() {
  const projects = (state.workspace && state.workspace.projects) || [];
  const archive = (state.workspace && state.workspace.archive) || {};
  const archivedProjects = archive.projects || [];
  const projectCards = projects.map(project => {
    const titleId = `history-title-${project.project_id}`;
    const notesId = `history-notes-${project.project_id}`;
    const operatorId = `history-operator-${project.project_id}`;
    return el('article', { class: 'card archive-card' },
      el('div', { class: 'card__head' },
        el('div', {},
          el('button', {
            class: 'project-link project-link--large', type: 'button',
            disabled: Boolean(project.problem),
            onclick: () => openProject(project.project_id),
          }, project.title),
          el('div', { class: 'dim', text: project.source_name || 'Kaynak video henuz yok' })),
        badge(project.problem ? 'failed' : (project.status || 'review_required'))),
      project.problem ? alertBox('danger', 'Kayit okunamadi', project.problem) : null,
      el('div', { class: 'grid grid--2 archive-meta' },
        el('div', {}, el('div', { class: 'dim', text: 'Olusturuldu' }),
          el('div', { text: project.created_at || '-' })),
        el('div', {}, el('div', { class: 'dim', text: 'Son duzenleme' }),
          el('div', { text: project.updated_at || project.created_at || '-' }))),
      requiredLegend(),
      inputField({ id: titleId, label: 'Proje adi', required: true,
                   attrs: { value: project.title, maxlength: 200,
                            disabled: Boolean(project.problem) } }),
      inputField({ id: operatorId, label: 'Ureten kisi', required: true,
                   hint: 'Degistirirsen bundan sonraki proje islemlerinde yeni ad kullanilir.',
                   attrs: { value: project.operator_name || '', maxlength: 120,
                            disabled: Boolean(project.problem) } }),
      field({ id: notesId, label: 'Proje notlari',
              control: el('textarea', { id: notesId, maxlength: 2000,
                placeholder: 'Icerik fikri, hedef kitle veya sonraki adimlar...',
                disabled: Boolean(project.problem) }, project.notes || '') }),
      el('div', { class: 'row' },
        el('button', {
          class: 'btn btn--primary', 'data-testid': 'open-project', disabled: Boolean(project.problem),
          onclick: async (event) => {
            const values = validateFields([
              { id: titleId, label: 'Proje adi', required: true },
              { id: operatorId, label: 'Ureten kisi', required: true },
              { id: notesId, label: 'Proje notlari' },
            ]);
            if (!values) return;
            const button = event.currentTarget;
            button.disabled = true;
            const updated = await guard(() => api.request(
              'PATCH', `/api/projects/${project.project_id}`,
              { body: { title: values[titleId], operator_name: values[operatorId],
                        notes: values[notesId] || '' } }),
              'Kayit guncellendi');
            button.disabled = false;
            if (updated) await refresh();
          },
        }, 'Degisiklikleri kaydet'),
        el('button', {
          class: 'btn', disabled: Boolean(project.problem),
          onclick: () => openProject(project.project_id),
        }, 'Calismaya devam et')));
  });

  return el('div', {},
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { text: 'Onceki kayitlar' }),
        el('p', { class: 'subtitle',
          text: 'Projeler, kaynaklar ve sonuclar yeniden baslatmadan sonra da burada kalir.' }))),
    projects.length === 0
      ? el('div', { class: 'card empty', text: 'Henuz aktif proje yok.' })
      : el('div', { class: 'archive-list' }, projectCards),
    el('div', { class: 'page-head archive-heading' },
      el('div', {},
        el('h2', { text: 'G arsivindeki projeler' }),
        el('p', { class: 'subtitle',
          text: archive.configured
            ? `${archive.location_name || 'Harici arsiv'} · aktif calisma listesinden ayridir.`
            : 'Arsiv konumu henuz yapilandirilmamis.' }))),
    archivedProjects.length === 0
      ? el('div', { class: 'card empty',
          text: archive.available ? 'Henuz arsivlenmis proje yok.' : 'Arsiv diski kullanilamiyor.' })
      : el('div', { class: 'archive-list' }, archivedProjects.map(item =>
          el('article', { class: 'card archive-card' },
            el('div', { class: 'card__head' },
              el('div', {},
                el('h3', { text: item.title || '(okunamadi)' }),
                el('div', { class: 'dim', text: item.source_name || 'Kaynak adi yok' })),
              badge('completed', 'arsivlendi')),
            el('div', { class: 'grid grid--3 archive-meta' },
              el('div', {}, el('div', { class: 'dim', text: 'Arsivlenme' }),
                el('div', { text: item.archived_at || '-' })),
              el('div', {}, el('div', { class: 'dim', text: 'Boyut' }),
                el('div', { text: formatBytes(item.total_bytes) })),
              el('div', {}, el('div', { class: 'dim', text: 'Dosya' }),
                el('div', { text: String(item.file_count || 0) })))))));
}

const LONG_FORM_DEFAULTS = {
  topic: '', content_type: 'explainer', target_duration_min: 18,
  chapter_count: 6, language: 'tr-TR', tone: 'documentary',
  audience: 'Genel izleyici', angle: '', narration_mode: 'undecided',
  include_subtitles: true, notes: '',
};

function longFormDraft() {
  if (!state.longFormDraft || state.longFormDraft.projectId !== state.projectId) {
    const saved = (((state.project || {}).long_form || {}).data) || {};
    state.longFormDraft = { ...LONG_FORM_DEFAULTS, ...saved, projectId: state.projectId };
  }
  return state.longFormDraft;
}

function setLongFormDraft(key, value) {
  const draft = longFormDraft();
  draft[key] = value;
  if (key === 'target_duration_min') {
    const duration = Math.max(15, Math.min(20, Number(value) || 18));
    const label = document.getElementById('longform-word-range');
    if (label) label.textContent = `${duration * 130}-${duration * 150} kelime`;
  }
}

function longFormSelect(id, label, value, options, onChange, hint = '') {
  return field({ id, label, required: true, hint,
    control: el('select', { id, 'aria-required': 'true',
      onchange: event => onChange(event.currentTarget.value) },
    options.map(([optionValue, optionLabel]) => el('option', {
      value: optionValue, selected: value === optionValue,
    }, optionLabel))) });
}

function screenLongForm() {
  const section = (state.project && state.project.long_form) || {};
  const draft = longFormDraft();
  const duration = Math.max(15, Math.min(20, Number(draft.target_duration_min) || 18));
  const saved = section.data;
  const update = key => event => setLongFormDraft(key, event.currentTarget.value);
  const rules = [
    { id: 'longform-topic', label: 'Video konusu', required: true },
    { id: 'longform-content-type', label: 'Video turu', required: true },
    { id: 'longform-duration', label: 'Hedef sure', required: true,
      number: true, min: 15, max: 20 },
    { id: 'longform-chapters', label: 'Bolum sayisi', required: true,
      number: true, min: 5, max: 7 },
    { id: 'longform-language', label: 'Video dili', required: true },
    { id: 'longform-tone', label: 'Anlatim tonu', required: true },
    { id: 'longform-audience', label: 'Hedef kitle' },
    { id: 'longform-angle', label: 'Anlatim acisi' },
    { id: 'longform-narration', label: 'Seslendirme tercihi', required: true },
    { id: 'longform-notes', label: 'Plan notlari' },
  ];
  return el('div', { 'data-testid': 'longform-screen' },
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { text: 'Uzun Video Planlama' }),
        el('p', { class: 'subtitle',
          text: '15-20 dakikalik teori, genel anlatim ve karsilastirma videolari icin brief.' })),
      saved ? badge('review_required', 'plan kayitli') : badge('planned', 'yeni plan')),
    alertBox('info', 'Yalnizca planlama',
      'Bu sekme model cagrisi, script uretimi, seslendirme veya render baslatmaz. '
      + 'Kaydedilen brief daha sonra outline ve bolum uretiminin girdisi olacak.'),
    section.problem ? alertBox('danger', 'Plan okunamadi', section.problem) : null,
    el('div', { class: 'card' },
      el('h2', { text: 'Icerik briefi' }),
      requiredLegend(),
      inputField({ id: 'longform-topic', label: 'Video konusu', required: true,
        hint: 'Ornek: House of the Dragon evreninde ejderha biniciligi nasil calisir?',
        attrs: { value: draft.topic, maxlength: 300, 'data-testid': 'longform-topic',
          oninput: update('topic') } }),
      el('div', { class: 'grid grid--2' },
        longFormSelect('longform-content-type', 'Video turu', draft.content_type, [
          ['explainer', 'Genel anlatim'], ['theory', 'Teori / yorum'],
          ['comparison', 'Karsilastirma / analiz'],
        ], value => setLongFormDraft('content_type', value)),
        longFormSelect('longform-language', 'Video dili', draft.language, [
          ['tr-TR', 'Turkce'], ['en-US', 'English'],
        ], value => setLongFormDraft('language', value))),
      textareaField({ id: 'longform-angle', label: 'Anlatim acisi',
        hint: 'Videonun ana iddiasi, sorusu veya izleyiciye verecegi bakis acisi.',
        value: draft.angle,
        attrs: { maxlength: 1000, 'data-testid': 'longform-angle',
          oninput: update('angle') } }),
      el('div', { class: 'grid grid--3' },
        inputField({ id: 'longform-duration', label: 'Hedef sure (dakika)', required: true,
          type: 'number', attrs: { value: duration, min: 15, max: 20,
            'data-testid': 'longform-duration', oninput: update('target_duration_min') } }),
        inputField({ id: 'longform-chapters', label: 'Bolum sayisi', required: true,
          type: 'number', attrs: { value: draft.chapter_count, min: 5, max: 7,
            'data-testid': 'longform-chapters', oninput: update('chapter_count') } }),
        longFormSelect('longform-tone', 'Anlatim tonu', draft.tone, [
          ['documentary', 'Belgesel'], ['cinematic', 'Sinematik'],
          ['conversational', 'Sohbet'], ['analytical', 'Analitik'],
        ], value => setLongFormDraft('tone', value))),
      inputField({ id: 'longform-audience', label: 'Hedef kitle',
        attrs: { value: draft.audience, maxlength: 300,
          'data-testid': 'longform-audience', oninput: update('audience') } }),
      longFormSelect('longform-narration', 'Seslendirme tercihi', draft.narration_mode, [
        ['undecided', 'Daha sonra karar ver'], ['tts', 'TTS anlatim'],
        ['own_voice', 'Kendi sesim'],
      ], value => setLongFormDraft('narration_mode', value)),
      el('label', { class: 'check-row' },
        el('input', { id: 'longform-subtitles', type: 'checkbox',
          checked: draft.include_subtitles !== false,
          onchange: event => setLongFormDraft('include_subtitles', event.currentTarget.checked) }),
        el('span', { text: 'Tam video altyazisi planla' })),
      textareaField({ id: 'longform-notes', label: 'Plan notlari',
        value: draft.notes,
        attrs: { maxlength: 3000, 'data-testid': 'longform-notes',
          oninput: update('notes') } }),
      el('div', { class: 'longform-estimate' },
        el('div', {}, el('span', { class: 'dim', text: 'Tahmini script: ' }),
          el('strong', { id: 'longform-word-range', text: `${duration * 130}-${duration * 150} kelime` })),
        el('div', { class: 'dim',
          text: 'Hook + giris + 5-7 ana bolum + sonuc/CTA yapisi hedeflenir.' })),
      el('button', { class: 'btn btn--primary', 'data-testid': 'longform-save',
        onclick: async event => {
          const values = validateFields(rules);
          if (!values) return;
          const button = event.currentTarget;
          button.disabled = true;
          const result = await guard(() => api.request(
            'PATCH', `/api/projects/${state.projectId}/long-form-plan`, { body: {
              topic: values['longform-topic'],
              content_type: values['longform-content-type'],
              target_duration_min: values['longform-duration'],
              chapter_count: values['longform-chapters'],
              language: values['longform-language'], tone: values['longform-tone'],
              audience: values['longform-audience'] || 'Genel izleyici',
              angle: values['longform-angle'] || '',
              narration_mode: values['longform-narration'],
              include_subtitles: document.getElementById('longform-subtitles').checked,
              notes: values['longform-notes'] || '', updated_by: projectActor(),
            } }), 'Uzun video plani kaydedildi');
          if (result) {
            state.longFormDraft = { ...result.long_form, projectId: state.projectId };
            await refresh();
          } else {
            button.disabled = false;
          }
        } }, saved ? 'Plani guncelle' : 'Plani kaydet')),
    saved ? el('div', { class: 'card' },
      el('h2', { text: 'Kayit durumu' }),
      el('div', { class: 'grid grid--3' },
        el('div', { class: 'tech-cell' }, el('div', { class: 'tech-cell__label', text: 'Durum' }),
          el('div', { class: 'tech-cell__value', text: 'Taslak' })),
        el('div', { class: 'tech-cell' }, el('div', { class: 'tech-cell__label', text: 'Son guncelleme' }),
          el('div', { class: 'tech-cell__value', text: saved.updated_at || '-' })),
        el('div', { class: 'tech-cell' }, el('div', { class: 'tech-cell__label', text: 'Uretim' }),
          el('div', { class: 'tech-cell__value', text: 'Baslatilmadi' })))) : null,
    longFormStudioPanel());
}

/* Uzun Video Studio: uretim paneli.
 *
 * The planning brief above stays exactly as it was. Everything here is the
 * PRODUCTION half: what a run would cost, which stage is next, and the two
 * things a long run actually needs from a UI -- visible progress and an
 * actionable reason when it stops.
 *
 * Rules this panel follows:
 *   1. Opening the tab spends nothing. Estimates and stage state are reads.
 *   2. Every long action shows the stage, the unit in flight and the elapsed
 *      time, so the screen is never a frozen button.
 *   3. A failure shows a sanitized reason plus the one control that fixes it
 *      (retry this chunk, regenerate this chunk, approve this gate). */
function longFormStudioPanel() {
  const studio = state.longFormStudio;
  if (!studio || !studio.available) return null;
  const progress = studio.progress || {};
  const estimates = studio.estimates || {};
  const readiness = studio.readiness || {};
  return el('div', {},
    longFormEstimatePanel(estimates, readiness),
    longFormControlPanel(progress),
    longFormStagePanel(progress),
    longFormScriptPanel(studio),
    longFormBrollPanel(studio.broll || {}, studio.chapters || []),
    longFormChunkPanel(studio.tts || {}),
    longFormEventPanel(studio.events || [], studio.quota || {}));
}

function longFormEstimatePanel(estimates, readiness) {
  const calls = estimates.api_calls || {};
  const capacity = readiness.tts_capacity || {};
  return el('div', { class: 'card', 'data-testid': 'lf-estimates' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Uretim tahmini' }),
      badge(readiness.ready ? 'completed' : 'missing',
            readiness.ready ? 'saglayici hazir' : 'saglayici eksik')),
    el('div', { class: 'grid grid--2' },
      techCell('Kelime hedefi', `${(estimates.word_range || {}).minimum}-${(estimates.word_range || {}).maximum}`),
      techCell('TTS parcasi (tahmini)', String(estimates.tts_chunk_estimate || '-')),
      techCell('API istegi (tahmini)', String(calls.total || '-')),
      techCell('En-boy orani', estimates.aspect_ratio || '16:9')),
    calls.total ? el('div', { class: 'dim', 'data-testid': 'lf-call-breakdown',
      text: `Arastirma ${calls.research} + outline ${calls.outline} + bolum `
            + `${calls.chapters} + tutarlilik ${calls.consistency} + TTS `
            + `${calls.tts_chunks}. ${calls.note || ''}` }) : null,
    (readiness.blockers || []).map(text => alertBox('danger', 'Baslatilamaz', text)),
    (readiness.warnings || []).map(text => alertBox('warn', 'Kota uyarisi', text)),
    capacity.needed ? el('div', { class: 'dim', 'data-testid': 'lf-capacity',
      text: `Gunluk TTS kapasitesi ${capacity.daily_request_capacity} istek, `
            + `dakikalik ${capacity.per_minute_request_capacity} istek `
            + `(sinirlayan: ${capacity.binding_limit}).` }) : null);
}

function longFormControlPanel(progress) {
  const busy = progress.status === 'running';
  const next = progress.next || {};
  const send = async (body, successTitle) => {
    const result = await guard(() => api.post(
      `/api/projects/${state.projectId}/long-form/control`, body), successTitle);
    if (result) state.longFormStudio = result.long_form_studio;
    await refresh();
  };
  return el('div', { class: 'card', 'data-testid': 'lf-controls' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Uretim kontrolu' }),
      badge(progress.cancelled ? 'failed' : (progress.paused ? 'queued' : progress.status),
            progress.cancelled ? 'iptal' : (progress.paused ? 'duraklatildi' : progress.status))),
    el('div', { class: 'dim', 'data-testid': 'lf-next',
      text: next.reason || '' }),
    next.billing_blocked
      ? alertBox('danger', 'Billing/kota ayari gerekiyor',
          'Google AI Studio planini, faturalandirmayi ve proje kotasini kontrol et. '
          + 'Bu engel zamanlayiciyla tekrar denenmez.')
      : null,
    el('div', { class: 'row' },
      el('button', { class: 'btn btn--primary', 'data-testid': 'lf-start',
        onclick: (e) => withButton(e, () => send({ action: 'start' }, 'Uretim baslatildi')) },
        'Uretimi baslat'),
      el('button', { class: 'btn btn--sm', 'data-testid': 'lf-run-stage',
        disabled: Boolean(next.blocked),
        onclick: (e) => withButton(e, async () => {
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/long-form/stage`, {}),
            'Asama calisti');
          if (result) state.longFormStudio = result.long_form_studio;
          await refresh();
        }) }, 'Sonraki asamayi calistir'),
      next.billing_blocked
        ? el('button', { class: 'btn btn--primary', 'data-testid': 'lf-retry-billing',
            onclick: (e) => withButton(e, () => send(
              { action: 'retry_billing' },
              'Billing engeli kaldirildi; asama yeniden denenebilir')) },
            'Billing duzeltildi - yeniden denemeyi ac')
        : null,
      progress.paused
        ? el('button', { class: 'btn btn--sm', 'data-testid': 'lf-resume',
            onclick: (e) => withButton(e, () => send({ action: 'resume' }, 'Devam')) },
            'Devam et')
        : el('button', { class: 'btn btn--sm', 'data-testid': 'lf-pause',
            onclick: (e) => withButton(e, () => send({ action: 'pause' }, 'Duraklatildi')) },
            'Duraklat'),
      el('button', { class: 'btn btn--sm btn--danger', 'data-testid': 'lf-cancel',
        onclick: (e) => withButton(e, () => send({ action: 'cancel' }, 'Iptal edildi')) },
        'Iptal et')));
}

function longFormStagePanel(progress) {
  const stages = progress.stages || [];
  return el('div', { class: 'card', 'data-testid': 'lf-stages' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Asamalar' }),
      badge('planned', `${progress.completed_count || 0}/${progress.total_count || 12}`)),
    el('div', { class: 'stack' }, stages.map(stage => el('div', {
      class: 'spread', 'data-testid': `lf-stage-${stage.key}` },
      el('span', {}, badge(_lfStageBadge(stage.state), stage.state), ' ', stage.label),
      el('span', { class: 'dim',
        text: (stage.attempts ? `deneme ${stage.attempts}` : '')
              + (stage.detail ? ` - ${stage.detail}` : '') }),
      stage.error
        ? el('span', { class: 'transcript-nomatch', 'data-testid': `lf-error-${stage.key}`,
                       text: stage.error })
        : null,
      (!stage.automatic && stage.state !== 'completed')
        ? el('button', { class: 'btn btn--sm', 'data-testid': `lf-approve-${stage.key}`,
            onclick: (e) => withButton(e, async () => {
              const result = await guard(() => api.post(
                `/api/projects/${state.projectId}/long-form/control`,
                { action: 'approve_stage', stage: stage.key }), 'Asama onaylandi');
              if (result) state.longFormStudio = result.long_form_studio;
              await refresh();
            }) }, 'Onayla')
        : null))));
}

function _lfStageBadge(stateName) {
  return { completed: 'completed', failed: 'failed', running: 'running',
           review_required: 'review_required', queued: 'queued',
           waiting_rate_limit: 'queued', waiting_daily_quota: 'queued',
           blocked_billing: 'failed',
           cancelled: 'failed' }[stateName] || 'missing';
}

async function longFormResumeTick() {
  if (state.longFormAutoBusy || state.screen !== 'longform' || !state.projectId) return;
  state.longFormAutoBusy = true;
  try {
    const result = await api.post(
      `/api/projects/${state.projectId}/long-form/stage`, {});
    if (result) state.longFormStudio = result.long_form_studio;
  } catch (error) {
    pushAlert('danger', 'Uzun video devam edemedi', error.message, error.hint);
  } finally {
    state.longFormAutoBusy = false;
  }
  await refresh();
}

function longFormScriptPanel(studio) {
  const chapters = studio.chapters || [];
  const summary = studio.chapters_summary || {};
  const research = studio.research;
  const consistency = studio.consistency;
  if (!chapters.length && !research) return null;
  if (!state.longFormDraft) state.longFormDraft = {};
  const draft = state.longFormDraft;
  return el('div', { class: 'card', 'data-testid': 'lf-script' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Script' }),
      badge(chapters.length ? 'review_required' : 'missing',
            `${summary.total_word_count || 0} kelime`)),
    research ? el('div', { class: 'dim', 'data-testid': 'lf-research',
      text: `Arastirma: ${(research.facts || []).length} kaynakli olgu, `
            + `${(research.interpretations || []).length} yorum. Tez: `
            + `${research.thesis || '-'}` }) : null,
    consistency ? el('div', { class: 'dim', 'data-testid': 'lf-consistency',
      text: `Tutarlilik: ${consistency.issue_count} bulgu `
            + `(${consistency.high_severity_count} yuksek). ${consistency.overall || ''}` })
      : null,
    el('div', { class: 'stack' }, chapters.map(chapter => el('div', {
      class: 'moment-card', 'data-testid': `lf-chapter-${chapter.index}` },
      el('div', { class: 'spread' },
        el('strong', { text: `${chapter.index}. ${chapter.title || ''}` }),
        el('span', { class: 'dim',
          text: `${chapter.word_count}/${chapter.target_words} kelime - `
                + `~${Math.round((chapter.duration_estimate || {}).expected_sec || 0)} sn`
                + (chapter.within_target ? '' : ' - hedef disi')
                + (chapter.edited_by_human ? ' - elle duzenlendi' : '') })),
      el('textarea', {
        class: 'transcript-full-input', rows: 4, spellcheck: 'false',
        'data-testid': `lf-chapter-input-${chapter.index}`,
        oninput: (e) => { draft[chapter.index] = e.currentTarget.value; },
      }, draft[chapter.index] !== undefined ? draft[chapter.index] : (chapter.text || '')),
      el('button', { class: 'btn btn--sm', 'data-testid': `lf-chapter-save-${chapter.index}`,
        onclick: (e) => withButton(e, async () => {
          const node = document.querySelector(`[data-testid=lf-chapter-input-${chapter.index}]`);
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/long-form/chapter`,
            { index: chapter.index, text: node ? node.value : '' }),
            'Bolum kaydedildi');
          if (result) { state.longFormStudio = result.long_form_studio; delete draft[chapter.index]; }
          await refresh();
        }) }, 'Bolumu kaydet')))),
    studio.full_script
      ? el('details', {}, el('summary', { text: 'Tum script' }),
          el('pre', { class: 'pre', 'data-testid': 'lf-full-script',
                      text: studio.full_script }))
      : null);
}

function longFormChunkPanel(tts) {
  const chunks = tts.chunks || [];
  const readiness = tts.readiness || {};
  const combined = tts.combined;
  if (!chunks.length) return null;
  return el('div', { class: 'card', 'data-testid': 'lf-chunks' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'TTS parcalari' }),
      badge(readiness.ready ? 'completed' : 'review_required',
            `${readiness.completed_count || 0}/${readiness.chunk_count || 0}`)),
    el('p', { class: 'dim', text: tts.approval_policy || '' }),
    el('div', { class: 'stack' }, chunks.map(chunk => el('div', {
      class: 'spread', 'data-testid': `lf-chunk-${chunk.id}` },
      el('span', {}, badge(_lfStageBadge(chunk.status === 'pending' ? 'queued' : chunk.status),
                           chunk.status), ` ${chunk.sequence}. ${chunk.chapter_id}`),
      el('span', { class: 'dim',
        text: `${chunk.word_count} kelime`
              + (chunk.duration_sec ? ` - ${chunk.duration_sec}s` : '')
              + (chunk.attempts ? ` - deneme ${chunk.attempts}` : '') }),
      chunk.error
        ? el('span', { class: 'transcript-nomatch', 'data-testid': `lf-chunk-error-${chunk.id}`,
                       text: `${chunk.error_kind || ''}: ${chunk.error}` })
        : null,
      chunk.wait_reason
        ? el('span', { class: 'dim', 'data-testid': `lf-chunk-wait-${chunk.id}`,
            text: `${chunk.wait_reason} - devam: `
                  + `${chunk.resume_at ? new Date(chunk.resume_at).toLocaleString() : '-'}` })
        : null,
      chunk.status === 'completed'
        ? el('audio', { controls: true, preload: 'none',
            'data-testid': `lf-chunk-audio-${chunk.id}`,
            src: `/api/projects/${state.projectId}/media/results/long_form/tts/`
                 + encodeURIComponent(chunk.media_name || '') })
        : null,
      el('button', { class: 'btn btn--sm', 'data-testid': `lf-chunk-retry-${chunk.id}`,
        onclick: (e) => withButton(e, async () => {
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/long-form/tts`,
            { action: 'retry', chunk_id: chunk.id }), 'Parca yeniden denendi');
          if (result) state.longFormStudio = result.long_form_studio;
          await refresh();
        }) }, chunk.status === 'completed' ? 'Yeniden uret' : 'Tekrar dene')))),
    el('div', { class: 'row' },
      el('button', { class: 'btn btn--primary', 'data-testid': 'lf-tts-run',
        onclick: (e) => withButton(e, async () => {
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/long-form/tts`, { action: 'run' }),
            'Eksik parcalar uretildi');
          if (result) state.longFormStudio = result.long_form_studio;
          await refresh();
        }) }, 'Eksik parcalari uret'),
      el('button', { class: 'btn btn--sm', 'data-testid': 'lf-tts-concat',
        disabled: !readiness.ready,
        onclick: (e) => withButton(e, async () => {
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/long-form/tts`, { action: 'concat' }),
            'Ses birlestirildi');
          if (result) state.longFormStudio = result.long_form_studio;
          await refresh();
        }) }, 'Parcalari birlestir')),
    combined ? el('div', { class: 'stack', 'data-testid': 'lf-combined' },
      el('div', { class: 'dim',
        text: `Birlesik anlatim: ${combined.duration_sec}s `
              + `(${(combined.duration_check || {}).measured_min} dk), `
              + `${combined.chunk_count} parca` }),
      (combined.duration_check && !combined.duration_check.within_target)
        ? alertBox('warn', 'Sure hedef disinda', combined.duration_check.reason)
        : null) : null);
}

function longFormBrollPanel(broll, chapters) {
  if (!chapters.length) return null;
  const providers = broll.providers || [];
  const configured = providers.filter(item => item.configured);
  const slots = broll.slots || [];
  const post = async (body, title) => {
    const result = await guard(() => api.post(
      `/api/projects/${state.projectId}/long-form/broll`, body), title);
    if (result) state.longFormStudio = result.long_form_studio;
    await refresh();
  };
  if (!broll.available) {
    return el('div', { class: 'card', 'data-testid': 'lf-broll' },
      el('div', { class: 'card__head' },
        el('h2', { text: 'B-roll materyal plani' }),
        badge('planned', 'MoneyPrinterTurbo uyarlamasi')),
      el('p', { class: 'dim', text:
        'Bolum sirasina gore duzenlenebilir arama slotlari olusturur. Bu adim API cagrisi yapmaz.' }),
      inputField({ id: 'lf-broll-duration', label: 'Klip degistirme suresi (sn)',
        type: 'number', attrs: { value: 5, min: 2, max: 12,
          'data-testid': 'lf-broll-duration' } }),
      el('button', { class: 'btn btn--primary', 'data-testid': 'lf-broll-plan',
        onclick: (e) => withButton(e, () => post({ action: 'plan',
          clip_duration_sec: Number(document.getElementById('lf-broll-duration').value || 5),
        }, 'B-roll plani olusturuldu')) }, 'B-roll plani olustur'));
  }
  return el('div', { class: 'card', 'data-testid': 'lf-broll' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'B-roll materyal plani' }),
      badge(broll.downloaded_count === broll.slot_count ? 'completed' : 'review_required',
        `${broll.downloaded_count || 0}/${broll.slot_count || 0} indirildi`)),
    el('p', { class: 'dim', text:
      `${broll.aspect_ratio || '16:9'} - klip degisimi ${broll.clip_duration_sec || 5} sn. `
      + `${broll.policy || ''}` }),
    broll.stale ? alertBox('danger', 'B-roll plani guncel degil',
      'Bolum scripti plan olusturulduktan sonra degisti. Eski secimleri kullanmadan plani yeniden olustur.') : null,
    broll.stale ? el('button', { class: 'btn btn--primary',
      'data-testid': 'lf-broll-rebuild',
      onclick: (e) => withButton(e, () => post({ action: 'plan',
        clip_duration_sec: broll.clip_duration_sec || 5,
      }, 'B-roll plani guncellendi')) }, 'Plani yeni scriptle olustur') : null,
    configured.length
      ? alertBox('info', 'Stok saglayicisi hazir',
          configured.map(item => item.label).join(', ')
          + '. Her arama yalnizca Arama dugmesine basildiginda bir API istegi yapar.')
      : alertBox('warn', 'Stok API anahtari eksik',
          '.env.local icine PEXELS_API_KEY veya PIXABAY_API_KEY ekle. Planlama ve yerel materyal kullanimi yine calisir.'),
    el('div', { class: 'stack' }, slots.map(slot => {
      const selected = slot.selected;
      const providerId = `lf-broll-provider-${slot.id}`;
      const queryId = `lf-broll-query-${slot.id}`;
      return el('div', { class: 'moment-card', 'data-testid': `lf-broll-slot-${slot.id}` },
        el('div', { class: 'spread' },
          el('strong', { text: `${slot.sequence}. Bolum ${slot.chapter_index}: ${slot.chapter_title}` }),
          badge(_lfStageBadge(slot.status === 'downloaded' ? 'completed' : 'review_required'),
            slot.status || 'planned')),
        el('div', { class: 'dim', text: slot.script_excerpt || '' }),
        el('div', { class: 'grid grid--2' },
          longFormSelect(providerId, 'Saglayici', slot.provider || (configured[0] || {}).id || 'pexels',
            providers.map(item => [item.id, `${item.label}${item.configured ? '' : ' (anahtar yok)'}`]),
            () => {}),
          inputField({ id: queryId, label: 'Arama metni', attrs: {
            value: slot.query || '', maxlength: 180,
            'data-testid': queryId } })),
        el('button', { class: 'btn btn--sm', 'data-testid': `lf-broll-search-${slot.id}`,
          disabled: broll.stale,
          onclick: (e) => withButton(e, () => post({
            action: 'search', slot_id: slot.id,
            provider: document.getElementById(providerId).value,
            query: document.getElementById(queryId).value, per_page: 6,
          }, 'Stok video adaylari getirildi')) }, '6 aday ara'),
        (slot.candidates || []).length ? el('div', { class: 'stack' },
          slot.candidates.map((candidate, index) => el('div', {
            class: 'spread', 'data-testid': `lf-broll-candidate-${slot.id}-${index}` },
            el('span', {},
              el('strong', { text: candidate.attribution || candidate.id }),
              el('span', { class: 'dim', text:
                ` - ${candidate.width}x${candidate.height} - ${candidate.duration_sec || '?'} sn` })),
            candidate.source_page ? el('a', { href: candidate.source_page,
              target: '_blank', rel: 'noopener noreferrer', text: 'Kaynak sayfasi' }) : null,
            el('button', { class: 'btn btn--sm',
              disabled: broll.stale || (selected && selected.id === candidate.id),
              onclick: (e) => withButton(e, () => post({ action: 'select',
                slot_id: slot.id, candidate_id: candidate.id,
              }, 'B-roll adayi secildi')) },
              selected && selected.id === candidate.id ? 'Secili' : 'Sec')))) : null,
        selected ? el('div', { class: 'row' },
          el('span', { class: 'dim', text:
            `Secili: ${selected.attribution || selected.id}` }),
          slot.download
            ? badge('completed', `${slot.download.local_file} - ${formatBytes(slot.download.bytes)}`)
            : el('button', { class: 'btn btn--primary',
                disabled: broll.stale,
                'data-testid': `lf-broll-download-${slot.id}`,
                onclick: (e) => withButton(e, () => post({ action: 'download',
                  slot_id: slot.id }, 'Secili B-roll proje deposuna indirildi')) },
                'Secili videoyu indir')) : null);
    })),
    el('p', { class: 'dim', text: broll.source_credit_note || '' }));
}

function longFormEventPanel(events, quota) {
  if (!events.length) return null;
  return el('div', { class: 'card', 'data-testid': 'lf-events' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Olaylar' }),
      badge('planned', `${quota.request_total || 0} istek (tahmini)`)),
    el('div', { class: 'stack' }, events.slice(-12).reverse().map(event => el('div', {
      class: 'spread', 'data-testid': `lf-event-${event.event}` },
      el('span', { class: 'mono', text: event.event }),
      el('span', { class: 'dim', text: String(event.at || '').slice(11, 19) }),
      el('span', { class: 'dim',
        text: [event.chunk_id, event.chapter_index != null ? `bolum ${event.chapter_index}` : '',
               event.latency_sec != null ? `${event.latency_sec}s` : '',
               event.error_kind || ''].filter(Boolean).join(' - ') })))));
}

function parseMomentRanks(value) {
  const raw = String(value || '').trim();
  if (!raw) return [];
  const ranks = raw.split(',').map(item => Number(item.trim()));
  if (ranks.some(rank => !Number.isInteger(rank) || rank < 1)) {
    throw new Error('Moment siralari virgul ile ayrilmis pozitif tam sayilar olmali.');
  }
  return [...new Set(ranks)].sort((a, b) => a - b);
}

function phase0PlanEditor(phase0, selectedMode) {
  const plan = phase0.plan || null;
  const decision = (plan && plan.decision) || {};
  const renderConfig = decision.render || {};
  const isEdit = Boolean(plan);
  const planMode = selectedMode || decision.mode || '';
  const approved = plan && plan.status === 'approved';

  return el('div', { class: 'plan-editor' },
    isEdit ? alertBox('info', 'Plan ayrintilari duzenlenebilir',
      'Alanlari degistirip kaydet. Degisiklikten sonra plan yeniden insan onayi bekler.') : null,
    approved ? alertBox('warn', 'Onayli plan duzenlenecek',
      'Kaydetmek mevcut plan onayini gecersiz kilar ve alt faz kapilarini yeniden kapatir.') : null,
    el('div', { class: 'grid grid--2' },
      inputField({ id: 'plan-count', label: 'Cikti sayisi', type: 'number', required: true,
                   attrs: { min: '1', max: '10', value: decision.output_count || 1 } }),
      inputField({ id: 'plan-duration', label: 'Hedef sure (sn)', type: 'number',
                   required: true,
                   attrs: { min: '5', max: '180',
                            value: decision.target_duration_sec || 60 } }),
      inputField({ id: 'plan-ranks', label: 'Moment siralari',
                   hint: 'Bos birakilirsa Faz 1 en iyi adaylari kullanir.',
                   attrs: { value: (decision.moment_ranks || []).join(', '),
                            placeholder: 'Ornek: 1, 3, 4' } }),
      field({ id: 'plan-render-mode', label: 'Dikey kadraj',
        control: el('select', { id: 'plan-render-mode' },
          el('option', { value: 'blurred_background',
            selected: (renderConfig.render_mode || 'blurred_background') === 'blurred_background' },
            'Bulanik arka plan'),
          el('option', { value: 'center_crop', selected: renderConfig.render_mode === 'center_crop' },
            'Merkez kirpma')) }),
      field({ id: 'plan-caption-theme', label: 'Altyazi temasi',
        control: el('select', { id: 'plan-caption-theme' },
          el('option', { value: 'auto', selected: (renderConfig.caption_theme || 'auto') === 'auto' },
            'Otomatik (konuya gore)'),
          el('option', { value: 'game_of_thrones',
            selected: renderConfig.caption_theme === 'game_of_thrones' },
            'Game of Thrones / Marcellus SC'),
          el('option', { value: 'game_of_thrones_cinzel',
            selected: renderConfig.caption_theme === 'game_of_thrones_cinzel' },
            'Game of Thrones / Cinzel (eski)')) }),
      planMode === 'direct_clip_series'
        ? field({ id: 'plan-subtitle-model', label: 'Altyazi dogrulugu',
            hint: 'Small yerelde dengeli secimdir. Large-v3-turbo GPU ile en yuksek dogruluk icindir.',
            control: el('select', { id: 'plan-subtitle-model' },
              el('option', { value: 'small',
                selected: renderConfig.subtitle_whisper_model_size === 'small' },
                'Small (hizli onizleme)'),
              el('option', { value: 'large-v3-turbo',
                selected: (renderConfig.subtitle_whisper_model_size || 'large-v3-turbo')
                  === 'large-v3-turbo' },
                'Large v3 Turbo (onerilen kalite)')) }) : null,
      planMode === 'direct_clip_series'
        ? field({ id: 'plan-subtitle-language', label: 'Kaynak konusma dili',
            hint: 'Auto, Faz 1 dil tespitini kullanir; kisa kliplerde yanlis tespiti onlemek icin English secilebilir.',
            control: el('select', { id: 'plan-subtitle-language' },
              el('option', { value: 'auto',
                selected: (renderConfig.subtitle_language || 'auto') === 'auto' },
                'Otomatik (Faz 1 tespiti)'),
              el('option', { value: 'en', selected: renderConfig.subtitle_language === 'en' },
                'English'),
              el('option', { value: 'tr', selected: renderConfig.subtitle_language === 'tr' },
                'Turkce')) }) : null),
    el('label', { class: 'check-row' },
      el('input', { id: 'plan-preview', type: 'checkbox',
        checked: renderConfig.preview_mode !== false }),
      el('span', { text: 'Onizleme modu' })),
    planMode === 'narrated_theory'
      ? field({ id: 'plan-language', label: 'Icerik dili', required: true,
          hint: 'Degistirmek plan onayini ve buna bagli script/ses/render '
                + 'onaylarini gecersiz kilar. Mevcut icerik OTOMATIK CEVRILMEZ.',
          control: el('select', { id: 'plan-language', 'aria-required': 'true',
                                  'data-testid': 'plan-language' },
            CONTENT_LANGUAGES.map(([value, label]) => el('option', {
              value,
              selected: (decision.content_language || 'en-US') === value }, label))) })
      : null,
    planMode === 'narrated_theory'
      ? narrationPresetField((decision.narration || {}).preset) : null,
    planMode === 'narrated_theory'
      ? field({ id: 'plan-theory', label: 'Teori / yorum acisi', required: true,
                hint: 'En az 20 karakter; Gemini scripti bu aciya gore uretilir.',
                control: el('textarea', { id: 'plan-theory', 'aria-required': 'true',
                  'aria-describedby': 'plan-theory-error',
                  placeholder: 'Bu sahnedeki karar aslinda...' },
                  decision.theory_angle || '') })
      : null,
    field({ id: 'plan-notes', label: 'Plan notlari',
      control: el('textarea', { id: 'plan-notes', maxlength: 2000,
        placeholder: 'Kurgu ritmi, acilis tercihi, kacinilacak bolumler...' },
        (plan && plan.notes) || '') }),
    requiredLegend(),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn btn--primary', disabled: !planMode || !phase0.assessment,
        'data-testid': isEdit ? 'save-plan' : 'create-plan',
        onclick: async (event) => {
          const rules = [
            { id: 'plan-count', label: 'Cikti sayisi', required: true,
              number: true, min: 1, max: 10 },
            { id: 'plan-duration', label: 'Hedef sure', required: true,
              number: true, min: 5, max: 180 },
          ];
          if (planMode === 'narrated_theory') {
            rules.push({ id: 'plan-theory', label: 'Teori / yorum acisi',
                         required: true, minLength: 20 });
          }
          const values = validateFields(rules);
          if (!values) return;
          const button = event.currentTarget;
          button.disabled = true;
          const result = await guard(async () => {
            const actor = projectActor();
            const body = {
              mode: planMode,
              selected_by: isEdit ? (plan.selected_by || actor) : actor,
              updated_by: actor,
              output_count: values['plan-count'],
              target_duration_sec: values['plan-duration'],
              moment_ranks: parseMomentRanks(
                document.getElementById('plan-ranks').value),
              theory_angle: values['plan-theory'] || '',
              content_language: planMode === 'narrated_theory'
                ? (document.getElementById('plan-language') || {}).value : null,
              narration_preset: planMode === 'narrated_theory'
                ? (document.getElementById('plan-narration') || {}).value : null,
              notes: document.getElementById('plan-notes').value,
              render_config: {
                render_mode: document.getElementById('plan-render-mode').value,
                preview_mode: document.getElementById('plan-preview').checked,
                caption_style: 'active_word_yellow',
                caption_theme: document.getElementById('plan-caption-theme').value,
                ...(planMode === 'direct_clip_series' ? {
                  subtitle_whisper_model_size:
                    document.getElementById('plan-subtitle-model').value,
                  subtitle_language:
                    document.getElementById('plan-subtitle-language').value,
                } : {}),
              },
            };
            return api.request(isEdit ? 'PATCH' : 'POST',
              `/api/projects/${state.projectId}/plan`, { body });
          }, isEdit ? 'Plan guncellendi - yeniden onay gerekli' : 'Plan olusturuldu - simdi onayla');
          button.disabled = false;
          if (result) {
            state.selectedMode = result.plan.decision.mode;
            await refresh();
          }
        },
      }, isEdit ? 'Plan degisikliklerini kaydet' :
        (planMode ? `Plan olustur (${planMode})` : 'Once bir mod sec'))));
}

function uploadProgressPanel(progress) {
  const total = Math.max(0, Number(progress.total) || 0);
  const loaded = Math.min(total || Infinity, Math.max(0, Number(progress.loaded) || 0));
  const determinate = total > 0;
  const percent = determinate ? Math.min(100, Math.round(100 * loaded / total)) : null;
  return el('section', { class: 'upload-live', 'data-testid': 'upload-live' },
    el('div', { class: 'row' },
      el('strong', { text: progress.filename || 'Video' }),
      el('span', { class: 'dim', 'data-testid': 'upload-live-label',
                   text: determinate
                     ? `${formatBytes(loaded)} / ${formatBytes(total)} · %${percent}`
                     : `${formatBytes(loaded)} yuklendi` })),
    el('div', {
      class: `pbar${determinate ? ' pbar--live' : ' pbar--indeterminate'}`,
      role: 'progressbar', 'aria-label': 'Kaynak video yukleme ilerlemesi',
      'aria-valuemin': determinate ? '0' : null,
      'aria-valuemax': determinate ? '100' : null,
      'aria-valuenow': determinate ? String(percent) : null,
    }, el('div', { class: 'pbar__fill', style: determinate ? `width:${percent}%` : null })),
    el('button', {
      class: 'btn btn--danger btn--sm', 'data-testid': 'cancel-upload',
      onclick: () => { if (state.uploadRequest) state.uploadRequest.abort(); },
    }, 'Yuklemeyi durdur'));
}

function screenPhase0() {
  const view = state.project;
  const phase0 = view.phase0;
  const source = (view.project.data || {}).source;
  const savedMode = phase0.plan && phase0.plan.decision && phase0.plan.decision.mode;
  const selectedPlanMode = state.selectedMode || savedMode || null;
  const uploadLimit = Number((view.upload_limits || {}).max_bytes || 0);
  const supportedExtensions = (view.upload_limits || {}).supported_extensions || [];
  const sourceAccept = supportedExtensions.length
    ? `${supportedExtensions.join(',')},video/*`
    : 'video/*';
  const uploading = state.uploadProgress;
  const sourceLibrary = view.source_library || {};
  const libraryVideos = sourceLibrary.videos || [];

  const uploadCard = el('div', { class: 'card' },
    el('h2', { text: '1 · Kaynak video' }),
    source
      ? el('div', { class: 'stack' },
          el('div', { class: 'row' }, badge('completed', 'kayitli'),
            el('strong', { text: source.display_name })),
          el('div', { class: 'dim', text: `${(source.size_bytes / 1048576).toFixed(1)} MB · parmak izi ${String(source.fingerprint_sha256).slice(0, 16)}…` }))
      : el('div', { class: 'stack' },
          el('p', { class: 'muted', text: 'Bu projede henuz kaynak video yok.' }),
          sourceLibrary.configured
            ? (sourceLibrary.available && libraryVideos.length
              ? el('div', { class: 'stack', 'data-testid': 'source-library-panel' },
                  el('strong', { text: 'Drive / kaynak kutuphanesinden al' }),
                  el('select', { id: 'source-library-file',
                    'data-testid': 'source-library-file' },
                    libraryVideos.map(video => el('option', { value: video.name },
                      `${video.name} (${formatBytes(video.size_bytes)})`))),
                  el('button', { class: 'btn btn--primary',
                    'data-testid': 'source-library-import',
                    onclick: (event) => withButton(event, async () => {
                      const input = document.getElementById('source-library-file');
                      const result = await guard(() => api.post(
                        `/api/projects/${state.projectId}/source-library`,
                        { filename: input ? input.value : '', actor: projectActor() }),
                        'Kaynak video kutuphaneden projeye alindi');
                      if (result) await refresh();
                    }) }, 'Secili videoyu projeye al'),
                  el('div', { class: 'dim',
                    text: 'Aktarim sunucu tarafinda yapilir; video tarayici veya Colab proxy uzerinden gecmez.' }))
              : alertBox('warn', 'Kaynak kutuphanesi bos veya erisilemiyor',
                  'Videoyu Drive/YouTubeAutomation/source_videos klasorune ekleyip Yenileye basin.'))
            : null,
          sourceLibrary.configured && libraryVideos.length
            ? el('div', { class: 'dim', text: 'Veya bu cihazdan yeni bir video yukle:' })
            : null,
          uploading ? null : el('input', {
            type: 'file', id: 'source-file', accept: sourceAccept,
            'data-testid': 'source-file',
          }),
          el('div', { class: 'dim',
            text: `Desteklenen kaynaklar: ${supportedExtensions.join(', ').toUpperCase()}. MKV yuklenebilir; moment onizlemeleri tarayicinin oynatabilecegi MP4 olarak uretilir.` }),
          uploadLimit > 0
            ? el('div', { class: 'dim', text: `En fazla ${formatBytes(uploadLimit)}; buyuk videolar RAM'e alinmadan dogrudan diske yazilir.` })
            : null,
          uploading ? uploadProgressPanel(uploading) : null,
          el('button', {
            class: 'btn btn--primary',
            hidden: Boolean(uploading),
            onclick: async (event) => {
              const input = document.getElementById('source-file');
              if (!input.files || !input.files[0]) {
                pushAlert('warn', 'Dosya secilmedi', 'Once bir video dosyasi sec.');
                return;
              }
              event.currentTarget.disabled = true;
              const file = input.files[0];
              await guard(() => uploadSourceFile(file), 'Kaynak video kaydedildi');
              await refresh();
            },
          }, 'Yukle')));

  const assessmentCard = phase0.assessment
    ? el('div', { class: 'card' },
        el('div', { class: 'card__head' },
          el('h2', { text: '2 · Faz 0 degerlendirmesi' }),
          badge('review_required', 'oneri · secim degil')),
        el('div', { class: 'grid grid--2' }, phase0.modes.map(mode => {
          const score = phase0.assessment.mode_scores[mode.key] || {};
          const isRecommended = phase0.assessment.recommended_mode === mode.key;
          return el('button', {
            class: 'mode-option', type: 'button',
            'aria-pressed': String(selectedPlanMode === mode.key),
            disabled: !score.feasible,
            onclick: () => { state.selectedMode = mode.key; render(); },
          },
            el('div', { class: 'mode-option__head' },
              el('span', { class: 'mode-option__title', text: mode.label }),
              el('span', { class: 'row' },
                isRecommended ? badge('planned', 'onerilen') : null,
                badge(score.feasible ? 'completed' : 'failed',
                  score.feasible ? `${score.score}/100` : 'engelli'))),
            el('div', { class: 'mode-option__desc', text: mode.description }),
            el('div', { class: 'dim', text: mode.consumes_api ? 'Uretim API cagrisi YAPAR' : 'Uretim API cagrisi YAPMAZ' }),
            el('div', { class: 'score-bar' },
              el('div', { class: 'score-bar__fill', style: `width:${score.score || 0}%` })),
            el('ul', { class: 'dim' },
              (score.reasons || []).map(reason => el('li', { text: reason })),
              (score.blockers || []).map(blocker => el('li', { text: `ENGEL: ${blocker}` }))));
        })),
        el('details', {}, el('summary', { class: 'muted', text: 'Ham degerlendirme paneli' }),
          el('pre', { class: 'pre', text: phase0.panel || '' })),
        el('button', {
          class: 'btn',
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            await guard(() => submitManualJob('source_assessment'),
              'Kaynak yeniden degerlendirme isi kuyruga alindi');
            await refresh();
          },
        }, 'Degerlendirmeyi yeniden calistir'))
    : el('div', { class: 'card' },
        el('h2', { text: '2 · Faz 0 degerlendirmesi' }),
        el('p', { class: 'muted', text: 'Degerlendirme henuz calistirilmadi.' }),
        el('button', {
          class: 'btn btn--primary', 'data-testid': 'run-assessment', disabled: !source,
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            await guard(() => submitManualJob('source_assessment'),
              'Degerlendirme isi kuyruga alindi');
            await refresh();
          },
        }, 'Kaynagi degerlendir'));

  const planCard = el('div', { class: 'card card--accent' },
    el('div', { class: 'card__head' },
      el('h2', { text: '3 · Isleme plani' }),
      phase0.plan ? badge(phase0.plan.status) : badge('missing', 'olusturulmadi')),
    phase0.plan_problem ? alertBox('danger', 'Plan okunamadi', phase0.plan_problem) : null,
    phase0.plan
      ? el('div', { class: 'stack' },
          el('pre', { class: 'pre', text: phase0.plan_panel || '' }),
          phase0PlanEditor(phase0, selectedPlanMode),
          // Plan approval is gated on this list server-side, so it has to be
          // answerable HERE. A gate the console cannot satisfy is a wall.
          copyrightReviewCard(),
          approvalPanel('processing_plan', 'Plani onayla'))
      : el('div', { class: 'stack' },
          el('div', { class: 'dim', text: `Karari kaydeden: ${projectActor()}` }),
          inputField({ id: 'plan-count', label: 'Cikti sayisi', type: 'number',
                       hint: 'Bos birakilirsa Faz 0 onerisi kullanilir.',
                       attrs: { min: '1', max: '10', placeholder: '1' } }),
          inputField({ id: 'plan-duration', label: 'Hedef sure (sn)', type: 'number',
                       hint: 'Bos birakilirsa Faz 0 onerisi kullanilir.',
                       attrs: { min: '5', max: '180', placeholder: '60' } }),
          field({ id: 'plan-render-mode', label: 'Dikey kadraj',
            control: el('select', { id: 'plan-render-mode' },
              el('option', { value: 'blurred_background' }, 'Bulanik arka plan'),
              el('option', { value: 'center_crop' }, 'Merkez kirpma')) }),
          field({ id: 'plan-caption-theme', label: 'Altyazi temasi',
            control: el('select', { id: 'plan-caption-theme' },
              el('option', { value: 'auto' }, 'Otomatik (konuya gore)'),
              el('option', { value: 'game_of_thrones' },
                'Game of Thrones / Marcellus SC'),
              el('option', { value: 'game_of_thrones_cinzel' },
                'Game of Thrones / Cinzel (eski)')) }),
          state.selectedMode === 'direct_clip_series'
            ? field({ id: 'plan-subtitle-model', label: 'Altyazi dogrulugu',
                control: el('select', { id: 'plan-subtitle-model' },
                  el('option', { value: 'small' }, 'Small (hizli onizleme)'),
                  el('option', { value: 'large-v3-turbo', selected: true },
                    'Large v3 Turbo (onerilen kalite)')) }) : null,
          state.selectedMode === 'direct_clip_series'
            ? field({ id: 'plan-subtitle-language', label: 'Kaynak konusma dili',
                control: el('select', { id: 'plan-subtitle-language' },
                  el('option', { value: 'auto' }, 'Otomatik (Faz 1 tespiti)'),
                  el('option', { value: 'en' }, 'English'),
                  el('option', { value: 'tr' }, 'Turkce')) }) : null,
          state.selectedMode === 'narrated_theory'
            ? field({ id: 'plan-language', label: 'Icerik dili', required: true,
                hint: 'Script, hook ve seslendirme bu dilde uretilir. Dil plan '
                      + 'kararinin bir parcasidir; degistirmek script/ses/render '
                      + 'onaylarini gecersiz kilar.',
                control: el('select', { id: 'plan-language', 'aria-required': 'true',
                                        'data-testid': 'plan-language' },
                  CONTENT_LANGUAGES.map(([value, label]) => el('option', {
                    value, selected: state.selectedLanguage === value }, label))) })
            : null,
          state.selectedMode === 'narrated_theory'
            ? narrationPresetField(state.selectedPreset) : null,
          state.selectedMode === 'narrated_theory'
            ? textareaField({ id: 'plan-theory', label: 'Teori / yorum acisi',
                              required: true,
                              hint: 'En az 20 karakter; script bu aciya gore uretilir.',
                              attrs: { placeholder: 'Bu sahnedeki karar aslinda...' } })
            : null,
          el('button', {
            class: 'btn btn--primary', 'data-testid': 'create-plan',
            disabled: !state.selectedMode || !phase0.assessment,
            onclick: async (event) => {
              const rules = [
                { id: 'plan-count', label: 'Cikti sayisi', number: true, min: 1, max: 10 },
                { id: 'plan-duration', label: 'Hedef sure', number: true, min: 5, max: 180 },
              ];
              if (state.selectedMode === 'narrated_theory') {
                rules.push({ id: 'plan-theory', label: 'Teori / yorum acisi',
                             required: true, minLength: 20 });
              }
              const values = validateFields(rules);
              if (!values) return;
              event.currentTarget.disabled = true;
              // An omitted OPTIONAL number is sent as null so the server applies
              // its documented suggestion. A value the user actually typed is
              // never silently swapped for a default.
              await guard(() => api.post(`/api/projects/${state.projectId}/plan`, {
                mode: state.selectedMode,
                selected_by: projectActor(),
                output_count: values['plan-count'],
                target_duration_sec: values['plan-duration'],
                theory_angle: values['plan-theory'] || '',
                content_language: state.selectedMode === 'narrated_theory'
                  ? (document.getElementById('plan-language') || {}).value : undefined,
                narration_preset: state.selectedMode === 'narrated_theory'
                  ? (document.getElementById('plan-narration') || {}).value : undefined,
                render_config: {
                  render_mode: document.getElementById('plan-render-mode').value,
                  preview_mode: true,
                  caption_style: 'active_word_yellow',
                  caption_theme: document.getElementById('plan-caption-theme').value,
                  ...(state.selectedMode === 'direct_clip_series' ? {
                    subtitle_whisper_model_size:
                      document.getElementById('plan-subtitle-model').value,
                    subtitle_language:
                      document.getElementById('plan-subtitle-language').value,
                  } : {}),
                },
              }), 'Plan olusturuldu — simdi onayla');
              await refresh();
            },
          }, state.selectedMode ? `Plan olustur (${state.selectedMode})` : 'Once bir mod sec')));

  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Faz 0 · Kaynak isleme stratejisi' }),
      el('p', { class: 'subtitle', text: 'Plan onaylanmadan hicbir alt faz calismaz.' }))),
    gateSummary(),
    uploadCard, phaseProgress('upload'),
    assessmentCard, phaseProgress('source_assessment'),
    planCard);
}

function gateSummary() {
  const gates = state.project.phase_gates || {};
  return el('div', { class: 'card' },
    el('h3', { text: 'Faz kapilari (plandan turetilir)' }),
    el('div', { class: 'grid grid--3' }, Object.entries(gates).map(([phase, gate]) =>
      el('div', { class: 'stat' },
        el('div', { class: 'row' }, badge(gate.allowed ? 'completed' : 'missing',
          gate.allowed ? 'acik' : 'kapali'), el('strong', { text: phase })),
        gate.reason ? el('div', { class: 'dim', text: gate.reason }) : null))));
}

function screenJobs() {
  const summary = state.project.job_summary || {};
  const jobs = state.project.jobs || [];
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Is akisi' }),
      el('p', { class: 'subtitle', text: 'Durum sunucuda saklanir; sayfa yenilense de kaybolmaz.' }))),
    phaseProgress('automation'),
    el('div', { class: 'grid grid--3' },
      ['queued', 'running', 'review_required', 'failed', 'completed'].map(key =>
        el('div', { class: 'stat' },
          el('div', { class: 'stat__value', text: String(summary[key] ?? 0) }),
          el('div', { class: 'stat__label', text: key })))),
    el('div', { class: 'card' },
      el('div', { class: 'card__head' }, el('h2', { text: 'Isler' }),
        el('button', { class: 'btn btn--sm', onclick: () => refresh() }, 'Yenile')),
      jobs.length === 0 ? el('div', { class: 'empty', text: 'Henuz is yok.' })
        : el('div', { class: 'table-scroll' }, el('table', {},
            el('thead', {}, el('tr', {},
              el('th', { text: 'Is' }), el('th', { text: 'Durum' }),
              el('th', { text: 'Artefakt' }), el('th', { text: 'Deneme' }),
              el('th', { text: 'Ayrinti' }), el('th', { text: '' }))),
            el('tbody', {}, jobs.map(job => el('tr', {},
              el('td', {}, el('strong', { text: job.job_type }),
                el('div', { class: 'dim mono', text: job.job_id })),
              el('td', {}, badge(job.status)),
              el('td', {}, job.artifact_status ? badge(job.artifact_status) : el('span', { class: 'dim', text: '-' })),
              el('td', { class: 'dim', text: `${job.attempts || 0}/${job.max_attempts || 3}` }),
              el('td', {}, job.error
                ? el('div', { class: 'muted', text: job.error })
                : el('span', { class: 'dim', text: '-' })),
              el('td', {}, el('div', { class: 'job-actions' },
                cancelJobAction(job),
                job.can_retry ? el('button', {
                    class: 'btn btn--sm',
                    'data-testid': `retry-job-${job.job_id}`,
                    onclick: async (event) => {
                      event.currentTarget.disabled = true;
                      await guard(() => api.post(
                        `/api/projects/${state.projectId}/jobs/${job.job_id}/retry`, {}),
                        'Is yeniden kuyruga alindi');
                      await refresh();
                    },
                  }, 'Yeniden dene')
                : el('span', { class: 'dim', text: job.status === 'failed' ? 'deneme hakki yok' : '' }))))))))));
}

function momentAnalysisControls(hasMoments) {
  const gate = (state.project.phase_gates || {}).video_analysis || {};
  const params = state.runParams.video_analysis;
  const sourceDuration = Number(((((state.project.phase0 || {}).assessment || {}).source || {})
    .duration_sec) || 0);
  const focusOptions = [
    ['start', 'Video basina odaklan'],
    ['middle', 'Video ortasina odaklan'],
    ['end', 'Video sonuna odaklan'],
    ['custom', 'Ozel dakika araligi'],
  ];
  const durationSec = Math.min(sourceDuration || Infinity,
    Math.max(0, Number(params.clip_duration_min) || 0) * 60);
  let startSec = 0;
  if (params.focus_region === 'middle') startSec = Math.max(0, (sourceDuration - durationSec) / 2);
  if (params.focus_region === 'end') startSec = Math.max(0, sourceDuration - durationSec);
  if (params.focus_region === 'custom') {
    startSec = Math.min(Math.max(0, Number(params.custom_start_min) || 0) * 60,
      Math.max(0, sourceDuration - durationSec));
  }
  return el('section', { class: 'card moment-settings' },
    el('h2', { text: 'Aday uretimi' }),
    el('p', { class: 'subtitle',
              text: 'Kaynak araligini ve kac farkli aday istedigini belirle.' }),
    !gate.allowed ? alertBox('warn', 'Bu faz plan tarafindan kapatildi', gate.reason) : null,
    el('div', { class: 'moment-settings__fields' },
      field({ id: 'va-focus', label: 'Videoda odak bolgesi', control: el('select', {
        id: 'va-focus',
        onchange: (event) => {
          params.focus_region = event.currentTarget.value;
          render();
        },
      }, focusOptions.map(([value, label]) => el('option', {
        value, selected: params.focus_region === value,
      }, label))) }),
      numberField('va-duration', 'Incelenecek sure (dakika)', 'video_analysis',
                  'clip_duration_min', { min: '0.5', max: '120', step: '0.5' }),
      params.focus_region === 'custom'
        ? numberField('va-custom-start', 'Ozel baslangic (dakika)', 'video_analysis',
                      'custom_start_min', { min: '0', step: '0.5' }) : null,
      numberField('va-topn', 'Aday sayisi (hedef)', 'video_analysis',
                  'top_n', { min: '1', max: '20', step: '1' }),
      selectField('va-whisper', 'Whisper modeli', 'video_analysis',
                  'whisper_model_size', ['tiny', 'small', 'large-v3-turbo']),
      selectField('va-scene', 'Sahne dedektoru', 'video_analysis',
                  'scene_detector', ['content', 'adaptive', 'threshold'])),
    sourceDuration > 0
      ? el('div', { class: 'analysis-range-preview', 'data-testid': 'analysis-range-preview' },
          el('strong', { text: 'Analiz araligi: ' }),
          el('span', { text: `${formatMediaTime(startSec)} - ${formatMediaTime(startSec + durationSec)}` }),
          el('span', { class: 'dim', text: ` / toplam ${formatMediaTime(sourceDuration)}` }))
      : alertBox('warn', 'Video suresi bilinmiyor',
                 'Once Faz 0 kaynak degerlendirmesini calistir.'),
    el('button', {
      class: 'btn btn--primary moment-settings__run', 'data-testid': 'run-video_analysis',
      disabled: !gate.allowed,
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        state.selectedMoments.clear();
        await guard(() => submitManualJob('video_analysis', { ...params }),
                    'Analiz isi kuyruga alindi');
        await refresh();
      },
    }, hasMoments ? 'Yeni adaylar uret' : 'Adaylari uret'),
    phaseProgress('video_analysis'));
}

function screenMoments() {
  const moments = state.project.moments;
  const gate = (state.project.phase_gates || {}).moment_selection || {};
  const selection = moments.selection;
  const staleSelection = Boolean(moments.selection_problem);
  const candidates = moments.data || [];
  const summary = moments.candidate_summary || {};
  const selectedRank = [...state.selectedMoments][0] || null;
  const produced = Number(summary.produced ?? candidates.length);
  const requested = Number(summary.requested ?? produced);
  const countComplete = produced === requested;

  const resultSummary = el('section', { class: 'card moment-summary',
                                        'data-testid': 'candidate-summary' },
    el('div', { class: 'moment-summary__count' },
      el('strong', { text: `${produced} / ${requested}` }),
      el('span', { text: 'aday hazir' })),
    el('div', { class: 'moment-summary__facts' },
      summary.analysis_duration_sec != null
        ? el('span', { text: `${formatMediaTime(summary.analysis_duration_sec)} analiz edildi` }) : null,
      summary.window_sec != null
        ? el('span', { text: `${summary.window_sec} sn aday penceresi` }) : null,
      selection && !staleSelection
        ? el('span', { class: 'moment-summary__selected',
                      text: `Kayitli secim: #${selection.selected_moment_rank}` })
        : el('span', { text: 'Henuz kayitli secim yok' })),
    !countComplete && requested > 0
      ? alertBox('warn', 'Istenen aday sayisina ulasilamadi',
                 `${requested} aday istendi, ${produced} aday uretildi.`,
                 'Klip suresini uzatip adaylari yeniden uret.') : null);

  const selectionPanel = el('section', { class: 'card moment-selection-panel' },
    el('h2', { text: 'Secimi tamamla' }),
    el('p', { class: 'subtitle',
              text: selectedRank ? `Aday #${selectedRank} secildi.`
                                 : 'Once bir adayi izle ve sec.' }),
    el('button', {
      class: 'btn btn--primary', 'data-testid': 'save-moment-selection',
      disabled: selectedRank === null || !gate.allowed,
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await guard(() => api.post(`/api/projects/${state.projectId}/moment-selection`, {
            selected_rank: selectedRank,
            reviewed_by: projectActor(),
            require_preview: false,
          }), 'Moment secimi kaydedildi');
        } finally {
          button.disabled = false;
          await refresh();
        }
      },
    }, 'Bu adayi kaydet'),
    selection && !staleSelection
      ? el('div', { class: 'moment-selection-panel__saved',
                    'data-testid': 'moment-selection-state',
                    text: `#${selection.selected_moment_rank} · ${selection.reviewed_by}` })
      : null);

  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Moment Explorer' }),
      el('p', { class: 'subtitle',
                text: 'Adaylari yan yana izle, karsilastir ve tek bir moment sec.' }))),
    moments.problem ? alertBox('danger', 'hot_moments okunamadi', moments.problem) : null,
    staleSelection
      ? alertBox('danger', 'Kayitli moment secimi artik gecerli degil',
                 moments.selection_problem,
                 'Analiz yeniden calistirildi; adaylari tekrar izleyip yeni bir secim kaydet.')
      : null,
    el('div', { class: 'moment-workspace' },
      el('main', { class: 'moment-results' },
        candidates.length ? resultSummary : null,
        candidates.length
          ? el('div', { class: 'moment-gallery' }, candidates.map(moment => el('article', {
            class: 'moment-card', role: 'group', 'data-testid': `moment-${moment.rank}`,
            'aria-pressed': String(selectedRank === moment.rank),
          },
            el('div', { class: 'moment-card__meta' },
              el('strong', { text: `Aday #${moment.rank}` }),
              el('span', { text: `${formatMediaTime(moment.source_start_sec)} - ${formatMediaTime(moment.source_end_sec)}` }),
              el('span', { text: `Skor ${moment.score}` }),
              // A loud but wordless window scores like any other candidate, and
              // only reveals itself after a render comes back with no subtitles.
              // Said here, where the choice is actually made.
              moment.has_speech === false
                ? badge('failed', 'konusma yok') : null,
              selection && selection.selected_moment_rank === moment.rank && !staleSelection
                ? badge('approved', 'kayitli') : null),
            moment.has_speech === false
              ? el('div', { class: 'dim', 'data-testid': `no-speech-${moment.rank}`,
                  text: 'Bu adayda konusma yok (muzik/aksiyon olabilir). Secilirse Short '
                        + 'ALTYAZISIZ uretilir; altyazi istiyorsan konusma iceren bir aday sec.' })
              : null,
            moment.preview_problem
              ? el('div', { class: 'dim', text: moment.preview_problem })
              : moment.preview_path
                ? el('video', { controls: true, preload: 'none',
                    'data-testid': `preview-${moment.rank}`,
                    src: `/api/projects/${state.projectId}/media/${encodeURI(String(moment.preview_path))}` })
                : el('div', { class: 'dim', text: 'preview uretilmedi' }),
            el('details', { class: 'moment-card__details' },
              el('summary', { text: 'Skor ayrintisi' }),
              el('div', { class: 'muted', text: moment.reason || 'Ayrinti yok.' })),
            el('button', {
              class: selectedRank === moment.rank ? 'btn btn--primary' : 'btn btn--sm',
              'data-testid': `select-moment-${moment.rank}`,
              onclick: () => {
                state.selectedMoments.clear();
                state.selectedMoments.add(moment.rank);
                render();
              },
            }, selectedRank === moment.rank ? 'Secildi' : 'Bu adayi sec'))))
          : el('div', { class: 'card' },
              el('div', { class: 'empty', 'data-testid': 'moments-empty',
                          text: 'Henuz aday yok. Sag taraftaki ayarlardan adaylari uret.' }))),
      el('aside', { class: 'moment-sidebar' },
        momentAnalysisControls(candidates.length > 0),
        candidates.length ? selectionPanel : null)));
}


function approvalPanel(kind, actionLabel) {
  const approval = (state.project.approvals || {})[kind] || {};
  if (!approval.present) {
    return el('div', { class: 'muted', text: 'Onaylanacak artefakt henuz uretilmedi.' });
  }
  // The plan carries a checklist gate like the render and voice screens do, and
  // the same rule applies: hide the button rather than let the click bounce off
  // a server refusal. `PipelineService.approve` refuses independently, so this
  // is a courtesy, not the gate itself.
  if (kind === 'processing_plan') {
    const rights = (state.project.copyright_review) || {};
    const rightsOpen = Boolean(rights.complete && !rights.open_revision);
    if ((rights.items || []).length && !rightsOpen) {
      return alertBox('warn', 'Once telif kontrol listesini tamamla',
        rights.blocker || 'Plan onayi telif sorulari cevaplanmadan acilmaz.',
        'Sorular yukaridaki karttadir.');
    }
  }
  return el('div', { class: 'stack' },
    el('div', { class: 'row' },
      badge(approval.status || 'missing'),
      approval.approved ? badge(approval.effective_valid ? 'approved' : 'failed',
        approval.effective_valid ? 'onay gecerli' : 'ONAY GECERSIZ') : badge('review_required', 'onay bekliyor')),
    approval.approved && !approval.effective_valid
      ? alertBox('danger', 'Onay artik gecerli degil', approval.reason,
          (approval.upstream_invalid || []).length
            ? `Yukari-akis degisti: ${(approval.upstream_invalid || []).join(', ')}. Once onu duzelt.`
            : 'Icerik onaydan sonra degismis. Yeniden incele ve tekrar onayla.')
      : null,
    approval.blocker ? alertBox('warn', 'Onay engelli', approval.blocker,
      (approval.blocking_conditions || []).join(', ')) : null,
    approval.reviewable
      ? el('div', {},
          el('p', { class: 'dim',
            text: `Onay ${projectActor()} adina, ekranda inceledigin guncel icerige baglanir.` }),
          el('button', {
            class: 'btn btn--primary', 'data-testid': `approve-${kind}`,
            disabled: !approval.content_sha256,
            onclick: async (event) => {
              event.currentTarget.disabled = true;
              await guard(() => api.post(`/api/projects/${state.projectId}/approvals/${kind}`, {
                reviewed_sha256: approval.content_sha256,
                approved_by: projectActor(),
              }), 'Onaylandi');
              await refresh();
            },
          }, actionLabel))
      : null);
}


/* ------------------------------------------------------- progress panel */
/* One tqdm-style panel per active phase screen. Server-persisted, so a refresh
 * or a restarted server shows the same bar at the same stage.
 *
 * HONESTY: `determinate` comes from the server and is true only when something
 * countable was counted. When it is false the bar is indeterminate -- this code
 * has no path that invents a percentage.
 *
 * Rendered through `phaseProgress(activity)`, which every screen calls at most
 * once, so two panels for the same activity cannot appear. */
const PROGRESS_STATE_LABEL = {
  running: 'calisiyor', completed: 'tamamlandi', failed: 'basarisiz',
  interrupted: 'yarida kesildi', stopped: 'durdu', idle: 'beklemede',
};
const PROGRESS_STATE_BADGE = {
  running: 'running', completed: 'completed', failed: 'failed',
  interrupted: 'failed', stopped: 'review_required', idle: 'missing',
};

function formatElapsed(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  return minutes ? `${minutes}dk ${String(total % 60).padStart(2, '0')}sn` : `${total}sn`;
}

function progressBar(record) {
  const determinate = Boolean(record.determinate)
    && record.percent !== null && record.percent !== undefined;
  const running = record.state === 'running';
  const bar = el('div', {
    class: `pbar${determinate ? '' : ' pbar--indeterminate'}`
           + (running ? ' pbar--live' : ''),
    'data-testid': `progress-bar-${record.activity}`,
    'data-determinate': String(determinate),
    role: 'progressbar',
    'aria-label': `${record.label}: ${record.message || ''}`,
    // An indeterminate bar deliberately omits aria-valuenow: assistive tech
    // reads that as "unknown", which is the truth.
    'aria-valuemin': determinate ? '0' : null,
    'aria-valuemax': determinate ? '100' : null,
    'aria-valuenow': determinate ? String(record.percent) : null,
    'aria-valuetext': determinate
      ? `%${record.percent} · ${record.completed}/${record.total} ${record.unit || ''}`.trim()
      : 'ilerleme olculemiyor',
    'aria-busy': running ? 'true' : 'false',
  }, el('div', {
    class: 'pbar__fill',
    style: determinate ? `width:${record.percent}%` : null,
  }));
  return bar;
}

function progressStages(record) {
  const stages = record.stages || [];
  if (!stages.length) return null;
  return el('ol', { class: 'pstages', 'data-testid': `progress-stages-${record.activity}` },
    stages.map(stage => el('li', {
      class: `pstage pstage--${stage.state || 'waiting'}`,
      'data-testid': `pstage-${record.activity}-${stage.key}`,
      'data-state': stage.state || 'waiting',
    },
      el('span', { class: 'pstage__mark', 'aria-hidden': 'true',
        text: { completed: '✓', running: '▸', failed: '✕' }[stage.state] || '·' }),
      el('span', { class: 'pstage__label', text: stage.label }))));
}

function cancelJobAction(job, label = 'Durdur') {
  if (!job) return null;
  if (job.cancel_requested) {
    return el('span', { class: 'dim', 'data-testid': `cancel-pending-${job.job_id}`,
                        text: 'Durdurma bekleniyor...' });
  }
  if (!job.can_cancel) return null;
  return el('button', {
    class: 'btn btn--danger btn--sm',
    'data-testid': `cancel-job-${job.job_id}`,
    onclick: async (event) => {
      event.currentTarget.disabled = true;
      await guard(() => api.post(
        `/api/projects/${state.projectId}/jobs/${job.job_id}/cancel`, {}),
      'Durdurma istegi gonderildi');
      await refresh();
    },
  }, label);
}

/* Activities already painted in THIS render pass. A second call for the same
 * activity returns null, so no screen can end up with two bars for one phase
 * however the components are composed. Reset by `render()`. */
const renderedProgress = new Set();

function phaseProgress(activity) {
  const record = ((state.project && state.project.progress) || {})[activity];
  if (!record) return null;
  if (renderedProgress.has(activity)) return null;
  renderedProgress.add(activity);
  const determinate = Boolean(record.determinate)
    && record.percent !== null && record.percent !== undefined;
  const failed = record.state === 'failed' || record.state === 'interrupted';
  const items = (record.item_total !== null && record.item_total !== undefined)
    ? `${record.item_completed}/${record.item_total} ${record.item_unit || ''}`.trim()
    : '';
  const activeJob = ((state.project && state.project.jobs) || [])
    .find(job => job.job_id === record.job_id);
  return el('section', {
    class: `pprog pprog--${record.state}`,
    'data-testid': `progress-${activity}`,
    'data-state': record.state,
    'aria-label': `${record.label} ilerlemesi`,
  },
    el('div', { class: 'pprog__head' },
      el('strong', { text: record.label }),
      badge(PROGRESS_STATE_BADGE[record.state] || 'missing',
            PROGRESS_STATE_LABEL[record.state] || record.state),
      record.is_retry
        ? el('span', { class: 'pprog__retry', 'data-testid': `progress-attempt-${activity}`,
                       text: `${record.attempt}. deneme` })
        : null,
      el('span', { class: 'pprog__meta', 'data-testid': `progress-meta-${activity}` },
        determinate
          ? `%${record.percent} · ${record.completed}/${record.total} ${record.unit || ''}`.trim()
          : 'ilerleme olculemiyor',
        ' · ',
        el('span', { 'data-testid': `progress-elapsed-${activity}` },
           formatElapsed(record.elapsed_sec)))),
    progressBar(record),
    // A SECOND measured bar when the running stage counts something of its own
    // (encoded seconds, generated clips). Deliberately not blended into the
    // stage bar above: stages do not take equal time, so combining them would
    // produce a number that is not a measurement of anything.
    (record.item_total && record.state === 'running')
      ? el('div', { class: 'pbar pbar--inner', 'data-testid': `progress-inner-${activity}`,
            role: 'progressbar', 'aria-label': `${record.step_label || ''} ayrinti`,
            'aria-valuemin': '0', 'aria-valuemax': '100',
            'aria-valuenow': String(Math.round(
              100 * record.item_completed / record.item_total)),
            'aria-valuetext': `${record.item_completed}/${record.item_total} `
                              + `${record.item_unit || ''}`.trim() },
          el('div', { class: 'pbar__fill',
            style: `width:${Math.round(100 * record.item_completed / record.item_total)}%` }))
      : null,
    el('div', { class: 'pprog__line' },
      el('span', { 'data-testid': `progress-step-${activity}`,
                   text: record.step_label || record.message || '-' }),
      items ? el('span', { class: 'dim mono', 'data-testid': `progress-items-${activity}`,
                           text: items }) : null),
    record.message && record.message !== record.step_label
      ? el('div', { class: 'dim', 'data-testid': `progress-message-${activity}`,
                    text: record.message })
      : null,
    progressStages(record),
    (activeJob && (activeJob.can_cancel || activeJob.cancel_requested))
      ? el('div', { class: 'pprog__actions' }, cancelJobAction(activeJob)) : null,
    failed
      ? el('div', { class: 'pprog__failure', 'data-testid': `progress-failure-${activity}` },
          el('div', { class: 'pprog__failed-step',
                      text: `Son adim: ${record.failed_step_label || record.step_label || '-'}` }),
          record.error
            ? el('div', { class: 'mono', 'data-testid': `progress-error-${activity}`,
                          text: record.error })
            : null,
          progressRetryAction(activity, record))
      : null,
    record.state === 'stopped' && record.blocker
      ? el('div', { class: 'dim', 'data-testid': `progress-blocker-${activity}`,
                    text: `Bekleniyor: ${record.blocker}` })
      : null);
}

/* The retry button reuses the SAME job-retry endpoint the jobs screen uses --
 * there is no second way to re-run a phase. */
function progressRetryAction(activity, record) {
  const job = ((state.project && state.project.jobs) || [])
    .find(item => item.job_id === record.job_id)
    || ((state.project && state.project.jobs) || [])
        .find(item => item.job_type === activity);
  if (!job || !job.can_retry) {
    return el('div', { class: 'dim', 'data-testid': `progress-retry-hint-${activity}`,
                       text: 'Yeniden calistirmak icin bu fazin calistirma kartini kullan.' });
  }
  return el('button', {
    class: 'btn btn--sm', 'data-testid': `progress-retry-${activity}`,
    onclick: async (event) => {
      event.currentTarget.disabled = true;
      await guard(() => api.post(
        `/api/projects/${state.projectId}/jobs/${job.job_id}/retry`, {}), 'Yeniden deneniyor');
      await refresh();
    },
  }, 'Yeniden dene');
}

/* One place that answers "what is this phase doing right now?" -- active job,
 * latest result, failure reason and a retry button, inside the tab that owns
 * the phase. Built from the server view model; nothing is derived here. */
function phaseStatusPanel(jobType) {
  const jobs = ((state.project && state.project.jobs) || [])
    .filter(job => job.job_type === jobType);
  if (!jobs.length) {
    return el('div', { class: 'dim', 'data-testid': `status-${jobType}`,
                       text: 'Bu faz henuz calistirilmadi.' });
  }
  const latest = jobs[0];
  const active = jobs.find(job => job.status === 'queued' || job.status === 'running');
  return el('div', { class: 'stack', 'data-testid': `status-${jobType}` },
    el('div', { class: 'row' },
      el('strong', { text: 'Son calistirma' }), badge(latest.status),
      latest.artifact_status ? badge(latest.artifact_status) : null,
      el('span', { class: 'dim', text: `deneme ${latest.attempts}/${latest.max_attempts}` })),
    active ? alertBox('info', 'Bu faz su anda calisiyor',
                      `${active.job_type} · ${active.status}`) : null,
    active ? cancelJobAction(active) : null,
    latest.error
      ? alertBox('danger', 'Son calistirma basarisiz', latest.error,
                 latest.can_retry ? 'Asagidaki "Yeniden dene" ile guvenle tekrarlanabilir.'
                                  : 'Deneme hakki kalmadi; ayarlari degistirip yeni bir calistirma baslat.')
      : null,
    latest.result && Object.keys(latest.result).length
      ? el('div', { class: 'dim mono', text: JSON.stringify(latest.result) })
      : null,
    latest.can_retry
      ? el('button', {
          class: 'btn btn--sm', 'data-testid': `retry-${jobType}`,
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            await guard(() => api.post(
              `/api/projects/${state.projectId}/jobs/${latest.job_id}/retry`, {}),
              'Is yeniden kuyruga alindi');
            await refresh();
          },
        }, 'Yeniden dene')
      : null);
}

/* Run parameters always carry a value the user can see and change, so they are
 * genuinely optional -- there is no missing input to silently default. */
function numberField(id, label, jobType, key, attrs = {}) {
  return field({ id, label, control: el('input', {
    id, type: 'number', value: String(state.runParams[jobType][key]), ...attrs,
    onchange: (event) => {
      const value = Number(event.currentTarget.value);
      state.runParams[jobType][key] = Number.isFinite(value)
        ? value : state.runParams[jobType][key];
    },
  }) });
}

function selectField(id, label, jobType, key, options) {
  return field({ id, label, control: el('select', {
    id,
    onchange: (event) => { state.runParams[jobType][key] = event.currentTarget.value; },
  }, options.map(option => el('option', {
    value: option, selected: state.runParams[jobType][key] === option,
  }, option))) });
}

function phaseRunCard(jobType, title, description, params, extraFields) {
  const gate = (state.project.phase_gates || {})[jobType] || {};
  return el('div', { class: 'card' },
    el('h2', { text: title }),
    el('p', { class: 'subtitle', text: description }),
    !gate.allowed ? alertBox('warn', 'Bu faz plan tarafindan kapatildi', gate.reason) : null,
    extraFields || null,
    el('button', {
      class: 'btn btn--primary', 'data-testid': `run-${jobType}`, disabled: !gate.allowed,
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        // A deliberate click is a NEW run request id, so it genuinely reruns;
        // a double-click / retried POST reuses it and deduplicates.
        await guard(() => submitManualJob(jobType, typeof params === 'function' ? params() : (params || {})),
                    'Is kuyruga alindi');
        await refresh();
      },
    }, 'Baslat / yeniden calistir'),
    phaseProgress(jobType),
    el('div', { style: 'margin-top:16px' }, phaseStatusPanel(jobType)));
}

/* The caption screen of `direct_clip_series`.
 *
 * This is NOT script generation: no model writes anything here and no provider
 * is called. It shows what the source audio already says, recognised locally,
 * so a human can fix a misheard proper noun before it is burned into the video.
 * Corrections are word-level on purpose -- they keep the alignment timings that
 * drive the yellow active-word highlight, which free-text editing would destroy.
 */
/* Segment timestamps read as a clock, not a float: "0:07 - 0:11" is a place in
 * the clip, "7.24" is a number. */
function formatClock(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '-';
  const total = Math.max(0, Math.round(value));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/* Altyazi inceleme ve duzeltme.
 *
 * The screen used to show counts and nothing else, so a misheard proper noun
 * could not be seen -- let alone fixed -- before it was burned into the video.
 * Now the whole transcript is here, one editable row per caption line, with the
 * words the recogniser was unsure about marked so the eye lands on them.
 *
 * Two rules this file has to respect:
 *   1. Drafts live in `state`, not in the DOM. The job poll re-renders every
 *      1.5s and anything typed straight into an input would vanish.
 *   2. "Save" and "Approve" stay separate buttons. Saving changes the content
 *      and therefore DROPS the approval; merging them would approve text the
 *      reviewer had not read in its saved form. */
/* Drafts belong to ONE project and ONE output slot. Segment ids repeat across
 * projects (`seg_0001` exists in every transcript), so an unsaved draft that
 * outlived a project switch would appear under the new project's identical ids
 * and a Save would write the OTHER project's words into this transcript. The
 * owner key is re-checked on every read, exactly as `longFormDraft` does. */
function draftOwner(slot) {
  return `${state.projectId || ''}::${slot === undefined || slot === null ? '' : slot}`;
}

function transcriptDraft(t) {
  const owner = draftOwner(t && t.slot);
  if (!state.transcriptDraft || state.transcriptDraftOwner !== owner) {
    state.transcriptDraft = {};
    state.transcriptDraftOwner = owner;
  }
  return state.transcriptDraft;
}

function transcriptFullDraftValue(t) {
  const owner = draftOwner(t && t.slot);
  if (state.transcriptFullDraftOwner !== owner) {
    state.transcriptFullDraft = null;
    state.transcriptFullDraftOwner = owner;
  }
  return state.transcriptFullDraft;
}

function setTranscriptFullDraft(t, value) {
  state.transcriptFullDraftOwner = draftOwner(t && t.slot);
  state.transcriptFullDraft = value;
}

function glossaryDraftValue() {
  const owner = draftOwner(null);
  if (state.glossaryDraftOwner !== owner) {
    state.glossaryDraft = null;
    state.glossaryDraftOwner = owner;
  }
  return state.glossaryDraft;
}

function setGlossaryDraft(value) {
  state.glossaryDraftOwner = draftOwner(null);
  state.glossaryDraft = value;
}

function transcriptDirty(t) {
  const draft = transcriptDraft(t);
  return (t.segments || []).some(seg => draft[seg.id] !== undefined
                                        && draft[seg.id] !== seg.text);
}

/* Buttons must come back even when the request fails or times out: the API
 * client aborts at 30s, and a permanently disabled Save is indistinguishable
 * from a frozen app. */
async function withButton(event, action) {
  const button = event.currentTarget;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Kaydediliyor...';
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function screenTranscript() {
  const t = state.project.transcript || {};
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Altyazi (orijinal ses)' }),
      el('p', { class: 'subtitle',
                text: 'Kaynak videonun kendi sesi yaziya dokulur. Model yeni metin '
                      + 'YAZMAZ, hicbir API cagrilmaz. Render bu metni kullanir.' }))),
    !t.phase_allowed
      ? alertBox('info', 'Bu planda altyazi transkripti kullanilmiyor',
                 t.phase_reason || '')
      : null,
    phaseRunCard('transcript', 'Transkripti uret',
      'Secilen momentin orijinal sesi yerel Whisper ile yaziya dokulur.', () => ({
        subtitle_whisper_model_size: planSubtitleModel(),
        subtitle_language: planSubtitleLanguage(),
        caption_theme: planCaptionTheme(),
      })),
    t.problem ? alertBox('danger', 'Transkript okunamadi', t.problem) : null,
    t.available && t.has_speech === false
      ? alertBox('warn', 'Bu momentte konusma yok',
                 'Altyazi uretilemez. Altyazili bir Short istiyorsan Moment '
                 + 'Explorer\'da konusma iceren bir aday sec.')
      : null,
    glossaryPanel(t.glossary || {}, t.suggestions || []),
    t.available && t.has_speech ? transcriptSummaryCard(t) : null,
    t.available && t.has_speech ? transcriptSegmentsPanel(t) : null,
    t.available && t.has_speech ? transcriptFullTextPanel(t) : null,
    t.available && t.has_speech ? transcriptHistoryPanel(t) : null,
    t.available && t.has_speech
      ? el('div', { class: 'card' }, el('h2', { text: 'Altyazi onayi' }),
          el('p', { class: 'dim',
                    text: 'Onaylanmadan render acilmaz. Metni degistirirsen onay '
                          + 'duser ve yeniden onaylaman gerekir.' }),
          transcriptDirty(t)
            ? alertBox('warn', 'Kaydedilmemis degisiklik var',
                       'Once "Degisiklikleri kaydet" de. Onay kaydedilmis metne '
                       + 'baglanir, ekranda yazdigina degil.')
            : null,
          approvalPanel('transcript', 'Altyaziyi onayla'))
      : null);
}

function transcriptSummaryCard(t) {
  const ctx = t.glossary_context || {};
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Ozet' }),
      badge(t.status === 'approved' ? 'approved' : 'review_required', t.status)),
    el('div', { class: 'dim' },
      `${t.word_count} kelime - ${t.cue_count} satir - dil ${t.language || '?'} - `
      + `${t.whisper_model_size} - ortalama guven `
      + `${t.average_word_probability != null ? t.average_word_probability : '?'}`),
    t.low_confidence_count
      ? el('div', { class: 'dim', 'data-testid': 'low-confidence-note',
          text: `${t.low_confidence_count} kelime dusuk guvenli (asagida isaretli). `
                + 'Ozel isimler genelde burada yanlis cikar.' })
      : null,
    t.excluded_cue_count
      ? el('div', { class: 'dim', 'data-testid': 'excluded-caption-note',
          text: `${t.excluded_cue_count} satir transkriptte korunuyor ancak videoda `
                + 'altyazi olarak gosterilmeyecek.' })
      : null,
    ctx.term_count
      ? el('div', { class: 'dim', 'data-testid': 'glossary-context-note',
          text: `Bu transkript ${ctx.term_count} sozluk terimi baglam olarak `
                + 'verilerek uretildi.' })
      : null,
    (ctx.terms_dropped || []).length
      ? alertBox('warn', 'Bazi terimler modele verilemedi',
                 `Uzunluk siniri nedeniyle ${ctx.terms_dropped.length} terim `
                 + 'disarida kaldi, yani bu transkripti etkilemediler: '
                 + ctx.terms_dropped.join(', '))
      : null);
}

function transcriptSegmentsPanel(t) {
  const draft = transcriptDraft(t);
  const segments = t.segments || [];
  const limits = t.limits || {};
  return el('div', { class: 'card', 'data-testid': 'transcript-segments' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Transkript' }),
      badge(transcriptDirty(t) ? 'review_required' : 'completed',
            transcriptDirty(t) ? 'kaydedilmedi' : `${segments.length} segment`)),
    el('p', { class: 'dim',
      text: 'Her satir ekranda bir anda gorunen altyazidir. Metni duzenleyebilirsin; '
            + 'zaman damgalari ve kelime zamanlamalari korunur.' }),
    el('div', { class: 'stack' }, segments.map(seg => {
      const value = draft[seg.id] !== undefined ? draft[seg.id] : seg.text;
      const tooLong = value.length > (limits.max_segment_chars || 220);
      const empty = !value.trim();
      return el('div', { class: 'moment-card', 'data-testid': `seg-${seg.id}` },
        el('div', { class: 'spread' },
          el('span', { class: 'dim mono', 'data-testid': `seg-time-${seg.id}`,
                       text: `${formatClock(seg.start_sec)} - ${formatClock(seg.end_sec)}` }),
          el('span', { class: 'dim',
            'data-testid': `seg-conf-${seg.id}`,
            text: `guven ${seg.average_probability != null ? seg.average_probability : '-'}`
                  + (seg.low_confidence_count ? ` - ${seg.low_confidence_count} dusuk` : '')
                  + (seg.retimed ? ' - yeniden zamanlandi' : '')
                  + (seg.edited ? ' - elle duzenlendi' : '') })),
        seg.excluded_from_subtitles
          ? alertBox('info', 'Videoda gosterilmeyecek',
                     'Metin ve zamanlama kayitli; bu satir altyazi renderindan dislandi.')
          : null,
        // The unsure words are shown as marked text ABOVE the editor rather than
        // inside it: highlighting inside a textarea would mean a rich editor,
        // and a rich editor is how invisible markup ends up in a caption.
        seg.low_confidence_words && seg.low_confidence_words.length
          ? el('div', { class: 'dim', 'data-testid': `seg-low-${seg.id}` },
              'Dusuk guvenli: ',
              seg.low_confidence_words.map(w => el('span', {
                class: 'transcript-word transcript-word--unsure',
                title: `guven ${w.probability}`,
              }, `${w.text} `)))
          : null,
        el('textarea', {
          class: 'transcript-seg-input' + (empty || tooLong ? ' field--invalid' : ''),
          'data-testid': `seg-input-${seg.id}`, rows: 2, spellcheck: 'false',
          oninput: (event) => {
            state.transcriptDraft[seg.id] = event.currentTarget.value;
            const box = event.currentTarget;
            const note = document.getElementById(`seg-msg-${seg.id}`);
            if (note) {
              note.textContent = validateSegmentValue(box.value, limits);
            }
          },
        }, value),
        el('div', { class: 'field__error', id: `seg-msg-${seg.id}`,
                    'data-testid': `seg-msg-${seg.id}`, role: 'alert',
                    text: validateSegmentValue(value, limits) }),
        el('button', {
          class: 'btn btn--sm', 'data-testid': `seg-exclude-${seg.id}`,
          onclick: (event) => withButton(event, async () => {
            const result = await guard(() => api.post(
              `/api/projects/${state.projectId}/transcript/exclusions`, {
                segment_ids: [seg.id], excluded: !seg.excluded_from_subtitles,
                slot: t.slot,
              }), seg.excluded_from_subtitles
                ? 'Satir yeniden altyaziya alindi'
                : 'Satir altyazidan cikarildi');
            if (result) {
              state.transcriptDraft = {};
              state.project.transcript = result.transcript;
            }
            await refresh();
          }),
        }, seg.excluded_from_subtitles
          ? 'Altyazida yeniden goster'
          : 'Sarki / muzik: altyazida gosterme'));
    })),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'transcript-save',
        onclick: (event) => withButton(event, async () => {
          const payload = (t.segments || [])
            .filter(seg => draft[seg.id] !== undefined && draft[seg.id] !== seg.text)
            .map(seg => ({ id: seg.id, text: draft[seg.id] }));
          if (!payload.length) {
            pushAlert('info', 'Degisiklik yok', 'Kaydedilecek bir duzenleme bulunamadi.');
            return;
          }
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/transcript/segments`,
            { segments: payload, slot: t.slot }), 'Transkript kaydedildi');
          if (result) {
            state.transcriptDraft = {};
            state.project.transcript = result.transcript;
          }
          await refresh();
        }),
      }, 'Degisiklikleri kaydet'),
      el('button', {
        class: 'btn btn--sm', 'data-testid': 'transcript-discard',
        onclick: () => { state.transcriptDraft = {}; render(); },
      }, 'Duzenlemeleri geri al'),
      t.asr_available
        ? el('button', {
            class: 'btn btn--sm', 'data-testid': 'transcript-restore-asr',
            onclick: (event) => withButton(event, async () => {
              const result = await guard(() => api.post(
                `/api/projects/${state.projectId}/transcript/restore-asr`,
                { slot: t.slot }), 'Ham ASR ciktisi geri yuklendi');
              if (result) {
                state.transcriptDraft = {};
                state.project.transcript = result.transcript;
              }
              await refresh();
            }),
          }, 'Ham ASR ciktisina don')
        : null));
}

function validateSegmentValue(value, limits) {
  const max = limits.max_segment_chars || 220;
  if (!String(value || '').trim()) return 'Altyazi metni bos olamaz.';
  if (String(value).length > max) {
    return `Satir cok uzun (${String(value).length}/${max}); dikey videoda tasar.`;
  }
  return '';
}

function transcriptFullTextPanel(t) {
  const draftValue = transcriptFullDraftValue(t);
  const current = draftValue !== null && draftValue !== undefined
    ? draftValue : (t.full_text || '');
  return el('div', { class: 'card', 'data-testid': 'transcript-full' },
    el('h2', { text: 'Tum metni birden duzenle' }),
    el('p', { class: 'dim',
      text: 'Her satir bir segmenttir. Satir EKLEYIP CIKARAMAZSIN: zaman damgalari '
            + 'satirlara bagli. Satir sayisi degisirse kayit reddedilir.' }),
    el('textarea', {
      class: 'transcript-full-input', 'data-testid': 'transcript-full-input',
      rows: Math.min(20, Math.max(4, (t.segments || []).length + 1)),
      spellcheck: 'false',
      oninput: (event) => { setTranscriptFullDraft(t, event.currentTarget.value); },
    }, current),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'transcript-full-save',
        onclick: (event) => withButton(event, async () => {
          const node = document.getElementById('transcript-full-input')
                       || document.querySelector('[data-testid=transcript-full-input]');
          const text = node ? node.value : current;
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/transcript/full-text`,
            { full_text: text, slot: t.slot }), 'Transkript kaydedildi');
          if (result) {
            state.transcriptFullDraft = null;
            state.transcriptDraft = {};
            state.project.transcript = result.transcript;
          }
          await refresh();
        }),
      }, 'Tum metni kaydet'),
      el('button', {
        class: 'btn btn--sm', 'data-testid': 'transcript-full-reset',
        onclick: () => { state.transcriptFullDraft = null; render(); },
      }, 'Sifirla')));
}

function transcriptHistoryPanel(t) {
  const revisions = t.revisions || [];
  return el('div', { class: 'card', 'data-testid': 'transcript-history' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Revizyon gecmisi' }),
      badge(revisions.length ? 'completed' : 'missing', `revizyon ${t.revision || 0}`)),
    t.asr_reconstructed
      ? alertBox('warn', 'Ham ASR ciktisi yeniden kurgulandi',
                 'Bu proje revizyon takibi eklenmeden once uretilmis. Saklanan '
                 + '"ham" metin, diskte bulunan en eski hali -- modelin ne '
                 + 'soyledigi degil.')
      : null,
    revisions.length === 0
      ? el('div', { class: 'empty', 'data-testid': 'transcript-history-empty',
                    text: 'Henuz elle duzenleme yapilmadi.' })
      : el('div', { class: 'stack' }, revisions.slice().reverse().map(rev =>
          el('div', { class: 'spread', 'data-testid': `rev-${rev.revision}` },
            el('span', { text: `#${rev.revision} ${rev.kind}` }),
            el('span', { class: 'dim',
                         text: `${rev.actor} - ${String(rev.at).slice(0, 19)}` }),
            el('span', { class: 'dim mono',
                         text: (rev.changed_segment_ids || []).join(', ') || '-' }),
            el('span', { class: 'dim mono', text: rev.content_sha256_16 || '' })))));
}

function glossaryPanel(glossary, suggestions) {
  const terms = (glossary.terms || []).map(entry => entry.term);
  const draftValue = glossaryDraftValue();
  const current = draftValue !== null && draftValue !== undefined
    ? draftValue : terms.join('\n');
  return el('div', { class: 'card', 'data-testid': 'glossary' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Terimler / Ozel isimler' }),
      badge(terms.length ? 'completed' : 'missing', `${terms.length} terim`)),
    el('p', { class: 'dim', text: glossary.asr_hint || '' }),
    el('textarea', {
      class: 'transcript-full-input', 'data-testid': 'glossary-input', rows: 5,
      spellcheck: 'false', placeholder: 'Her satira bir terim: Rhaena',
      oninput: (event) => { setGlossaryDraft(event.currentTarget.value); },
    }, current),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'glossary-save',
        onclick: (event) => withButton(event, async () => {
          const node = document.querySelector('[data-testid=glossary-input]');
          const lines = String(node ? node.value : current)
            .split('\n').map(line => line.trim()).filter(Boolean);
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/glossary`, { terms: lines }),
            'Sozluk kaydedildi');
          if (result) state.glossaryDraft = null;
          await refresh();
        }),
      }, 'Sozlugu kaydet')),
    (suggestions || []).length
      ? el('div', { class: 'stack', 'data-testid': 'glossary-suggestions' },
          el('h3', { text: 'Transkriptte bulunan eslesmeler' }),
          el('p', { class: 'dim',
            text: 'Bunlar OTOMATIK uygulanmaz. "kesin" olanlar sadece buyuk/kucuk '
                  + 'harf veya noktalama farki; "oneri" olanlar benzerlige dayanir.' }),
          suggestions.map((s, index) => el('div', {
            class: 'spread', 'data-testid': `glossary-suggestion-${index}` },
            el('span', {}, el('span', { class: 'mono', text: s.from }), ' -> ',
                            el('strong', { text: s.to })),
            badge(s.kind === 'certain' ? 'completed' : 'review_required',
                  `${s.kind} ${s.score}`))))
      : null,
    (glossary.suggestions || []).length
      ? el('div', { class: 'stack', 'data-testid': 'glossary-term-suggestions' },
          el('h3', { text: 'Duzeltmelerinden ogrenilenler' }),
          glossary.suggestions.map((item, index) => el('div', {
            class: 'spread', 'data-testid': `glossary-learn-${index}` },
            el('span', {}, el('strong', { text: item.term }),
                           el('span', { class: 'dim', text: ` - ${item.reason || ''}` })),
            el('button', {
              class: 'btn btn--sm', 'data-testid': `glossary-accept-${index}`,
              onclick: (event) => withButton(event, async () => {
                await guard(() => api.post(
                  `/api/projects/${state.projectId}/glossary`,
                  { accept_term: item.term }), 'Terim sozluge eklendi');
                state.glossaryDraft = null;
                await refresh();
              }),
            }, 'Sozluge ekle'))))
      : null);
}

function planSubtitleModel() {
  const cfg = (((state.project.phase0 || {}).plan || {}).decision || {}).render || {};
  return cfg.subtitle_whisper_model_size || 'large-v3-turbo';
}
function planSubtitleLanguage() {
  const cfg = (((state.project.phase0 || {}).plan || {}).decision || {}).render || {};
  return cfg.subtitle_language || 'auto';
}
function planCaptionTheme() {
  const cfg = (((state.project.phase0 || {}).plan || {}).decision || {}).render || {};
  return cfg.caption_theme || 'auto';
}

function screenScript() {
  const decision = ((state.project.phase0 || {}).plan || {}).decision || {};
  // One tab, two meanings, decided by the approved plan: a clip series captions
  // the source's own words, a narrated theory speaks a written script. Showing
  // the narrated panel to a clip project is what made "open script generation"
  // look like the way to get subtitles.
  if (decision.mode && decision.mode !== 'narrated_theory') return screenTranscript();
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Script Studio' }),
      el('p', { class: 'subtitle',
                text: 'Yalnizca narrated_theory planinda calisir; ONAYLI planin saglayici '
                      + 'rotasi kullanilir.' }))),
    decision.theory_angle
      ? el('div', { class: 'card' },
          el('h3', { text: 'Onaylanan teori acisi' }),
          el('div', { class: 'muted', 'data-testid': 'theory-angle',
                      text: decision.theory_angle }),
          el('div', { class: 'dim', text: `hedef sure: ${decision.target_duration_sec}s` }))
      : null,
    phaseRunCard('script_generation', 'Script uret',
      'Mevcut Gemini rotasi kullanilir; uretilen script review_required olur.', {}),
    scriptReadingPanel(),
    el('div', { class: 'card' }, el('h2', { text: 'Script onayi' }),
      approvalPanel('script', 'Scripti onayla')));
}


/* The script, readable. Approving a SHA-256 for text you never saw is not a
 * review, so the content has to be on the page next to the approval panel.
 * Read-only by construction: there is no editor here, because editing would
 * invalidate the hash the approval is bound to. */
function scriptReadingPanel() {
  const section = (state.project && state.project.script) || {};
  if (section.problem) {
    return el('div', { class: 'card' },
      el('h2', { text: 'Uretilen script' }),
      alertBox('danger', 'Script artefakti okunamadi', section.problem));
  }
  const data = section.data;
  if (!data) {
    return el('div', { class: 'card' },
      el('h2', { text: 'Uretilen script' }),
      el('div', { class: 'empty', 'data-testid': 'script-empty',
                  text: 'Henuz script uretilmedi.' }));
  }
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Uretilen script' }),
      badge(data.status || 'missing')),
    data.hook
      ? el('blockquote', { class: 'script-hook', 'data-testid': 'script-hook',
                           text: data.hook })
      // Stated plainly rather than hidden: the prompt asks for a hook, so its
      // absence is information about the response, not a rendering gap.
      : el('div', { class: 'dim', 'data-testid': 'script-hook-missing',
                    text: 'Model bu yanitta ayri bir hook cumlesi dondurmedi.' }),
    el('div', { class: 'script-body', 'data-testid': 'script-text',
                text: data.script_text || '' }),
    el('details', { class: 'script-editor', 'data-testid': 'script-editor' },
      el('summary', { text: 'Script metnini duzenle' }),
      alertBox('warn', 'Bu bir icerik revizyonudur',
        'Kaydetmek script onayini kaldirir; mevcut seslendirme ve render revizyon '
        + 'arsivine tasinir. Script yeniden onaylandiktan sonra ses ve renderi yeniden uret.'),
      requiredLegend(),
      textareaField({ id: 'script-editor-text', label: 'Script metni', required: true,
                      attrs: { maxlength: 20000, 'data-testid': 'script-editor-text',
                               text: data.script_text || '' } }),
      textareaField({ id: 'script-editor-notes', label: 'Revizyon notu',
                      attrs: { maxlength: 1000,
                               placeholder: 'Neyi ve neden degistirdin?' } }),
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'script-editor-save',
        onclick: async (event) => {
          const values = validateFields([
            { id: 'script-editor-text', label: 'Script metni', required: true },
            { id: 'script-editor-notes', label: 'Revizyon notu' },
          ]);
          if (!values) return;
          event.currentTarget.disabled = true;
          const saved = await guard(() => api.request(
            'PATCH', `/api/projects/${state.projectId}/script`, {
              body: { updated_by: projectActor(),
                      script_text: values['script-editor-text'],
                      notes: values['script-editor-notes'] || '' },
            }), 'Script guncellendi; ses ve render yeniden uretilmeli');
          if (saved) await refresh();
        },
      }, 'Script revizyonunu kaydet')),
    el('div', { class: 'grid grid--3', 'data-testid': 'script-meta' },
      techCell('Model', data.model),
      techCell('Saglayici', data.provider),
      techCell('Dil', data.language),
      techCell('Karakter', data.character_count),
      techCell('Tahmini sure', data.estimated_duration_sec
        ? `${data.estimated_duration_sec} sn` : null),
      techCell('Uretim gecikmesi', data.latency_sec
        ? `${Number(data.latency_sec).toFixed(1)} sn` : null)),
    data.status_reason
      ? el('div', { class: 'dim', 'data-testid': 'script-status-reason',
                    text: data.status_reason })
      : null);
}


/* The narration preset picker. Server-supplied list (label, voice and the
 * DOCUMENTED characteristic of that voice), so the browser never invents a
 * voice id and the user can see why a style sounds the way it does. */
function narrationPresetField(selected) {
  const presets = (state.project && state.project.narration_presets)
    || [{ key: 'deep_storyteller', label: 'Deep storyteller (EN)', voice: 'Charon' }];
  const current = presets.find(item => item.key === selected) || presets[0] || {};
  return el('div', {},
    field({ id: 'plan-narration', label: 'Anlatim on ayari', required: true,
      hint: 'Ses ve anlatim yonergesi birlikte secilir; degistirmek ses ve render '
            + 'onaylarini gecersiz kilar.',
      control: el('select', { id: 'plan-narration', 'aria-required': 'true',
                              'data-testid': 'plan-narration',
                              onchange: () => render() },
        presets.map(item => el('option', {
          value: item.key, selected: item.key === (selected || presets[0].key),
        }, `${item.label} · ${item.voice}`))) }),
    current.description
      ? el('div', { class: 'dim', 'data-testid': 'preset-description',
                    style: 'margin:-8px 0 16px', text: current.description })
      : null,
    current.voice_rationale
      ? el('details', { class: 'preset-why' },
          el('summary', { text: `Neden ${current.voice}?` }),
          el('div', { class: 'dim', 'data-testid': 'preset-rationale',
                      text: current.voice_rationale }))
      : null);
}

/* Content languages a narrated plan may choose. Mirrors
 * `source_strategy.CONTENT_LANGUAGES`; the SERVER validates, this is the picker. */
const CONTENT_LANGUAGES = [
  ['en-US', 'English (US)'],
  ['tr-TR', 'Turkce'],
];

const GEMINI_TTS_VOICES = ['Kore', 'Puck', 'Zephyr', 'Charon', 'Fenrir', 'Leda',
                           'Orus', 'Aoede', 'Callirrhoe', 'Autonoe'];
const TTS_LANGUAGES = ['tr-TR', 'en-US', 'de-DE', 'fr-FR', 'es-ES'];

function screenVoice() {
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Voice Studio' }),
      el('p', { class: 'subtitle',
                text: 'Gemini TTS preview modelleri; cikti her zaman review_required.' }))),
    phaseRunCard('tts', 'Seslendirme uret',
      'Script ONAYLI olmadan calismaz. Ses ve dil ayarlanabilir.',
      () => ({ ...state.runParams.tts }),
      el('div', { class: 'grid grid--3' },
        selectField('tts-voice', 'Ses', 'tts', 'voice', GEMINI_TTS_VOICES),
        selectField('tts-lang', 'Dil kodu', 'tts', 'language_code', TTS_LANGUAGES),
        field({ id: 'tts-direction', label: 'Anlatim yonergesi',
          control: el('input', {
            id: 'tts-direction', value: state.runParams.tts.narration_direction,
            placeholder: 'Gergin, merak uyandiran bir tonda oku:',
            onchange: (event) => {
              state.runParams.tts.narration_direction = event.currentTarget.value;
            },
          }) }))),
    voicePlayerPanel(),
    voiceTechnicalPanel(),
    voiceScriptPanel(),
    reviewChecklist('voice'),
    reviewRevisionPanel('voice'),
    voiceRerunAction(),
    reviewApprovalSection('voice', 'Ses onayi', 'Sesi onayla', 'voiceover'));
}

/* The generated voiceover, listenable. Approving a SHA-256 for audio you never
 * heard is not a review, and no container check can hear a mispronounced name.
 *
 * The source goes through the ownership-checked media route with a namespace-
 * RELATIVE path -- the browser never receives, and cannot ask for, a host path.
 * The digest is in the query string so a regenerated file busts the cache
 * instead of playing yesterday's audio under today's hash. */
function voicePlayerPanel() {
  const review = (state.project && state.project.voice_review) || {};
  const relative = review.audio_relative_path;
  if (!relative) {
    return el('div', { class: 'card' },
      el('h2', { text: 'Uretilen seslendirme' }),
      el('div', { class: 'empty', 'data-testid': 'voice-empty',
                  text: 'Henuz seslendirme uretilmedi.' }));
  }
  const digest = (review.media_sha256 || '').slice(0, 16);
  const source = `/api/projects/${state.projectId}/media/${relative}?v=${digest}`;
  const tech = review.technical || {};
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Uretilen seslendirme' }),
      badge(tech.status || 'missing')),
    el('div', { class: 'voice-player' },
      el('audio', {
        'data-testid': 'voice-player', src: source, controls: true,
        preload: 'metadata', crossorigin: 'anonymous',
        'aria-label': 'Uretilen seslendirme',
      }),
      el('div', { class: 'voice-player__bar' },
        el('span', { text: `${tech.voice_id || '-'} · ${tech.language_code || '-'}` }),
        el('span', { class: 'dim mono', 'data-testid': 'voice-digest',
                     text: `ses SHA-256: ${digest || '-'}` }))),
    tech.preview_model
      ? el('div', { class: 'dim', 'data-testid': 'voice-preview-note',
                    text: 'Bu model PREVIEW; cikti her zaman review_required olur ve '
                          + 'ses yeniden uretilirse onceki onay gecersizlesir.' })
      : null);
}

function voiceTechnicalPanel() {
  const review = (state.project && state.project.voice_review) || {};
  const tech = review.technical || {};
  if (!tech.available) return null;
  const match = tech.script_hash_matches;
  return el('div', { class: 'card' },
    el('h2', { text: 'Teknik ozet' }),
    el('div', { class: 'grid grid--3', 'data-testid': 'voice-technical' },
      techCell('Ses', tech.voice_id),
      techCell('Model', tech.model),
      techCell('Dil', tech.language_code),
      techCell('Sure', tech.duration_sec ? `${Number(tech.duration_sec).toFixed(2)} sn` : null),
      techCell('Bicim', tech.format),
      techCell('Ornekleme', tech.sample_rate ? `${tech.sample_rate} Hz` : null),
      techCell('Kanal', tech.channels === 1 ? '1 (mono)' : tech.channels),
      techCell('Bit derinligi', tech.bits_per_sample ? `${tech.bits_per_sample} bit` : null),
      techCell('Boyut', tech.size_bytes ? `${(tech.size_bytes / 1024).toFixed(0)} KB` : null),
      techCell('Anlatim yonergesi', tech.narration_direction),
      techCell('Teknik dogrulama', tech.validation_passed ? 'gecti' : 'GECEMEDI'),
      techCell('artifact_status', tech.status)),
    el('div', { class: 'row', style: 'margin-top:12px; gap:8px; flex-wrap:wrap' },
      el('span', { class: 'dim mono', 'data-testid': 'voice-script-hash',
                   text: `bagli script hash: ${tech.source_script_hash || '-'}` }),
      el('span', { class: 'dim mono', 'data-testid': 'voice-language',
                   text: `dil: ${review.spoken_language || tech.language_code || '-'}` }),
      el('span', { class: 'dim mono', 'data-testid': 'voice-preset',
                   text: `on ayar: ${review.narration_preset || '-'}` }),
      // Three-valued on purpose: "unknown" must never render as "matches".
      match === true
        ? badge('completed', 'script ile eslesiyor')
        : match === false
          ? badge('failed', 'SCRIPT ILE ESLESMIYOR')
          : badge('missing', 'karsilastirilamadi')),
    match === false
      ? alertBox('danger', 'Ses, mevcut scriptten uretilmemis',
                 'Kayitli script hash i su anki script metniyle uyusmuyor.',
                 'Script degistiyse sesi yeniden uret; aksi halde onay gecersiz olur.')
      : null,
    // A language change makes existing audio wrong, not merely stale. Said
    // outright, because nothing else on this page would tell you.
    review.language_mismatch
      ? alertBox('danger', 'Ses plandaki dilde DEGIL',
                 `Plan ${review.planned_language}, ses ${review.spoken_language}.`,
                 'Mevcut ses otomatik cevrilmez; seslendirmeyi yeniden uret.')
      : null,
    review.preset_mismatch
      ? alertBox('danger', 'Ses plandaki anlatim on ayarinda DEGIL',
                 `Plan ${review.planned_voice}, mevcut ses ${review.spoken_voice}.`,
                 'On ayar degisti; seslendirmeyi yeniden uret.')
      : null);
}

/* The words being spoken, so "does it match the script?" is a thing the
 * listener can actually check instead of recall. */
function voiceScriptPanel() {
  const review = (state.project && state.project.voice_review) || {};
  if (!review.script_text) return null;
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Seslendirilen script' }),
      badge(review.script_status === 'approved' ? 'approved' : 'review_required',
            review.script_status || '-')),
    el('p', { class: 'subtitle',
              text: 'Dinlerken takip et: eksik, eklenmis veya degistirilmis cumle var mi?' }),
    el('div', { class: 'script-body', 'data-testid': 'voice-script-text',
                text: review.script_text }));
}

/* The one action that fixes a bad voiceover: run TTS again with the current
 * voice/direction settings. Reuses the SAME manual job submission the run card
 * uses -- there is no second way to produce a voiceover. */
function voiceRerunAction() {
  const review = (state.project && state.project.voice_review) || {};
  if (!review.total_count) return null;
  return el('div', { class: 'card' },
    el('h3', { text: 'Duzeltme' }),
    el('p', { class: 'subtitle',
              text: 'Ses/ton/tempo ayarini yukaridaki calistirma kartindan degistirip '
                    + 'seslendirmeyi yeniden uretebilirsin. Yeni ses, listeyi ve onayi '
                    + 'otomatik olarak sifirlar.' }),
    el('button', {
      class: 'btn', 'data-testid': 'voice-rerun-tts',
      disabled: !((state.project.phase_gates || {}).tts || {}).allowed,
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        await guard(() => submitManualJob('tts', { ...state.runParams.tts }),
                    'Seslendirme yeniden uretiliyor');
        await refresh();
      },
    }, 'Seslendirmeyi yeniden uret'));
}



/* Render review and voice review are the same interaction over different media,
 * so they are ONE component parameterised by kind rather than two that drift.
 * The render ids keep their historical, unprefixed form; voice gets its own. */
const REVIEW_KINDS = {
  render: {
    viewKey: 'render_review', endpoint: 'render-review',
    testid: (name) => name,
    mediaNoun: 'video', regeneratedNoun: 'video yeniden uretilirse',
    revisionLead: 'Bir sorun gordiysen not birak ve duzeltmenin yapilacagi sekmeye git. '
                  + 'Bu islem videoyu DEGISTIRMEZ ve hicbir durumu onaylamaz.',
    targets: [['moments', 'Moment Explorer (klip araligi / moment secimi)'],
              ['script', 'Script Studio (metin / hook revizyonu)'],
              ['voice', 'Voice Studio (seslendirme revizyonu)'],
              ['render', 'Render Studio (yeniden render)'],
              ['phase0', 'Faz 0 (plan / render ayarlari)']],
  },
  copyright: {
    viewKey: 'copyright_review', endpoint: 'copyright-review',
    testid: (name) => `copyright-${name}`,
    mediaNoun: 'kaynak video', regeneratedNoun: 'kaynak degisirse',
    revisionLead: 'Bir dayanak eksik gorunuyorsa not birak. Bu islem hicbir sey '
                  + 'onaylamaz ve sistem telif hakkinda bir sonuca VARMAZ.',
    targets: [['phase0', 'Faz 0 (kaynak / plan)'],
              ['moments', 'Moment Explorer (daha kisa moment sec)']],
  },
  voice: {
    viewKey: 'voice_review', endpoint: 'voice-review',
    testid: (name) => `voice-${name}`,
    mediaNoun: 'ses', regeneratedNoun: 'ses yeniden uretilirse',
    revisionLead: 'Bir sorun duydysan not birak. Bu islem sesi DEGISTIRMEZ ve '
                  + 'hicbir durumu onaylamaz; duzeltmeyi sen baslatirsin.',
    targets: [['voice', 'Voice Studio (ses/ton/tempo ayari, yeniden uret)'],
              ['script', 'Script Studio (metni duzelt)']],
  },
};

/* The rights questions, shown on the plan screen because that is where the
 * gate is. Deliberately reuses the shared checklist component: the digest
 * binding, the staleness warning and the "who ticked it" record are the same
 * rules the render and voice lists follow. */
function copyrightReviewCard() {
  const review = (state.project && state.project.copyright_review) || {};
  if (!(review.items || []).length) return null;
  const source = review.source || {};
  return el('div', { class: 'stack', 'data-testid': 'copyright-review' },
    el('div', { class: 'dim',
      text: `Telif sorulari su kaynak icin: ${source.display_name || '(kaynak yok)'}` }),
    el('p', { class: 'dim',
      text: 'Sistem bu icerigin adil kullanim oldugunu IDDIA ETMEZ. Sorular '
            + 'senin cevaplarindir ve artefakta kaydedilir.' }),
    reviewChecklist('copyright'));
}

function reviewChecklist(kind) {
  const config = REVIEW_KINDS[kind];
  const review = (state.project && state.project[config.viewKey]) || {};
  const items = review.items || [];
  if (!items.length) return null;
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Insan inceleme listesi' }),
      badge(review.complete ? 'completed' : 'review_required',
            `${review.done_count}/${review.total_count}`)),
    el('p', { class: 'subtitle',
              text: `Onay, bu maddelerin HEPSI isaretlenmeden acilmaz. Isaretler `
                    + `${config.mediaNoun} hash ine baglidir; ${config.regeneratedNoun} sifirlanir.` }),
    review.stale_count
      ? alertBox('warn', `Isaretler baska bir ${config.mediaNoun} dosyasina ait`,
                 `${review.stale_count} madde onceki ${config.mediaNoun} icin isaretlenmis.`,
                 review.blocker)
      : null,
    el('div', { class: 'review-progress' },
      el('div', { class: 'review-progress__track' },
        el('div', { class: 'review-progress__fill',
                    style: `width:${Math.round(100 * review.done_count / Math.max(1, review.total_count))}%` })),
      el('span', { class: 'dim', 'data-testid': config.testid('checklist-progress'),
                   text: `${review.done_count}/${review.total_count} tamamlandi` })),
    el('div', { class: 'dim', text: `Inceleyen: ${projectActor()}` }),
    el('div', { class: 'checklist' }, items.map(item => el('div', {
      class: `check-item${item.checked ? ' check-item--done' : ''}`
             + `${item.stale ? ' check-item--stale' : ''}`,
      'data-testid': config.testid(`check-${item.key}`),
    },
      el('input', {
        type: 'checkbox', id: `${kind}-chk-${item.key}`, checked: item.checked,
        'data-testid': config.testid(`checkbox-${item.key}`),
        'aria-label': item.label,
        onchange: async (event) => {
          const box = event.currentTarget;
          const wanted = box.checked;
          const who = projectActor();
          box.disabled = true;
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/${config.endpoint}`,
            { action: 'set_item', key: item.key, checked: wanted, reviewed_by: who }));
          if (!result) box.checked = !wanted;
          box.disabled = false;
          await refresh();
        },
      }),
      el('div', {},
        el('label', { class: 'check-item__label', for: `${kind}-chk-${item.key}`,
                      text: item.label }),
        el('div', { class: 'check-item__q', text: item.question }),
        item.checked && item.checked_by
          ? el('div', { class: 'check-item__meta',
                        text: `${item.checked_by} · ${String(item.checked_at).slice(0, 19)}` })
          : el('div', { class: 'check-item__meta', text: item.fix_hint }))))));
}

/* The approval card, gated by the SAME checklist state the panel above shows.
 * Shared, because "hide the approve button until the list is complete" is the
 * whole point of the checklist and must not be re-decided per screen -- the
 * voice screen shipped without it for exactly that reason.
 *
 * This is a UI courtesy. `PipelineService.approve` refuses independently, so a
 * user who bypasses the browser still cannot approve an unreviewed artifact. */
function reviewApprovalSection(kind, heading, actionLabel, approvalKind) {
  const review = (state.project && state.project[REVIEW_KINDS[kind].viewKey]) || {};
  const config = REVIEW_KINDS[kind];
  const gateOpen = Boolean(review.complete && !review.open_revision);
  return el('div', { class: 'card' },
    el('h2', { text: heading }),
    !gateOpen
      ? alertBox('warn', `${heading} henuz acik degil`,
                 review.blocker || 'Once inceleme listesini tamamla.',
                 'Yukaridaki maddelerin hepsini isaretle; acik revizyon kaydi varsa kapat.')
      : null,
    gateOpen
      ? approvalPanel(approvalKind, actionLabel)
      : el('div', { class: 'dim', 'data-testid': config.testid('approval-locked'),
                    text: 'Onay alani kontrol listesi tamamlaninca acilir.' }));
}

function reviewRevisionPanel(kind) {
  const config = REVIEW_KINDS[kind];
  const review = (state.project && state.project[config.viewKey]) || {};
  const open = review.open_revision;
  const notesId = `${config.testid('revision')}-notes`;
  return el('div', { class: 'card' },
    el('h2', { text: 'Revizyon gerekli' }),
    el('p', { class: 'subtitle', text: config.revisionLead }),
    open
      ? el('div', { class: 'stack' },
          alertBox('warn', 'Acik revizyon kaydi', open.notes,
                   `${open.requested_by} · ${String(open.requested_at).slice(0, 19)} · `
                   + `hedef: ${open.target_tab}`),
          el('div', { class: 'row' },
            el('button', {
              class: 'btn btn--primary', 'data-testid': config.testid('revision-goto'),
              onclick: () => goto(open.target_tab),
            }, 'Duzeltme sekmesine git'),
            el('button', {
              class: 'btn', 'data-testid': config.testid('revision-clear'),
              onclick: async (event) => {
                event.currentTarget.disabled = true;
                const who = projectActor();
                await guard(() => api.post(
                  `/api/projects/${state.projectId}/${config.endpoint}`,
                  { action: 'clear_revision', resolved_by: who }),
                  'Revizyon kaydi kapatildi');
                await refresh();
              },
            }, 'Revizyonu kapat')))
      : el('div', { class: 'stack' },
          requiredLegend(),
          textareaField({ id: notesId, label: 'Ne duzeltilmeli?', required: true,
            attrs: { 'data-testid': notesId,
                     placeholder: kind === 'voice'
                       ? 'Ornek: ozel isim yanlis okunuyor, tempo cok hizli'
                       : 'Ornek: klip cumle ortasinda basliyor, 2 saniye geri al' } }),
          field({ id: `${config.testid('revision')}-target`,
            label: 'Duzeltme nerede yapilacak?',
            control: el('select', { id: `${config.testid('revision')}-target`,
                                    'data-testid': config.testid('revision-target') },
              config.targets.map(([value, label]) =>
                el('option', { value }, label))) }),
          el('button', {
            class: 'btn btn--danger', 'data-testid': config.testid('revision-submit'),
            onclick: async (event) => {
              const values = validateFields([
                { id: notesId, label: 'Revizyon notu', required: true },
              ]);
              if (!values) return;
              event.currentTarget.disabled = true;
              await guard(() => api.post(
                `/api/projects/${state.projectId}/${config.endpoint}`, {
                  action: 'request_revision',
                  requested_by: projectActor(),
                  notes: values[notesId],
                  target_tab: document.getElementById(
                    `${config.testid('revision')}-target`).value,
                }), 'Revizyon kaydedildi');
              await refresh();
            },
          }, 'Revizyon iste')));
}

/* Render Studio review. Every decision shown here comes from
 * `project.render_review`, which the SERVER computes and persists -- ticking a
 * box is an API call that writes an artifact, not browser state. */
function techCell(label, value) {
  return el('div', { class: 'tech-cell' },
    el('div', { class: 'tech-cell__label', text: label }),
    el('div', { class: 'tech-cell__value', text: value === null || value === undefined
                                                  || value === '' ? '-' : String(value) }));
}

function renderTechnicalSummary(tech) {
  if (!tech || !tech.available) {
    return el('div', { class: 'dim', text: 'Teknik ozet icin once render calistir.' });
  }
  return el('div', { class: 'card' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Teknik ozet' }),
      badge(tech.validation_passed ? 'completed' : 'failed',
            tech.validation_passed ? 'teknik dogrulama gecti' : 'teknik dogrulama BASARISIZ')),
    // Technically valid and still not what the mode promised: a Short with no
    // subtitles because the chosen moment had no speech.
    tech.no_speech_detected
      ? alertBox('warn', 'Bu Short ALTYAZISIZ uretildi',
                 tech.subtitle_warning
                 || 'Secilen momentte konusma tespit edilmedi.')
      : null,
    tech.excluded_subtitle_cues
      ? alertBox('info', 'Sarki / muzik altyazisi gizlendi',
                 `${tech.excluded_subtitle_cues} satir insan karariyla renderdan cikarildi.`,
                 'Metin transkript gecmisinde korunuyor ve Altyazi sekmesinden geri acilabilir.')
      : null,
    el('div', { class: 'tech-grid', 'data-testid': 'render-technical' },
      techCell('Sure', tech.duration_sec ? `${Number(tech.duration_sec).toFixed(2)} sn` : null),
      techCell('Cozunurluk', tech.resolution + (tech.vertical ? ' (9:16)' : ' (DIKEY DEGIL)')),
      techCell('Video codec', tech.video_codec),
      techCell('Ses', tech.audio_present
        ? (tech.original_audio_preserved ? 'orijinal ses korundu' : 'ses var')
        : 'SES YOK'),
      techCell('TTS', tech.tts_used ? 'kullanildi' : 'kullanilmadi'),
      techCell('Altyazi modu', tech.subtitle_source),
      techCell('Altyazi', `${tech.subtitle_cues ?? '-'} cue / ${tech.subtitle_words ?? '-'} kelime`),
      techCell('Aktif kelime', tech.dynamic_word_highlight ? 'dinamik vurgu acik' : 'duz altyazi'),
      techCell('Caption tema/font', `${tech.caption_theme || '-'} / ${tech.caption_font || '-'}`),
      techCell('Render modu', tech.render_mode),
      techCell('Kadraj zoom', tech.frame_zoom ? `${Number(tech.frame_zoom).toFixed(2)}x` : '1.00x'),
      techCell('Kadraj odagi', tech.frame_focus_x != null && tech.frame_focus_y != null
        ? `${Number(tech.frame_focus_x).toFixed(2)}, ${Number(tech.frame_focus_y).toFixed(2)}` : '-'),
      techCell('Kalite', tech.preview_mode ? 'preview' : 'uretim'),
      techCell('artifact_status', tech.status)),
    el('div', { class: 'dim', style: 'margin-top:12px',
                text: `Video SHA-256 (kisa): ${tech.video_sha256_16 || '-'} · `
                      + `yuklenmedi: ${tech.not_uploaded}` }));
}

/* Render review = the shared component, pinned to its kind. Kept as named
 * functions so the render screen reads the same as before. */
function renderReviewChecklist() {
  return reviewChecklist('render');
}

function renderRevisionPanel() {
  // The note panel records WHY; the studio below acts on it by moving the
  // invalidated stage out of the way. Kept adjacent so the two are
  // obviously one workflow rather than two competing buttons.
  return el('div', {}, reviewRevisionPanel('render'), revisionStudioPanel());
}

/* Revizyon istasyonu.
 *
 * Requesting a revision used to be a NOTE: it changed nothing, so the rejected
 * clip, transcript and video stayed exactly where the next run would pick them
 * up again. Starting a revision here MOVES the invalidated stage into the
 * archive and hands the project back for a rerun.
 *
 * Two rules:
 *   1. Nothing is deleted. The old attempt goes to `revisions/<id>/` and can be
 *      recovered; permanent deletion is a separate, explicitly confirmed action.
 *   2. The confirmation names the files. "Emin misin?" that cannot say what it
 *      is about to move is not consent. */
function revisionStudioPanel() {
  const r = state.revision;
  if (!r) return null;
  const suggested = r.suggested || {};
  const chosen = state.revisionStage || suggested.stage;
  const plan = r.plan || {};
  const planMatches = plan.target === chosen;
  return el('div', { class: 'card', 'data-testid': 'revision-studio' },
    el('div', { class: 'card__head' },
      el('h2', { text: 'Revizyon: yeniden uretime gec' }),
      badge(plan.nothing_to_move ? 'missing' : 'review_required',
            plan.nothing_to_move ? 'tasinacak cikti yok'
                                 : `${plan.file_count || 0} dosya`)),
    el('p', { class: 'dim',
      text: 'Revizyon baslatildiginda gecersizlesen asama ve sonrasi arsive '
            + 'TASINIR (silinmez), boylece sonraki koşu eski ciktiyi yeniden '
            + 'kullanamaz.' }),
    (suggested.reasons || []).length
      ? el('div', { class: 'stack', 'data-testid': 'revision-suggestion' },
          el('div', { class: 'dim', text: 'Isaretlenmemis inceleme maddelerinden:' }),
          suggested.reasons.map(reason => el('div', { class: 'dim mono', text: reason })),
          el('div', { class: 'dim', text: suggested.note || '' }))
      : el('div', { class: 'dim', 'data-testid': 'revision-suggestion',
                    text: suggested.note || '' }),
    field({ id: 'revision-stage', label: 'Nereden yeniden baslansin',
      hint: 'Sistem oneriyor, kararı sen veriyorsun. Daha erken bir asama daha '
            + 'cok ciktiyi arsive tasir.',
      control: el('select', {
        id: 'revision-stage', 'data-testid': 'revision-stage',
        onchange: async (event) => {
          state.revisionStage = event.currentTarget.value;
          await refreshRevision();
          render();
        },
      }, (r.stages || []).map(stage => el('option', {
        value: stage.key, selected: stage.key === chosen,
      }, stage.label))) }),
    textareaField({ id: 'revision-why', label: 'Neden (kayda gecer)',
      hint: 'Serbest metin yorumlanmaz; hangi asamanin yeniden calisacagini '
            + 'yukaridaki secim belirler.',
      attrs: { rows: 2, 'data-testid': 'revision-why',
               oninput: (event) => { state.revisionNotes = event.currentTarget.value; } } }),
    plan.suggested_render_adjustment
      ? alertBox('info', 'Kadraj komutu algilandi',
          `${Number(plan.suggested_render_adjustment.frame_zoom).toFixed(2)}x zoom; `
          + `odak ${Number(plan.suggested_render_adjustment.frame_focus_x).toFixed(2)}, `
          + `${Number(plan.suggested_render_adjustment.frame_focus_y).toFixed(2)}.`,
          'Revizyonu baslatinca bu ayar yeni rendera uygulanacak ve manifestte kaydedilecek.')
      : (chosen === 'render'
          ? alertBox('warn', 'Notta uygulanabilir kadraj komutu yok',
              'Otomatik yorumlanan komutlar: biraz yakinlas, yakinlastir, uzaklastir, '
              + 'sola/saga/yukari/asagi odakla.',
              'Belirsiz bir not videoyu sessizce degistirmez.')
          : null),
    // The list is the consent: it says exactly what leaves.
    planMatches && (plan.moves || []).length
      ? el('div', { class: 'stack', 'data-testid': 'revision-moves' },
          el('h3', { text: 'Arsive tasinacaklar' }),
          plan.moves.map(move => el('div', { class: 'spread',
                                             'data-testid': `revision-move-${move.path}` },
            el('span', { class: 'mono', text: move.path }),
            el('span', { class: 'dim', text: move.kind === 'dir'
              ? `${move.file_count} dosya` : 'dosya' }),
            el('span', { class: 'dim',
                         text: `${(move.size_bytes / 1024).toFixed(0)} KB` }))),
          el('div', { class: 'dim', text: `Hedef: ${plan.destination} (geri alinabilir)` }))
      : el('div', { class: 'empty', 'data-testid': 'revision-moves-empty',
                    text: plan.nothing_to_move
                      ? 'Bu asamada tasinacak cikti yok.'
                      : 'Asamayi sec, tasinacak dosyalar burada listelenecek.' }),
    el('button', {
      class: 'btn btn--danger', 'data-testid': 'revision-start',
      disabled: Boolean(plan.nothing_to_move) || !planMatches,
      onclick: (event) => withButton(event, async () => {
        const result = await guard(() => api.post(
          `/api/projects/${state.projectId}/revision`,
          { target: chosen, notes: state.revisionNotes || '' }),
          'Revizyon baslatildi; eski ciktilar arsive tasindi');
        if (result) {
          state.revisionNotes = '';
          state.revision = null;
        }
        await refresh();
      }),
    }, 'Revizyonu baslat'),
    revisionArchivePanel(r.archive || {}));
}

function revisionArchivePanel(archive) {
  const items = archive.revisions || [];
  return el('div', { class: 'stack', 'data-testid': 'revision-archive',
                     style: 'margin-top:14px' },
    el('h3', { text: `Arsiv (${archive.count || 0})` }),
    el('p', { class: 'dim', text: archive.hint || '' }),
    items.length === 0
      ? el('div', { class: 'empty', 'data-testid': 'revision-archive-empty',
                    text: 'Henuz arsivlenmis bir deneme yok.' })
      : el('div', { class: 'stack' }, items.map(item => el('div', {
          class: 'moment-card', 'data-testid': `archive-${item.revision_id}` },
        el('div', { class: 'spread' },
          el('span', { class: 'mono', text: item.revision_id }),
          el('span', { class: 'dim',
                       text: `${item.action}${item.target ? ' / ' + item.target : ''}` }),
          el('span', { class: 'dim',
                       text: String(item.created_at || '').slice(0, 19) })),
        el('div', { class: 'dim mono',
                    text: (item.moved_paths || []).join(', ') || '-' }),
        el('div', { class: 'row' },
          inputField({ id: `purge-${item.revision_id}`,
                       label: 'Kalici silmek icin kimligi yaz',
                       attrs: { placeholder: item.revision_id,
                                'data-testid': `purge-input-${item.revision_id}` } }),
          el('button', {
            class: 'btn btn--sm btn--danger',
            'data-testid': `purge-${item.revision_id}`,
            onclick: (event) => withButton(event, async () => {
              const node = document.getElementById(`purge-${item.revision_id}`);
              await guard(() => api.post(
                `/api/projects/${state.projectId}/revision/purge`,
                { revision_id: item.revision_id,
                  confirmation: node ? node.value : '' }),
                'Arsivlenmis revizyon kalici olarak silindi');
              state.revision = null;
              await refresh();
            }),
          }, 'Kalici sil'))))));
}

async function refreshRevision() {
  if (!state.projectId) return;
  const stage = state.revisionStage ? `?target=${encodeURIComponent(state.revisionStage)}` : '';
  const result = await api.get(
    `/api/projects/${state.projectId}/revision${stage}`).catch(() => null);
  state.revision = result ? result.revision : null;
}

function screenRender() {
  const decision = ((state.project.phase0 || {}).plan || {}).decision || {};
  const config = decision.render || {};
  const review = state.project.render_review || {};
  const tech = review.technical || {};
  const outputs = state.project.outputs || [];
  const primary = outputs[0] || {};
  const playable = primary.video && primary.video.usable;
  // Render settings are NOT editable here on purpose: they are part of the
  // approved plan's decision hash. Changing them must go through Phase 0.
  const params = {
    render_mode: config.render_mode || 'blurred_background',
    preview_mode: config.preview_mode !== undefined ? config.preview_mode : true,
    subtitle_whisper_model_size: config.subtitle_whisper_model_size || 'small',
    subtitle_language: config.subtitle_language || 'auto',
    caption_style: config.caption_style || 'active_word_yellow',
    caption_theme: config.caption_theme || 'auto',
  };
  const approval = (state.project.approvals || {}).render || {};

  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Render Studio' }),
      el('p', { class: 'subtitle',
                text: 'Videoyu izle, teknik ozeti oku, kontrol listesini doldur; '
                      + 'final onay ancak ondan sonra acilir.' }))),

    playable
      ? el('div', { class: 'card' },
          el('h2', { text: 'Uretilen Short' }),
          el('div', { class: 'review-player' },
            el('video', {
              controls: true, preload: 'metadata', playsinline: true,
              'data-testid': 'render-player',
              src: `/api/projects/${state.projectId}/media/${primary.video.relative_path}`,
            }),
            el('div', { class: 'review-player__bar' },
              el('span', { text: `${(primary.video.size_bytes / 1048576).toFixed(2)} MB` }),
              el('span', { text: tech.resolution || '' }),
              el('span', { text: tech.duration_sec
                                  ? `${Number(tech.duration_sec).toFixed(2)} sn` : '' }),
              badge(primary.status || 'missing'))))
      : el('div', { class: 'card' },
          el('h2', { text: 'Uretilen Short' }),
          primary.problem
            ? alertBox('danger', 'Video oynatilamaz', primary.problem,
                       'Renderi yeniden calistir.')
            : el('div', { class: 'empty', 'data-testid': 'render-empty',
                          text: 'Henuz render edilmis bir video yok.' })),

    renderTechnicalSummary(tech),

    el('div', { class: 'card' },
      el('h3', { text: 'Onaylanan render ayarlari' }),
      el('div', { class: 'mono dim', 'data-testid': 'render-config',
                  text: JSON.stringify(params) }),
      el('div', { class: 'field__hint',
                  text: 'Degistirmek icin Faz 0 planini yenile ve yeniden onayla.' })),

    phaseRunCard('render', 'Render calistir',
      'Plandaki render ayarlari kullanilir.', params),

    playable ? renderReviewChecklist() : null,
    playable ? renderRevisionPanel() : null,

    reviewApprovalSection('render', 'Final onay', 'Renderi onayla', 'render'));
}


const BATCH_STATE_LABELS = {
  needs_moment: 'Moment bekliyor',
  ready_to_render: 'Render edilebilir',
  needs_review: 'Izlenmeyi bekliyor',
  needs_revision: 'Revizyon acik',
  approved: 'Onaylandi',
};

//: The batch board. Every state, action and blocker shown here is decided by
//: `PipelineService.batch_state()` on the server -- the browser picks layout and
//: colour only. Re-deriving "can this slot render?" in JS is how a board starts
//: offering buttons the pipeline refuses.
function batchBoard() {
  const batch = state.project.batch;
  if (!batch || !batch.slots || batch.slots.length <= 1) return null;
  const runnable = batch.runnable_slots || [];
  return el('div', { class: 'card', 'data-testid': 'batch-board' },
    el('div', { class: 'spread' },
      el('h2', { text: `Batch: ${batch.output_count} Short` }),
      el('span', { class: 'dim',
        text: `${batch.approved_count}/${batch.output_count} onayli - `
              + `${batch.rendered_count}/${batch.output_count} render edildi` })),
    batch.render_blocker
      ? alertBox('warn', 'Render fazi kapali', batch.render_blocker) : null,
    el('div', { class: 'spread' },
      el('p', { class: 'dim', text: runnable.length
        ? `${runnable.length} yuva makine isine hazir (moment secilmis, render bekliyor).`
        : 'Makine isine hazir yuva yok; sirada bir insan karari var.' }),
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'batch-run',
        disabled: runnable.length === 0,
        text: runnable.length ? `${runnable.length} render'i kuyruga al` : 'Kuyruga alinacak yuva yok',
        onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          const result = await guard(() => api.post(
            `/api/projects/${state.projectId}/batch`,
            { run_request_id: crypto.randomUUID() }));
          if (result) {
            pushAlert('info', `${result.submitted_count} render kuyruga alindi`,
              result.skipped_count
                ? `${result.skipped_count} yuva atlandi; her biri once bir insan karari bekliyor.`
                : 'Her yuva kendi dizinine render edilir ve ayri ayri onaylanir.');
          }
          await refresh();
        } })),
    el('div', { class: 'stack' }, batch.slots.map(slot => el('div', {
        class: 'moment-card', 'data-testid': `batch-slot-${slot.index}` },
      el('div', { class: 'spread' },
        el('strong', { text: `Short ${slot.index}` }),
        badge(slot.state, BATCH_STATE_LABELS[slot.state] || slot.state)),
      el('div', { class: 'dim', text: slot.moment_selected
        ? `Moment rank ${slot.moment_rank}` : 'Moment secilmedi' }),
      slot.review_total
        ? el('div', { class: 'dim',
            text: `Inceleme ${slot.review_checked || 0}/${slot.review_total}` })
        : null,
      slot.problem ? el('div', { class: 'dim', text: slot.problem }) : null,
      slot.video && slot.video.usable
        ? el('video', { controls: true, preload: 'none',
            src: `/api/projects/${state.projectId}/media/${slot.video.relative_path}` })
        : null,
      slot.next_action
        ? el('button', { class: 'btn', 'data-testid': `batch-slot-${slot.index}-action`,
            text: slot.next_action,
            onclick: () => goto(slot.state === 'needs_moment' ? 'moments' : 'render') })
        : null))));
}

function screenOutputs() {
  const outputs = state.project.outputs || [];
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Ciktilar ve gecmis' }),
      el('p', { class: 'subtitle', text: 'Hicbir cikti otomatik yuklenmez veya yayinlanmaz.' }))),
    batchBoard(),
    el('div', { class: 'card' },
      outputs.length === 0 ? el('div', { class: 'empty', text: 'Henuz cikti yok.' })
        : el('div', { class: 'stack' }, outputs.map(output => el('div', { class: 'moment-card' },
            el('div', { class: 'spread' },
              el('strong', { text: `Cikti #${output.index}` }),
              badge(output.status || 'missing')),
            output.problem ? alertBox('danger', 'Bu cikti kullanilamaz', output.problem) : null,
            output.video && output.video.usable
              ? el('div', {},
                  el('video', { controls: true, preload: 'none',
                    src: `/api/projects/${state.projectId}/media/${output.video.relative_path}` }),
                  el('div', { class: 'dim', text: `${(output.video.size_bytes / 1048576).toFixed(2)} MB` }))
              : null,
            el('div', { class: 'dim mono', text: output.relative_path || '' }))))),
    el('div', { class: 'card' },
      el('h2', { text: 'Audit gecmisi' }),
      el('div', { class: 'table-scroll' }, el('table', {},
        el('thead', {}, el('tr', {}, el('th', { text: 'Olay' }), el('th', { text: 'Aktor' }),
          el('th', { text: 'Zaman' }))),
        el('tbody', {}, (state.project.audit || []).slice(0, 25).map(event => el('tr', {},
          el('td', { text: event.event_type }),
          el('td', { class: 'dim', text: event.actor || '-' }),
          el('td', { class: 'dim', text: event.recorded_at || '-' }))))))));
}

function screenSettings() {
  const source = state.workspace || {};
  const secrets = (state.project && state.project.secrets) || source.secrets || [];
  const planned = source.planned_not_implemented || [];
  const secretRows = secrets.map(secret => el('tr', {},
    el('td', {},
      el('strong', { text: secret.kind }),
      el('div', { class: 'dim', text: (secret.aliases_supported || []).join(' → ') })),
    el('td', {}, badge(secret.state === 'configured' ? 'completed' : 'missing', secret.state)),
    el('td', { class: 'dim', text: secret.purpose || '' })));
  const plannedRows = planned.map(item => el('div', { class: 'row' },
    badge('planned', 'planlandi'),
    el('span', { text: item.label }),
    el('span', { class: 'dim', text: item.milestone })));

  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Ayarlar' }),
      el('p', { class: 'subtitle',
                text: 'Kaydedilmis secret degerleri sunucudan tarayiciya geri gonderilmez.' }))),
    preflightPanel(),
    diagnosticsPanel(),
    phaseProgress('diagnostics'),
    el('div', { class: 'card' },
      el('h2', { text: 'Kimlik bilgileri' }),
      el('div', { class: 'table-scroll' }, el('table', {},
        el('thead', {}, el('tr', {}, el('th', { text: 'Anahtar' }), el('th', { text: 'Durum' }),
          el('th', { text: 'Kullanim' }))),
        el('tbody', {}, secretRows)))),
    el('div', { class: 'card secret-entry', 'data-testid': 'github-secret-entry' },
      el('h2', { text: 'GitHub token kaydet' }),
      el('p', { class: 'subtitle',
                text: 'Token yalniz bu bilgisayardaki .env.local dosyasina yazilir. '
                      + 'API yanitinda, logda veya proje artefaktinda geri dondurulmez.' }),
      alertBox('info', 'Otomatik veri senkronu henuz kapali',
        'Bu alan yalniz kimlik bilgisini hazirlar. .console_data icin guvenli '
        + 'baslangic/kapanis senkronizasyonu ayri bir adimda etkinlestirilecek.'),
      requiredLegend(),
      inputField({ id: 'github-token', label: 'GitHub Personal Access Token',
                   type: 'password', required: true,
                   hint: 'Private repo erisimi icin gerekli en dar izinleri kullan. '
                         + 'Kaydettikten sonra alan temizlenir.',
                   attrs: { autocomplete: 'new-password', minlength: 20,
                            maxlength: 8192, placeholder: 'github_pat_... veya ghp_...',
                            'data-testid': 'github-token' } }),
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'github-token-save',
        onclick: async (event) => {
          const values = validateFields([
            { id: 'github-token', label: 'GitHub token', required: true, minLength: 20 },
          ]);
          if (!values) return;
          const input = document.getElementById('github-token');
          event.currentTarget.disabled = true;
          const saved = await guard(() => api.post('/api/settings/secrets', {
            kind: 'github', secret_value: values['github-token'],
          }), 'GitHub token yerel kasaya kaydedildi');
          // Whether save succeeds or fails, do not leave a credential sitting
          // in the DOM where another person at the machine could reveal it.
          if (input) input.value = '';
          if (saved) await refresh();
          else event.currentTarget.disabled = false;
        },
      }, 'Tokeni yerel kasaya kaydet')),
    el('div', { class: 'card' },
      el('h2', { text: 'Sahiplik' }),
      el('div', { class: 'mono dim', text: JSON.stringify(source.owner || {}, null, 2) })),
    el('div', { class: 'card' },
      el('h2', { text: 'Henuz uygulanmadi (planlandi)' }),
      el('div', { class: 'stack' }, plannedRows)));
}

/* ----------------------------------------------------------- Turbo Studio */
/* Fourteen stages layered over the existing script, TTS, stock-media, timeline
   and render services. Every control a phase has not yet wired is stated as
   not-yet-connected rather than rendered as a button that does nothing -- a
   dead control is worse than an absent one, because it invites a click and
   teaches the operator not to trust the screen. */

const TURBO_STAGE_BADGE = {
  completed: 'completed', failed: 'failed', running: 'running',
  queued: 'queued', not_started: 'planned', review_required: 'review_required',
  waiting_rate_limit: 'queued', waiting_daily_quota: 'queued',
  blocked_billing: 'failed', cancelled: 'failed',
};

function turboDraft() {
  if (state.turboDraft) return state.turboDraft;
  const studio = state.turboStudio || {};
  const base = studio.brief || studio.brief_defaults || {};
  state.turboDraft = {
    topic: base.topic || '',
    content_type: base.content_type || 'explainer',
    duration_sec: base.duration_sec || 60,
    aspect_ratio: base.aspect_ratio || '9:16',
    fps: base.fps || 30,
    clip_duration_sec: base.clip_duration_sec || 5,
    fit_mode: base.fit_mode || 'crop',
    transition: base.transition || 'none',
    transition_sec: base.transition_sec || 0.4,
    ordering: base.ordering || 'sequential',
    seed_input: base.seed_input || '',
    notes: base.notes || '',
  };
  return state.turboDraft;
}

function setTurboDraft(key, value) {
  turboDraft()[key] = value;
}

function turboSelect(id, label, key, options, hint = '') {
  const draft = turboDraft();
  return field({ id, label, required: true, hint,
    control: el('select', { id, 'data-testid': id, 'aria-required': 'true',
      onchange: event => setTurboDraft(key, event.currentTarget.value) },
    options.map(([value, text]) => el('option', {
      value, selected: String(draft[key]) === String(value),
    }, text))) });
}

function turboBriefCard() {
  const studio = state.turboStudio;
  const draft = turboDraft();
  const limits = studio.limits || {};
  const options = studio.options || {};
  const update = key => event => setTurboDraft(key, event.currentTarget.value);

  const save = el('button', {
    class: 'btn btn--primary', type: 'button', 'data-testid': 'turbo-save-brief',
    onclick: async () => {
      const result = await guard(() => api.post(
        `/api/projects/${state.projectId}/turbo/brief`, {
          ...draft,
          duration_sec: Number(draft.duration_sec),
          clip_duration_sec: Number(draft.clip_duration_sec),
          fps: Number(draft.fps),
          transition_sec: Number(draft.transition_sec),
        }), 'Brief kaydedildi');
      if (result) {
        state.turboStudio = result.turbo_studio;
        state.turboDraft = null;
        if (result.saved) {
          pushAlert('warning', 'Brief degisti',
            'Ciktiyi etkileyen bir alan degisti; script ve sonrasindaki '
            + 'asamalar sifirlandi. Onceki ciktilar silinmedi.');
        }
        render();
      }
    },
  }, 'Brief kaydet');

  return el('div', { class: 'card' },
    el('h2', { text: 'Brief' }),
    requiredLegend(),
    inputField({ id: 'turbo-topic', label: 'Konu', required: true,
      hint: 'En az 8 karakter. Ornek: Roma imparatorlugunun cokus nedenleri.',
      attrs: { value: draft.topic, maxlength: 200, 'data-testid': 'turbo-topic',
        oninput: update('topic') } }),
    el('div', { class: 'grid grid--2' },
      turboSelect('turbo-content-type', 'Icerik turu', 'content_type',
        (options.content_types || []).map(value => [value, value])),
      turboSelect('turbo-aspect', 'En-boy orani', 'aspect_ratio',
        (options.aspect_ratios || []).map(item => [item.value, item.label]))),
    el('div', { class: 'grid grid--3' },
      inputField({ id: 'turbo-duration', label: 'Sure (saniye)', required: true,
        type: 'number',
        hint: `${limits.min_duration_sec || 15}-${limits.max_duration_sec || 1800} sn`,
        attrs: { value: draft.duration_sec, min: limits.min_duration_sec,
          max: limits.max_duration_sec, 'data-testid': 'turbo-duration',
          oninput: update('duration_sec') } }),
      inputField({ id: 'turbo-clip-duration', label: 'Klip suresi (sn)',
        required: true, type: 'number',
        hint: `en fazla ${limits.max_clips || 240} klip`,
        attrs: { value: draft.clip_duration_sec, min: limits.min_clip_sec,
          max: limits.max_clip_sec, step: '0.5',
          'data-testid': 'turbo-clip-duration',
          oninput: update('clip_duration_sec') } }),
      turboSelect('turbo-fps', 'fps', 'fps',
        (options.fps_choices || [30]).map(value => [value, String(value)]))),
    el('div', { class: 'grid grid--3' },
      turboSelect('turbo-fit-mode', 'Kadraj', 'fit_mode', [
        ['crop', 'Kirp (kenarlar kesilir)'],
        ['fit', 'Sigdir (bulanik arka plan)'],
      ]),
      turboSelect('turbo-transition', 'Gecis', 'transition', [
        ['none', 'Gecis yok'], ['fade', 'Karartmali gecis'],
      ], 'Karartmali gecis toplam sureyi degistirmez.'),
      turboSelect('turbo-ordering', 'Siralama', 'ordering', [
        ['sequential', 'Script sirasi'], ['seeded_random', 'Seed ile rastgele'],
      ]),
      turboSelect('turbo-caption-theme', 'Altyazi temasi', 'caption_theme',
        (options.caption_themes || ['default']).map(value => [value, value]),
        'Aktif kelime vurgusunun font ve rengini belirler.')),
    inputField({ id: 'turbo-seed', label: 'Seed (istege bagli)',
      hint: 'Ayni seed ayni klip sirasini uretir; bos birakilirsa script sirasi kullanilir.',
      attrs: { value: draft.seed_input, maxlength: 80,
        'data-testid': 'turbo-seed', oninput: update('seed_input') } }),
    el('div', { class: 'actions' }, save));
}

function turboEstimateCard() {
  const estimates = ((state.turboStudio.brief || {}).estimates) || null;
  if (!estimates) return null;
  return el('div', { class: 'card', 'data-testid': 'turbo-estimates' },
    el('h2', { text: 'Harcama oncesi tahmin' }),
    el('div', { class: 'grid grid--3' },
      techCell('Tahmini kelime', estimates.narration_words),
      techCell('Klip sayisi', estimates.clip_count),
      techCell('TTS parcasi', estimates.tts_chunks_estimate)),
    alertBox('info', 'Bunlar tahmindir', estimates.note));
}

/* One place that posts to a Turbo route and folds the fresh view back in, so
   every control behaves identically and none of them can forget to refresh. */
async function turboPost(path, body, successTitle) {
  const result = await guard(
    () => api.post(`/api/projects/${state.projectId}/turbo${path}`, body || {}),
    successTitle);
  if (result && result.turbo_studio) {
    state.turboStudio = result.turbo_studio;
    render();
  }
  return result;
}

function turboButton(label, testid, handler, { primary = false,
                                               disabled = false,
                                               reason = '' } = {}) {
  // A control that cannot be used is shown DISABLED WITH ITS REASON rather than
  // hidden: hiding it makes the workflow look broken, and enabling it makes the
  // click look broken.
  const button = el('button', {
    class: `btn${primary ? ' btn--primary' : ''}`, type: 'button',
    'data-testid': testid, disabled: disabled || undefined,
    title: disabled ? reason : '',
    onclick: disabled ? undefined : handler,
  }, label);
  if (!disabled) return button;
  return el('span', { class: 'row' }, button,
    el('span', { class: 'muted', text: reason }));
}

function turboStagesCard() {
  const studio = state.turboStudio;
  const progress = studio.progress || {};
  const next = progress.next || {};
  const stages = progress.stages || [];
  const completed = new Set(stages.filter(s => s.state === 'completed')
    .map(s => s.key));

  const rows = stages.map((stage, index) => {
    const earlierDone = stages.slice(0, index)
      .every(s => s.state === 'completed');
    const controls = el('div', { class: 'row' });
    if (!stage.automatic && stage.state !== 'completed'
        && stage.key !== 'final_render' && stage.key !== 'archive') {
      controls.appendChild(turboButton(
        'Onayla', `turbo-approve-${stage.key}`,
        () => turboPost('/approve', { stage: stage.key },
          `${stage.label} onaylandi`),
        { disabled: !earlierDone,
          reason: 'onceki asamalar tamamlanmadi' }));
    }
    controls.appendChild(badge(TURBO_STAGE_BADGE[stage.state] || 'missing',
      stage.state));
    return el('div', { class: 'row', 'data-testid': `turbo-stage-${stage.key}` },
      el('div', {},
        el('strong', { text: `${index + 1}. ${stage.label}` }),
        el('div', { class: 'muted',
          text: stage.automatic ? 'otomatik calisir' : 'insan karari gerekir' }),
        stage.detail ? el('div', { class: 'muted', text: stage.detail }) : null,
        stage.error ? el('div', { class: 'muted', text: stage.error }) : null,
        stage.resume_at
          ? el('div', { class: 'muted',
            text: `kota beklemesi, devam: ${stage.resume_at}` })
          : null),
      controls);
  });

  const runnable = !next.blocked && next.stage;
  const actions = el('div', { class: 'actions' },
    turboButton('Siradaki asamayi calistir', 'turbo-run-stage',
      () => turboPost('/stage', {}, 'Asama calistirildi'),
      { primary: true, disabled: !runnable,
        reason: next.reason || 'calistirilabilir asama yok' }));

  return el('div', { class: 'card', 'data-testid': 'turbo-stages' },
    el('h2', { text: `Asamalar (${progress.completed_count || 0}/${progress.total_count || 14})` }),
    el('div', { class: 'stack' }, rows),
    next.reason
      ? alertBox(next.blocked ? 'warning' : 'info', 'Siradaki', next.reason)
      : null,
    actions);
}

function turboScriptCard() {
  const script = state.turboStudio.script;
  if (!script) return null;
  return el('div', { class: 'card', 'data-testid': 'turbo-script' },
    el('h2', { text: 'Script ve sahne plani' }),
    el('div', { class: 'grid grid--3' },
      techCell('Kelime', script.word_count),
      techCell('Sahne', script.beat_count),
      techCell('Model', script.model)),
    script.beat_coverage !== undefined && script.beat_coverage < 0.9
      ? alertBox('warning', 'Sahne-anlatim uyumu', script.coverage_note)
      : null,
    el('div', { class: 'stack' },
      (script.beats || []).map(beat => el('div', { class: 'row' },
        el('div', {},
          el('strong', { text: `${beat.index}. ${beat.search_query}` }),
          el('div', { class: 'muted', text: beat.text }))))));
}

function turboNarrationCard() {
  const narration = state.turboStudio.narration;
  if (!narration) return null;
  const summary = narration.summary || {};
  const combined = narration.combined;
  return el('div', { class: 'card', 'data-testid': 'turbo-narration' },
    el('h2', { text: 'Anlatim' }),
    el('div', { class: 'grid grid--3' },
      techCell('Parca', `${summary.completed_count || 0}/${summary.chunk_count || 0}`),
      techCell('Birlesik sure', combined ? `${combined.duration_sec} sn` : '-'),
      techCell('Brief hedefinden sapma',
        combined && combined.drift_sec !== null && combined.drift_sec !== undefined
          ? `${combined.drift_sec} sn` : '-')),
    el('div', { class: 'stack' },
      (narration.chunks || []).map(chunk => el('div', { class: 'row' },
        el('span', { text: `${chunk.sequence}. parca (${chunk.word_count || 0} kelime)` }),
        el('div', { class: 'row' },
          chunk.resume_at
            ? el('span', { class: 'muted', text: `devam: ${chunk.resume_at}` })
            : null,
          badge(chunk.status === 'completed' ? 'completed'
            : (chunk.status === 'failed' ? 'failed' : 'queued'),
          chunk.status))))));
}

function turboMediaCard() {
  const media = state.turboStudio.media;
  if (!media) {
    return el('div', { class: 'card', 'data-testid': 'turbo-media' },
      el('h2', { text: 'Stok materyal' }),
      alertBox('info', 'Slotlar henuz olusturulmadi',
        'Script uretildikten sonra "Siradaki asamayi calistir" klip slotlarini olusturur. '
        + 'Slot olusturmak hicbir saglayici istegi yapmaz.'));
  }
  const readiness = media.readiness || {};
  const rows = (media.slots || []).map(slot => {
    const controls = el('div', { class: 'row' });
    media.providers.forEach(provider => {
      controls.appendChild(turboButton(
        `${provider} ara`, `turbo-search-${slot.id}-${provider}`,
        () => turboPost('/media',
          { action: 'search', slot_id: slot.id, provider },
          `${provider} aramasi tamamlandi`),
        { disabled: readiness.stale,
          reason: 'script degisti; once slotlari yenileyin' }));
    });
    if (slot.selected_id && !slot.downloaded) {
      controls.appendChild(turboButton(
        'Indir', `turbo-download-${slot.id}`,
        () => turboPost('/media', { action: 'download', slot_id: slot.id },
          'Klip indirildi'),
        { primary: true, disabled: readiness.stale,
          reason: 'script degisti; once slotlari yenileyin' }));
    }

    const candidates = el('div', { class: 'stack' },
      (slot.candidates || []).map(candidate => el('div', { class: 'row' },
        el('div', {},
          el('span', { text: `${candidate.provider} · ${candidate.width}x${candidate.height} · ${candidate.duration_sec}sn` }),
          el('div', { class: 'muted', text: candidate.attribution || '' }),
          candidate.source_page
            ? el('a', { href: candidate.source_page, target: '_blank',
              rel: 'noopener noreferrer', text: 'kaynak sayfasi' })
            : null),
        candidate.id === slot.selected_id
          ? badge('completed', 'secili')
          : turboButton('Sec', `turbo-select-${slot.id}-${candidate.id}`,
            () => turboPost('/media',
              { action: 'select', slot_id: slot.id, candidate_id: candidate.id },
              'Aday secildi'),
            { disabled: readiness.stale,
              reason: 'script degisti; once slotlari yenileyin' }))));

    return el('div', { class: 'stack', 'data-testid': `turbo-slot-${slot.id}` },
      el('div', { class: 'row' },
        el('div', {},
          el('strong', { text: `${slot.sequence}. ${slot.query}` }),
          el('div', { class: 'muted', text: slot.excerpt || '' })),
        el('div', { class: 'row' },
          badge(slot.downloaded ? 'completed'
            : (slot.selected_id ? 'review_required' : 'planned'),
          slot.downloaded ? 'indirildi'
            : (slot.selected_id ? 'secildi' : 'bekliyor')),
          controls)),
      candidates);
  });

  return el('div', { class: 'card', 'data-testid': 'turbo-media' },
    el('h2', { text: `Stok materyal (${readiness.downloaded_count || 0}/${readiness.slot_count || 0} indirildi)` }),
    alertBox('info', 'Arama, secim ve indirme ayri eylemlerdir',
      media.policy || ''),
    readiness.stale
      ? alertBox('warning', 'Plan bayat',
        'Script degisti. Arama, secim ve indirme, slot listesi yenilenene kadar kilitli.')
      : null,
    el('div', { class: 'row' },
      turboButton('Slotlari yenile', 'turbo-media-plan',
        () => turboPost('/media', { action: 'plan' }, 'Slotlar yenilendi'),
        { reason: '' })),
    el('div', { class: 'stack' }, rows));
}

function turboMusicCard() {
  const music = state.turboStudio.music || {};
  const chosen = music.chosen;
  const library = music.library || { tracks: [] };
  const tracks = [...(library.tracks || []), ...(music.uploaded || [])];

  const rows = tracks.map(track => el('div', {
    class: 'row', 'data-testid': `turbo-music-${track.filename}`,
  },
  el('div', {},
    el('strong', { text: track.display_name }),
    el('div', { class: 'muted', text: track.license.label })),
  el('div', { class: 'row' },
    badge(track.license.status === 'verified' ? 'completed' : 'review_required',
      track.license.status),
    turboButton('Sec', `turbo-music-pick-${track.filename}`,
      () => turboPost('/music',
        { origin: track.origin, filename: track.filename },
        'Muzik secildi')))));

  return el('div', { class: 'card', 'data-testid': 'turbo-music' },
    el('h2', { text: 'Muzik' }),
    alertBox('warning', 'Telif iddiasi yapilmaz',
      music.policy
      || 'Bu proje hicbir parcayi telifsiz olarak nitelemez. Lisans kaydi eksik '
         + 'olan parcalar isaretlenir; kullanmadan once siz dogrulayin.'),
    chosen
      ? el('div', { class: 'row' },
        el('span', { text: chosen.present
          ? `Secili: ${chosen.display_name}` : 'Muzik yok (acik karar)' }),
        chosen.present
          ? badge(chosen.license && chosen.license.status === 'verified'
            ? 'completed' : 'review_required',
          (chosen.license || {}).status || 'bilinmiyor')
          : null)
      : null,
    !library.configured
      ? alertBox('info', 'Kutuphane tanimli degil',
        'CREATOR_CONSOLE_MUSIC_ROOT ayarlayin veya muziksiz devam edin.')
      : null,
    el('div', { class: 'stack' }, rows),
    el('div', { class: 'actions' },
      turboButton('Muziksiz devam et', 'turbo-music-none',
        () => turboPost('/music', { origin: 'none' }, 'Muziksiz devam')),
      (() => {
        // The second music source: a file the operator owns. Streamed with the
        // filename in a header, like the source-video upload, so the extension
        // is checked before a byte is written.
        const picker = el('input', {
          type: 'file', accept: '.mp3,.wav,.m4a,.aac,.ogg,.flac',
          'data-testid': 'turbo-music-upload',
          onchange: async (event) => {
            const file = event.currentTarget.files[0];
            if (!file) return;
            await guard(async () => {
              const response = await fetch(
                `/api/projects/${state.projectId}/turbo/music/upload`,
                { method: 'POST', body: file,
                  headers: { 'X-Filename': encodeURIComponent(file.name) } });
              const payload = await response.json();
              if (!response.ok) throw new Error((payload.error || {}).message
                || 'Muzik yuklenemedi');
              state.turboStudio = payload.turbo_studio;
              render();
            }, 'Muzik yuklendi');
          },
        });
        return field({ id: 'turbo-music-upload-field',
          label: 'Kendi muzigini yukle',
          hint: 'Lisans kaydi icin yaninda <dosya>.license.json arayin; yoksa '
            + 'parca "lisans dogrulanmadi" olarak isaretlenir.',
          control: picker });
      })()));
}

function turboTimelineCard() {
  const line = state.turboStudio.timeline;
  if (!line) return null;
  return el('div', { class: 'card', 'data-testid': 'turbo-timeline' },
    el('h2', { text: 'Zaman cizelgesi' }),
    el('div', { class: 'grid grid--3' },
      techCell('Klip', line.clip_count),
      techCell('Sure', `${line.duration_sec} sn`),
      techCell('Tekrar kullanilan', line.reuse_count)),
    line.reuse_count
      ? alertBox('warning', 'Klipler tekrar kullanildi',
        `${line.reuse_count} yuva ayni kaynaktan tekrar dolduruldu. Daha fazla `
        + 'klip indirirseniz tekrar azalir.')
      : null,
    el('div', { class: 'stack' },
      (line.clips || []).map(clip => el('div', {
        class: 'row', 'data-testid': `turbo-clip-${clip.clip_id}`,
      },
      el('div', {},
        el('span', { text: `${clip.clip_id}  ${clip.start}-${clip.end} sn` }),
        el('div', { class: 'muted', text: clip.attribution || '' })),
      el('div', { class: 'row' },
        clip.reuse_index ? badge('review_required', `tekrar ${clip.reuse_index}`) : null,
        turboButton('Kirp', `turbo-frame-crop-${clip.clip_id}`,
          () => turboPost('/revision',
            { kind: 'frame', target: clip.clip_id, values: { mode: 'crop' } },
            'Kadraj guncellendi')),
        turboButton('Sigdir', `turbo-frame-fit-${clip.clip_id}`,
          () => turboPost('/revision',
            { kind: 'frame', target: clip.clip_id, values: { mode: 'fit' } },
            'Kadraj guncellendi')))))),
    line.attribution_lines && line.attribution_lines.length
      ? el('div', {},
        el('h3', { text: 'Atif' }),
        el('ul', {}, line.attribution_lines.map(t => el('li', { text: t }))))
      : null);
}

function turboRenderCard() {
  const studio = state.turboStudio;
  const renders = studio.renders || {};
  const stages = (studio.progress || {}).stages || [];
  const stageState = key => (stages.find(s => s.key === key) || {}).state;
  const timelineReady = stageState('timeline') === 'completed';
  const previewDone = stageState('preview_render') === 'completed';

  const panel = (preset, label) => {
    const record = renders[preset];
    return el('div', { class: 'stack', 'data-testid': `turbo-render-${preset}` },
      el('div', { class: 'row' },
        el('strong', { text: label }),
        record ? badge(record.stale ? 'review_required' : 'completed',
          record.stale ? 'zaman cizelgesi degisti' : 'hazir')
          : badge('planned', 'uretilmedi')),
      record
        ? el('video', {
          class: 'preview', controls: true, preload: 'metadata',
          'data-testid': `turbo-player-${preset}`,
          src: `/api/projects/${state.projectId}/media/${record.relative_path}`,
        })
        : null,
      record && record.stale
        ? alertBox('warning', 'Bu video guncel degil',
          'Zaman cizelgesi bu render uretildikten sonra degisti. Izlediginiz '
          + 'kayit artik projedeki kurgu degil; yeniden render alin.')
        : null,
      record && record.music_license && record.music_license !== 'verified'
        ? alertBox('warning', 'Muzik lisansi dogrulanmadi',
          'Bu videoda lisans kaydi tamamlanmamis bir parca var.')
        : null);
  };

  return el('div', { class: 'card', 'data-testid': 'turbo-renders' },
    el('h2', { text: 'Render' }),
    panel('preview', 'Onizleme (yarim olcek, hizli)'),
    panel('final', 'Final (tam 1080p)'),
    el('div', { class: 'actions' },
      turboButton('Onizleme uret', 'turbo-render-preview-btn',
        () => turboPost('/render', { preset: 'preview' }, 'Onizleme uretildi'),
        { primary: true, disabled: !timelineReady,
          reason: 'once zaman cizelgesini olusturun' }),
      turboButton('Final uret', 'turbo-render-final-btn',
        () => turboPost('/render', { preset: 'final' }, 'Final uretildi'),
        { disabled: !previewDone,
          reason: 'once onizlemeyi uretip izleyin' })),
    alertBox('info', 'Onizleme ve final ayridir',
      'Onizlemeyi onaylamak finali onaylamaz. Final render dakikalar surer ve '
      + 'yalnizca siz istediginizde baslar.'));
}

function turboRevisionCard() {
  const kinds = state.turboStudio.revision_kinds || [];
  return el('div', { class: 'card', 'data-testid': 'turbo-revisions' },
    el('h2', { text: 'Revizyon turleri ve maliyeti' }),
    el('div', { class: 'stack' },
      kinds.map(item => el('div', { class: 'row' },
        el('span', { text: item.label }),
        badge(item.manifest_only ? 'completed' : 'review_required',
          item.manifest_only ? 'yalnizca render yenilenir'
            : 'yukari-akis yeniden calisir')))),
    alertBox('info', 'Hicbir sey silinmez',
      'Bir revizyon uygulandiginda eski cikti arsive tasinir, uzerine yazilmaz.'));
}

function turboArchiveCard() {
  const archive = state.turboStudio.archive;
  if (!archive) return null;
  const mb = (archive.total_bytes / (1024 * 1024)).toFixed(1);
  const clipMb = (archive.clip_bytes / (1024 * 1024)).toFixed(1);
  return el('div', { class: 'card', 'data-testid': 'turbo-archive' },
    el('h2', { text: 'Arsiv' }),
    el('div', { class: 'grid grid--2' },
      techCell('Toplam boyut', `${mb} MB`),
      techCell('Bunun klipleri', `${clipMb} MB`)),
    // Stated before the operator commits: a Turbo project carries every
    // downloaded clip, and that is not obvious from the rest of the screen.
    alertBox(archive.ready ? 'info' : 'warning',
      archive.ready ? 'Arsivlemeye hazir' : 'Henuz hazir degil',
      archive.ready ? archive.control : archive.reason));
}

function screenTurbo() {
  const studio = state.turboStudio;
  if (!studio) {
    return el('div', { 'data-testid': 'turbo-screen' },
      el('div', { class: 'page-head' }, el('h1', { text: 'Turbo Studio' })),
      alertBox('info', 'Yukleniyor',
        'Turbo is akisi okunuyor. Bu sekmeyi acmak hicbir saglayici istegi yapmaz.'));
  }
  const progress = studio.progress || {};
  return el('div', { 'data-testid': 'turbo-screen' },
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { text: 'Turbo Studio' }),
        el('p', { class: 'subtitle',
          text: 'Brief girdisinden stok goruntulu, seslendirilmis ve altyazili '
            + 'videoya kadar 14 asamali akis.' })),
      studio.started ? badge('review_required', progress.status || 'hazir')
        : badge('planned', 'brief bekliyor')),
    alertBox('info', 'Bu sekmeyi acmak hicbir sey harcamaz',
      'Gorunum yalnizca diskteki artefaktlari okur. Model, seslendirme, stok '
      + 'materyal ve render istekleri yalnizca acikca bir eylem baslattiginizda '
      + 'yapilir.'),
    turboBriefCard(),
    turboEstimateCard(),
    turboStagesCard(),
    turboScriptCard(),
    turboNarrationCard(),
    turboMediaCard(),
    turboMusicCard(),
    turboTimelineCard(),
    turboRenderCard(),
    turboRevisionCard(),
    turboArchiveCard());
}

const RENDERERS = {
  projects: screenProjects, history: screenHistory, phase0: screenPhase0,
  longform: screenLongForm,
  turbo: screenTurbo,
  jobs: screenJobs, moments: screenMoments,
  script: screenScript, voice: screenVoice, render: screenRender, outputs: screenOutputs,
  settings: screenSettings,
};

/* The script tab means different things in the two modes, so it is named for
 * what it actually does in the plan that was approved. Calling it "Script
 * Studio" on a clip project is what suggested subtitles needed a generated
 * script. */
function screenLabel(screen) {
  if (screen.key !== 'script') return screen.label;
  const mode = (((state.project || {}).phase0 || {}).plan || {}).decision || {};
  if (mode.mode && mode.mode !== 'narrated_theory') return 'Altyazi';
  return screen.label;
}

/* ------------------------------------------------------------ rendering */
function renderNav() {
  const nav = document.getElementById('nav');
  nav.replaceChildren(...SCREENS.map(screen => el('button', {
    class: 'nav__item', type: 'button',
    'aria-current': String(state.screen === screen.key),
    disabled: screen.needsProject && !state.projectId,
    onclick: () => goto(screen.key),
  }, screenLabel(screen),
    screen.key === 'jobs' && state.project
      ? badge('running', String((state.project.job_summary || {}).active || 0))
      : null)));

  const owner = (state.workspace && state.workspace.owner) || {};
  document.getElementById('owner-info').replaceChildren(
    el('div', { text: `workspace: ${owner.workspace_id || '-'}` }),
    el('div', { text: `user: ${owner.user_id || '-'}` }),
    el('div', { text: 'yuklenmez · yayinlanmaz' }));
}

function render() {
  renderedProgress.clear();
  renderNav();
  renderAlerts();
  const host = document.getElementById('screen');
  const needsProject = (SCREENS.find(s => s.key === state.screen) || {}).needsProject;
  if (needsProject && !state.project) {
    host.replaceChildren(el('div', { class: 'empty', text: 'Once bir proje sec.' }));
    return;
  }
  const screen = RENDERERS[state.screen]();
  host.replaceChildren(...(state.projectId && state.project
    ? [preflightBanner(), workflowMap(), projectLifecyclePanel(), screen]
    : [screen]));
}

function formatMediaTime(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`;
}

function formatBytes(bytes) {
  const value = Math.max(0, Number(bytes) || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  const digits = index === 0 ? 0 : (amount >= 10 ? 1 : 2);
  return `${amount.toFixed(digits)} ${units[index]}`;
}

async function refresh() {
  state.workspace = await api.get('/api/workspace').catch(() => state.workspace);
  // The CACHED report only, and only on the screen that shows it. Running the
  // checks is an explicit user action, never a side effect of navigation.
  if (state.screen === 'settings' && !state.diagnostics && !state.diagnosticsBusy) {
    const cached = await api.get('/api/diagnostics').catch(() => null);
    if (cached) state.diagnostics = cached.diagnostics;
  }
  // Only on the render screen: describing a revision is a read, but it
  // still walks the project tree and no other screen shows it.
  if (state.screen === 'render' && state.projectId && !state.revision) {
    await refreshRevision();
  }
  // Describing the workflow is a read; it never spends a request.
  if (state.screen === 'longform' && state.projectId) {
    const lf = await api.get(
      `/api/projects/${state.projectId}/long-form`).catch(() => null);
    state.longFormStudio = lf ? lf.long_form_studio : null;
  }
  // Same contract for Turbo: opening the tab reads. It never spends an LLM,
  // TTS, stock-provider, download or render request.
  if (state.screen === 'turbo' && state.projectId) {
    const tb = await api.get(
      `/api/projects/${state.projectId}/turbo`).catch(() => null);
    state.turboStudio = tb ? tb.turbo_studio : null;
  }
  if (state.projectId) {
    try {
      state.project = await api.get(`/api/projects/${state.projectId}`);
    } catch (error) {
      state.project = null;
      state.projectId = null;
      pushAlert('danger', 'Proje acilamadi', error.message, error.hint);
    }
  }
  // Poll only while work is genuinely in flight; a static page must not poll.
  const active = state.project && (state.project.job_summary || {}).active > 0;
  const auto = (state.project && state.project.automation) || {};
  clearTimeout(state.polling);
  if (active) {
    state.polling = setTimeout(refresh, 1500);
  } else if (auto.should_poll && !state.autoBusy) {
    // The SERVER says the flow is running, so keep ticking it. Browser memory
    // is not the source of truth any more: a refresh resumes this loop.
    state.polling = setTimeout(() => automationAction('tick'), 1500);
  } else {
    const lfNext = (((state.longFormStudio || {}).progress || {}).next || {});
    if (state.screen === 'longform' && lfNext.waiting && lfNext.resume_at) {
      // The server owns the persisted wait. The browser only wakes it at the
      // advertised time; reopening the tab reconstructs this timer from disk.
      const dueMs = Date.parse(lfNext.resume_at) - Date.now();
      const delayMs = Math.max(500, Math.min(2147000000, dueMs + 250));
      state.polling = setTimeout(longFormResumeTick, delayMs);
    }
  }
  render();
}

async function goto(screen) {
  state.screen = screen;
  const url = new URL(location.href);
  if (state.projectId) url.searchParams.set('project', state.projectId);
  else url.searchParams.delete('project');
  url.hash = screen;
  // pushState, not replaceState: navigating between screens should be
  // Back-able, which is what a user expects from a multi-screen app.
  if (url.href !== location.href) history.pushState(null, '', url);
  await refresh();
}

async function openProject(projectId, screen = 'phase0') {
  if (state.projectId !== projectId) {
    state.selectedMode = null;
    state.longFormDraft = null;
  }
  state.projectId = projectId;
  await goto(screen);
}

/* Browser navigation. Without these listeners a hash change (Back/Forward, or a
 * pasted #screen URL on an already-open page) silently does nothing: the URL
 * moves but the view does not. */
function screenFromLocation() {
  const screen = (location.hash || '#projects').slice(1) || 'projects';
  return RENDERERS[screen] ? screen : 'projects';
}

function syncFromLocation() {
  const screen = screenFromLocation();
  const project = new URLSearchParams(location.search).get('project');
  const changed = screen !== state.screen || project !== state.projectId;
  state.screen = screen;
  if (project !== state.projectId) {
    state.projectId = project;
    state.project = null;
    state.selectedMode = null;
    state.selectedMoments.clear();
  }
  if (changed) refresh();
}

window.addEventListener('hashchange', syncFromLocation);
window.addEventListener('popstate', syncFromLocation);

(async function boot() {
  state.screen = screenFromLocation();
  await refresh();
})();
