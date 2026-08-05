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
    const options = { method, headers: { ...(headers || {}) } };
    if (body !== undefined && !(body instanceof ArrayBuffer) && !(body instanceof Blob)) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    } else if (body !== undefined) {
      options.body = body;
    }
    const response = await fetch(path, options);
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : null;
    if (!response.ok) {
      const error = (payload && payload.error) || {};
      const failure = new Error(error.message || `HTTP ${response.status}`);
      failure.hint = error.hint || '';
      failure.code = error.code || `http_${response.status}`;
      failure.status = response.status;
      // Which form fields the server rejected, so the UI can mark them.
      failure.fields = error.fields || [];
      throw failure;
    }
    return payload;
  },
  get(path) { return this.request('GET', path); },
  post(path, body, headers) { return this.request('POST', path, { body, headers }); },
};

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
  // The LAST diagnostics report, fetched from the server (never recomputed in
  // the browser). Null until the Settings screen asks for it.
  diagnostics: null,
  diagnosticsBusy: false,
  // Who is doing each review. Every tick is an API call that re-renders the
  // page, and a fresh input would come back empty -- so ticking six boxes meant
  // typing the name six times, and boxes 2..6 failed with "reviewer required"
  // until you did. UI-local like runParams: the RESULT still lives on the
  // server, this is only the input value surviving a repaint. Mirrored into
  // localStorage so it also survives a refresh, and backfilled from whoever
  // ticked the last item so it survives a restart or a different browser.
  reviewers: { render: '', voice: '' },
  // Manual run parameters are UI-local (they are inputs), but every RESULT and
  // the automation flow itself live on the server.
  runParams: {
    video_analysis: { clip_start_sec: 0, clip_duration_sec: 240,
                      whisper_model_size: 'small', scene_detector: 'content', top_n: 5 },
    tts: { voice: 'Kore', language_code: 'tr-TR', narration_direction: '' },
  },
};

const SCREENS = [
  { key: 'projects',  label: 'Projeler',        needsProject: false },
  { key: 'history',   label: 'Kayitlar',        needsProject: false },
  { key: 'phase0',    label: 'Faz 0 · Strateji', needsProject: true },
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
  return field({ ...spec, control: el('textarea', attrs) });
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
  for (const item of fields) markFieldError(item.name, `${item.label} zorunlu.`);
  const first = document.getElementById(fields[0].name);
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
  return el('div', {},
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { text: 'Projeler' }),
        el('p', { class: 'subtitle', text: 'Her proje kendi izole depolama alanina sahiptir.' })),
    ),
    el('div', { class: 'card' },
      el('h2', { text: 'Yeni proje' }),
      requiredLegend(),
      inputField({ id: 'new-project-title', label: 'Proje adi', required: true,
                   attrs: { placeholder: 'Ornek: HOTD sezon analizi', maxlength: 200 } }),
      el('button', {
        class: 'btn btn--primary', 'data-testid': 'create-project',
        onclick: async (event) => {
          const values = validateFields([
            { id: 'new-project-title', label: 'Proje adi', required: true }]);
          if (!values) return;
          const button = event.currentTarget;
          button.disabled = true;   // double-click guard on the client too
          const created = await guard(
            () => api.post('/api/projects', { title: values['new-project-title'] }),
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
    submitted_by: 'console:manual',
  });
}

async function automationAction(action) {
  if (!state.projectId || state.autoBusy) return null;
  state.autoBusy = true;
  render();
  const result = await guard(
    () => api.post(`/api/projects/${state.projectId}/automation`, { action }));
  state.autoBusy = false;
  await refresh();
  return result;
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
  const disabled = active > 0;
  const runLifecycle = async (action, extra, successTitle) => {
    const values = validateFields([
      { id: 'lifecycle-actor', label: 'Islemi yapan kisi', required: true },
    ]);
    if (!values) return null;
    const result = await guard(() => api.post(
      `/api/projects/${state.projectId}/lifecycle`, {
        action, actor: values['lifecycle-actor'], ...extra,
      }), successTitle);
    if (result) await refresh();
    return result;
  };
  const destructiveValues = () => validateFields([
    { id: 'lifecycle-actor', label: 'Islemi yapan kisi', required: true },
    { id: 'lifecycle-confirmation', label: 'Proje adi dogrulamasi', required: true },
  ]);
  return el('details', { class: 'card lifecycle-card', 'data-testid': 'project-lifecycle' },
    el('summary', {},
      el('span', { text: 'Proje islemleri ve onceki asamaya donus' }),
      revisions.length ? badge('review_required', `${revisions.length} revizyon`) : null),
    el('p', { class: 'subtitle lifecycle-lead',
              text: 'Geri donulen asama ve sonraki ciktilar kaybolmaz; proje icindeki '
                    + 'revizyon arsivine tasinir. Aktif is varken bu islemler kilitlidir.' }),
    disabled ? alertBox('warn', 'Proje su anda calisiyor',
      `${active} aktif is tamamlanmadan geri donme, sifirlama veya silme yapilamaz.`) : null,
    requiredLegend(),
    inputField({ id: 'lifecycle-actor', label: 'Islemi yapan kisi', required: true,
                 attrs: { maxlength: 120, placeholder: 'adin' } }),
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
      el('h3', { text: 'Sifirla veya sil' }),
      inputField({ id: 'lifecycle-confirmation', label: 'Proje adi dogrulamasi',
                   required: true,
                   hint: `Devam etmek icin proje adini birebir yaz: ${record.title}`,
                   attrs: { maxlength: 200, autocomplete: 'off',
                            placeholder: record.title || '' } }),
      el('div', { class: 'row' },
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
                        actor: values['lifecycle-actor'] },
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
  const projectCards = projects.map(project => {
    const titleId = `history-title-${project.project_id}`;
    const notesId = `history-notes-${project.project_id}`;
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
              { id: notesId, label: 'Proje notlari' },
            ]);
            if (!values) return;
            const button = event.currentTarget;
            button.disabled = true;
            const updated = await guard(() => api.request(
              'PATCH', `/api/projects/${project.project_id}`,
              { body: { title: values[titleId], notes: values[notesId] || '' } }),
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
      ? el('div', { class: 'card empty', text: 'Henuz kayitli proje yok.' })
      : el('div', { class: 'archive-list' }, projectCards));
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
      inputField({ id: 'plan-by', required: true,
                   label: isEdit ? 'Plani duzenleyen kisi' : 'Secimi yapan kisi',
                   attrs: { maxlength: 120, placeholder: 'adin',
                            value: plan ? (plan.updated_by || plan.selected_by || '') : '' } }),
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
            { id: 'plan-by', label: isEdit ? 'Plani duzenleyen kisi' : 'Secimi yapan kisi',
              required: true },
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
            const actor = values['plan-by'];
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

function screenPhase0() {
  const view = state.project;
  const phase0 = view.phase0;
  const source = (view.project.data || {}).source;
  const savedMode = phase0.plan && phase0.plan.decision && phase0.plan.decision.mode;
  const selectedPlanMode = state.selectedMode || savedMode || null;

  const uploadCard = el('div', { class: 'card' },
    el('h2', { text: '1 · Kaynak video' }),
    source
      ? el('div', { class: 'stack' },
          el('div', { class: 'row' }, badge('completed', 'kayitli'),
            el('strong', { text: source.display_name })),
          el('div', { class: 'dim', text: `${(source.size_bytes / 1048576).toFixed(1)} MB · parmak izi ${String(source.fingerprint_sha256).slice(0, 16)}…` }))
      : el('div', { class: 'stack' },
          el('p', { class: 'muted', text: 'Bu projede henuz kaynak video yok.' }),
          el('input', { type: 'file', id: 'source-file', accept: 'video/*' }),
          el('button', {
            class: 'btn btn--primary',
            onclick: async (event) => {
              const input = document.getElementById('source-file');
              if (!input.files || !input.files[0]) {
                pushAlert('warn', 'Dosya secilmedi', 'Once bir video dosyasi sec.');
                return;
              }
              event.currentTarget.disabled = true;
              const file = input.files[0];
              await guard(() => api.request('POST', `/api/projects/${state.projectId}/source`, {
                body: file, headers: { 'X-Filename': file.name },
              }), 'Kaynak video kaydedildi');
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
          approvalPanel('processing_plan', 'Plani onayla'))
      : el('div', { class: 'stack' },
          requiredLegend(),
          inputField({ id: 'plan-by', label: 'Secimi yapan kisi', required: true,
                       hint: 'Mod secimi bir insan karari; adin plana yazilir.',
                       attrs: { placeholder: 'adin', maxlength: 120 } }),
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
                { id: 'plan-by', label: 'Secimi yapan kisi', required: true },
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
                selected_by: values['plan-by'],
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
              el('td', {}, job.can_retry
                ? el('button', {
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
                : el('span', { class: 'dim', text: job.status === 'failed' ? 'deneme hakki yok' : '' })))))))));
}

function momentAnalysisControls(hasMoments) {
  const gate = (state.project.phase_gates || {}).video_analysis || {};
  return el('section', { class: 'card moment-settings' },
    el('h2', { text: 'Aday uretimi' }),
    el('p', { class: 'subtitle',
              text: 'Kaynak araligini ve kac farkli aday istedigini belirle.' }),
    !gate.allowed ? alertBox('warn', 'Bu faz plan tarafindan kapatildi', gate.reason) : null,
    el('div', { class: 'moment-settings__fields' },
      numberField('va-start', 'Klip baslangici (sn)', 'video_analysis',
                  'clip_start_sec', { min: '0', step: '1' }),
      numberField('va-duration', 'Klip suresi (sn)', 'video_analysis',
                  'clip_duration_sec', { min: '5', max: '3600', step: '5' }),
      numberField('va-topn', 'Aday sayisi (hedef)', 'video_analysis',
                  'top_n', { min: '1', max: '20', step: '1' }),
      selectField('va-whisper', 'Whisper modeli', 'video_analysis',
                  'whisper_model_size', ['tiny', 'small', 'large-v3-turbo']),
      selectField('va-scene', 'Sahne dedektoru', 'video_analysis',
                  'scene_detector', ['content', 'adaptive', 'threshold'])),
    el('button', {
      class: 'btn btn--primary moment-settings__run', 'data-testid': 'run-video_analysis',
      disabled: !gate.allowed,
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        state.selectedMoments.clear();
        await guard(() => submitManualJob('video_analysis',
                                          { ...state.runParams.video_analysis }),
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
        ? el('span', { text: `${summary.analysis_duration_sec} sn analiz edildi` }) : null,
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
    requiredLegend(),
    inputField({ id: 'moment-by', label: 'Inceleyen kisi', required: true,
                 attrs: { placeholder: 'adin', maxlength: 120 } }),
    el('button', {
      class: 'btn btn--primary', 'data-testid': 'save-moment-selection',
      disabled: selectedRank === null || !gate.allowed,
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        await guard(() => api.post(`/api/projects/${state.projectId}/moment-selection`, {
          selected_rank: selectedRank,
          reviewed_by: document.getElementById('moment-by').value,
          require_preview: false,
        }), 'Moment secimi kaydedildi');
        await refresh();
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
              el('span', { text: `${moment.source_start_sec}s - ${moment.source_end_sec}s` }),
              el('span', { text: `Skor ${moment.score}` }),
              selection && selection.selected_moment_rank === moment.rank && !staleSelection
                ? badge('approved', 'kayitli') : null),
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
  return el('div', { class: 'stack' },
    el('div', { class: 'row' },
      badge(approval.status || 'missing'),
      approval.approved ? badge(approval.effective_valid ? 'approved' : 'failed',
        approval.effective_valid ? 'onay gecerli' : 'ONAY GECERSIZ') : badge('review_required', 'onay bekliyor')),
    approval.approved && !approval.effective_valid
      ? alertBox('danger', 'Onay artik gecerli degil', approval.reason,
          (approval.upstream_invalid || []).length
            ? `Yukari-akis degisti: ${(approval.upstream_invalid || []).join(', ')}. Once onu duzelt.`
            : 'Icerik onaydan sonra degismis. Yeniden incele ve yeni hash ile onayla.')
      : null,
    approval.blocker ? alertBox('warn', 'Onay engelli', approval.blocker,
      (approval.blocking_conditions || []).join(', ')) : null,
    approval.content_sha256
      ? el('div', {},
          el('h3', { text: 'Incelenen icerigin SHA-256 degeri' }),
          el('div', { class: 'pre mono', 'data-testid': `hash-${kind}`,
                      text: approval.content_sha256 }))
      : null,
    approval.reviewable
      ? el('div', {},
          requiredLegend(),
          inputField({ id: `${kind}-hash`, label: 'Yukaridaki hash i buraya yapistir',
                       required: true,
                       attrs: { class: 'mono', placeholder: 'sha256…' } }),
          inputField({ id: `${kind}-by`, label: 'Onaylayan', required: true,
                       attrs: { placeholder: 'adin', maxlength: 120 } }),
          el('button', {
            class: 'btn btn--primary', 'data-testid': `approve-${kind}`,
            onclick: async (event) => {
              const values = validateFields([
                { id: `${kind}-hash`, label: 'Dogrulama hash i',
                  required: true, minLength: 64 },
                { id: `${kind}-by`, label: 'Onaylayan', required: true },
              ]);
              if (!values) return;
              event.currentTarget.disabled = true;
              await guard(() => api.post(`/api/projects/${state.projectId}/approvals/${kind}`, {
                reviewed_sha256: values[`${kind}-hash`],
                approved_by: values[`${kind}-by`],
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

function screenScript() {
  const decision = ((state.project.phase0 || {}).plan || {}).decision || {};
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
      inputField({ id: 'script-editor-by', label: 'Duzenleyen kisi', required: true,
                   attrs: { maxlength: 120, placeholder: 'adin' } }),
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
            { id: 'script-editor-by', label: 'Duzenleyen kisi', required: true },
            { id: 'script-editor-text', label: 'Script metni', required: true },
            { id: 'script-editor-notes', label: 'Revizyon notu' },
          ]);
          if (!values) return;
          event.currentTarget.disabled = true;
          const saved = await guard(() => api.request(
            'PATCH', `/api/projects/${state.projectId}/script`, {
              body: { updated_by: values['script-editor-by'],
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



/* The single reviewer input the checklist AND the revision request both read.
 * Module scope because two panels share it: keeping it local to one of them is
 * how the revision button ended up throwing a ReferenceError on click. */
const REVIEWER_INPUT_ID = 'render-reviewer';

/* Render review and voice review are the same interaction over different media,
 * so they are ONE component parameterised by kind rather than two that drift.
 * The render ids keep their historical, unprefixed form; voice gets its own. */
const REVIEW_KINDS = {
  render: {
    viewKey: 'render_review', endpoint: 'render-review',
    reviewerId: 'render-reviewer', testid: (name) => name,
    mediaNoun: 'video', regeneratedNoun: 'video yeniden uretilirse',
    reviewerHint: 'Kontrol maddesini isaretlemek bir insan karari.',
    revisionLead: 'Bir sorun gordiysen not birak ve duzeltmenin yapilacagi sekmeye git. '
                  + 'Bu islem videoyu DEGISTIRMEZ ve hicbir durumu onaylamaz.',
    targets: [['moments', 'Moment Explorer (klip araligi / moment secimi)'],
              ['script', 'Script Studio (metin / hook revizyonu)'],
              ['voice', 'Voice Studio (seslendirme revizyonu)'],
              ['render', 'Render Studio (yeniden render)'],
              ['phase0', 'Faz 0 (plan / render ayarlari)']],
  },
  voice: {
    viewKey: 'voice_review', endpoint: 'voice-review',
    reviewerId: 'voice-reviewer', testid: (name) => `voice-${name}`,
    mediaNoun: 'ses', regeneratedNoun: 'ses yeniden uretilirse',
    reviewerHint: 'Sesi DINLEYIP isaretlemek bir insan karari.',
    revisionLead: 'Bir sorun duydysan not birak. Bu islem sesi DEGISTIRMEZ ve '
                  + 'hicbir durumu onaylamaz; duzeltmeyi sen baslatirsin.',
    targets: [['voice', 'Voice Studio (ses/ton/tempo ayari, yeniden uret)'],
              ['script', 'Script Studio (metni duzelt)']],
  },
};

function reviewerName(kind) {
  if (state.reviewers[kind]) return state.reviewers[kind];
  try {
    const stored = localStorage.getItem(`reviewer:${kind}`);
    if (stored) { state.reviewers[kind] = stored; return stored; }
  } catch (error) { /* private mode: fall through to the server value */ }
  // Server-derived fallback: whoever ticked an item last. Survives a restart
  // and a different browser, which localStorage alone does not.
  const review = (state.project && state.project[REVIEW_KINDS[kind].viewKey]) || {};
  const ticked = (review.items || []).filter(item => item.checked_by);
  return ticked.length ? ticked[ticked.length - 1].checked_by : '';
}

function setReviewerName(kind, value) {
  state.reviewers[kind] = value;
  try { localStorage.setItem(`reviewer:${kind}`, value); } catch (error) { /* ignore */ }
}

function reviewChecklist(kind) {
  const config = REVIEW_KINDS[kind];
  const review = (state.project && state.project[config.viewKey]) || {};
  const items = review.items || [];
  if (!items.length) return null;
  const reviewerId = config.reviewerId;
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
    requiredLegend(),
    inputField({ id: reviewerId, label: 'Inceleyen kisi', required: true,
                 hint: config.reviewerHint,
                 attrs: { 'data-testid': reviewerId, placeholder: 'adin',
                          maxlength: 120, value: reviewerName(kind),
                          oninput: (event) => {
                            setReviewerName(kind, event.currentTarget.value);
                          } } }),
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
          const who = ((document.getElementById(reviewerId) || {}).value
                       || reviewerName(kind) || '').trim();
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
                const who = ((document.getElementById(config.reviewerId) || {}).value
                             || reviewerName(kind) || 'console').trim();
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
                { id: config.reviewerId, label: 'Inceleyen kisi', required: true },
                { id: notesId, label: 'Revizyon notu', required: true },
              ]);
              if (!values) return;
              event.currentTarget.disabled = true;
              await guard(() => api.post(
                `/api/projects/${state.projectId}/${config.endpoint}`, {
                  action: 'request_revision',
                  requested_by: values[config.reviewerId],
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
  return reviewRevisionPanel('render');
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


function screenOutputs() {
  const outputs = state.project.outputs || [];
  return el('div', {},
    el('div', { class: 'page-head' }, el('div', {},
      el('h1', { text: 'Ciktilar ve gecmis' }),
      el('p', { class: 'subtitle', text: 'Hicbir cikti otomatik yuklenmez veya yayinlanmaz.' }))),
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
      el('p', { class: 'subtitle', text: 'Secret DEGERLERI hicbir zaman tarayiciya gonderilmez.' }))),
    preflightPanel(),
    diagnosticsPanel(),
    phaseProgress('diagnostics'),
    el('div', { class: 'card' },
      el('h2', { text: 'Kimlik bilgileri' }),
      el('div', { class: 'table-scroll' }, el('table', {},
        el('thead', {}, el('tr', {}, el('th', { text: 'Anahtar' }), el('th', { text: 'Durum' }),
          el('th', { text: 'Kullanim' }))),
        el('tbody', {}, secretRows)))),
    el('div', { class: 'card' },
      el('h2', { text: 'Sahiplik' }),
      el('div', { class: 'mono dim', text: JSON.stringify(source.owner || {}, null, 2) })),
    el('div', { class: 'card' },
      el('h2', { text: 'Henuz uygulanmadi (planlandi)' }),
      el('div', { class: 'stack' }, plannedRows)));
}

const RENDERERS = {
  projects: screenProjects, history: screenHistory, phase0: screenPhase0,
  jobs: screenJobs, moments: screenMoments,
  script: screenScript, voice: screenVoice, render: screenRender, outputs: screenOutputs,
  settings: screenSettings,
};

/* ------------------------------------------------------------ rendering */
function renderNav() {
  const nav = document.getElementById('nav');
  nav.replaceChildren(...SCREENS.map(screen => el('button', {
    class: 'nav__item', type: 'button',
    'aria-current': String(state.screen === screen.key),
    disabled: screen.needsProject && !state.projectId,
    onclick: () => goto(screen.key),
  }, screen.label,
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

async function refresh() {
  state.workspace = await api.get('/api/workspace').catch(() => state.workspace);
  // The CACHED report only, and only on the screen that shows it. Running the
  // checks is an explicit user action, never a side effect of navigation.
  if (state.screen === 'settings' && !state.diagnostics && !state.diagnosticsBusy) {
    const cached = await api.get('/api/diagnostics').catch(() => null);
    if (cached) state.diagnostics = cached.diagnostics;
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
  if (state.projectId !== projectId) state.selectedMode = null;
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
