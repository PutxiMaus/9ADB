const APP_ADMIN_MODE = document.body.dataset.admin === '1';

const state = {
  status: null,
  currentTab: 'dashboard',
  screenTimer: null,
  screenLastFrameAt: 0,
  apps: {items: [], loaded: false, kind: 'user', query: ''},
  files: {androidPath: '/sdcard', androidParent: '/sdcard'},
  scripts: {items: [], loaded: false, editId: ''},
  fastboot: {devices: [], loaded: false},
  clientAgents: {items: [], loaded: false},
  clientAgents: {items: [], loaded: false},
};

const pageInfo = {
  dashboard: ['Inicio', 'Conexión rápida, Wi‑Fi y perfiles.'],
  network: ['Red y recientes', 'Escaneo LAN, conectados y dispositivos recientes.'],
  screen: ['Pantalla', 'Scrcpy real y ajustes de visor.'],
  apps: ['Apps', 'Instaladas, abrir/cerrar y acciones de paquetes.'],
  files: ['Archivos', 'Explorador del Android activo.'],
  scripts: ['Scripts', 'Comandos ADB personalizados por fases.'],
  fastboot: ['Bootloader', 'Fastboot, recuperación y flasheo controlado.'],
  agent: ['Agente', 'Descarga y estado del agente cliente.'],
  commands: ['Comandos', 'Acciones agrupadas por tipo.'],
};

const tabLogs = {
  dashboard: [],
  network: [],
  screen: [],
  apps: [],
  files: [],
  scripts: [],
  fastboot: [],
  agent: [],
  commands: [],
};

const commandGroups = {
  control: ['home', 'back', 'recents', 'power', 'volume_up', 'volume_down', 'mute', 'notifications', 'quick_settings'],
  apps: ['settings', 'play_store', 'youtube', 'chrome', 'gmail', 'maps', 'photos', 'camera', 'phone', 'messages', 'contacts', 'calendar', 'clock', 'calculator'],
};

function $(id) { return document.getElementById(id); }

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function cleanMessage(value) {
  let text = String(value || '').replace(/\r/g, '').trim();
  if (!text) return '';
  if (text.includes('<!doctype html') || text.includes('<html')) {
    const title = text.match(/<title>(.*?)<\/title>/i)?.[1];
    const h1 = text.match(/<h1>(.*?)<\/h1>/i)?.[1];
    text = title || h1 || 'Error HTTP.';
  }
  return text
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .split('\n')
    .map(line => line.replace(/[\t ]+/g, ' ').trim())
    .join('\n')
    .replace(/\n{4,}/g, '\n\n\n')
    .trim();
}


function nowTime() {
  return new Date().toLocaleTimeString('es-ES', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function appendTabLog(tab, message, ok = true) {
  const realTab = tabLogs[tab] ? tab : state.currentTab;
  const prefix = ok ? 'OK' : 'ERROR';
  const clean = cleanMessage(message) || (ok ? 'Hecho.' : 'Error.');
  tabLogs[realTab].push(`[${nowTime()}] ${prefix} · ${clean}`);
  if (tabLogs[realTab].length > 700) tabLogs[realTab] = tabLogs[realTab].slice(-700);
  const box = $(`log-${realTab}`);
  if (box) {
    const wasNearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 36;
    box.textContent = tabLogs[realTab].join('\n\n') || 'Sin actividad todavía.';
    if (wasNearBottom) box.scrollTop = box.scrollHeight;
  }
}


function initLogs() {
  Object.keys(tabLogs).forEach(tab => {
    const box = $(`log-${tab}`);
    if (box) box.textContent = 'Sin actividad todavía.';
  });
}

async function api(path, options = {}) {
  const config = {headers: {'Content-Type': 'application/json'}, ...options};
  if (config.body && typeof config.body !== 'string') config.body = JSON.stringify(config.body);
  const res = await fetch(path, config);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); }
  catch { data = {ok: res.ok, message: cleanMessage(text)}; }
  if (!res.ok) {
    data.ok = false;
    data.message = cleanMessage(data.message || text) || `HTTP ${res.status}`;
  }
  return data;
}

async function uploadApi(path, formData) {
  const res = await fetch(path, {method: 'POST', body: formData});
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); }
  catch { data = {ok: res.ok, message: cleanMessage(text)}; }
  if (!res.ok) {
    data.ok = false;
    data.message = cleanMessage(data.message || text) || `HTTP ${res.status}`;
  }
  return data;
}

function triggerDownload(result) {
  if (!result || !result.download_url) return;
  const a = document.createElement('a');
  a.href = result.download_url;
  a.download = result.download_name || '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function setBusy(button, busy = true) {
  if (!button) return;
  if (busy) {
    button.dataset.oldText = button.textContent;
    button.disabled = true;
    button.textContent = '...';
  } else {
    button.disabled = false;
    button.textContent = button.dataset.oldText || button.textContent;
  }
}

async function runAction(button, action, options = {}) {
  const tab = options.tab || state.currentTab;
  setBusy(button, true);
  try {
    const result = await action();
    const msg = cleanMessage(result?.message) || (result?.ok ? 'Hecho.' : 'No se pudo completar.');
    if (options.output && msg) $(options.output).textContent = msg;
    appendTabLog(tab, msg, Boolean(result?.ok));
    if (result?.ok) triggerDownload(result);
    await refreshAll();
    if (tab === 'network') await refreshNetworkLast();
    return result;
  } catch (error) {
    appendTabLog(tab, error.message || String(error), false);
    return {ok: false, message: error.message || String(error)};
  } finally {
    setBusy(button, false);
  }
}

function setTab(tab) {
  state.currentTab = tab;
  document.querySelectorAll('.tab-button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === `tab-${tab}`));
  const [title, subtitle] = pageInfo[tab] || pageInfo.dashboard;
  $('pageTitle').textContent = title;
  $('pageSubtitle').textContent = subtitle;
  syncScreenLoop();
  if (tab === 'network') refreshNetworkLast();
  if (tab === 'apps' && !state.apps.loaded && state.status?.active) loadApps($('appsRefresh'));
  if (tab === 'network' && !state.clientAgents.loaded) refreshClientAgents($('clientAgentsRefresh'));
  if (tab === 'files') refreshFiles($('filesRefresh'));
  if (tab === 'scripts' && !state.scripts.loaded) loadScripts($('scriptsRefresh'));
  if (tab === 'fastboot' && !state.fastboot.loaded) refreshFastboot($('fastbootRefresh'));
  if (tab === 'agent' && !state.clientAgents.loaded) refreshClientAgents($('clientAgentsRefresh'));
}

function renderHealth(ok) {
  $('healthDot').classList.toggle('ok', Boolean(ok));
  $('healthDot').classList.toggle('bad', !ok);
}

function endpointParts(serial) {
  const match = String(serial || '').match(/^(\d+\.\d+\.\d+\.\d+):(\d+)$/);
  if (!match) return null;
  return {ip: match[1], port: Number(match[2] || 5555)};
}

function profileForSerial(serial) {
  const parts = endpointParts(serial);
  if (!parts || !state.status?.profiles) return null;
  return Object.values(state.status.profiles).find(profile => {
    const pPort = Number(profile.port || 5555);
    return String(profile.ip || '').trim() === parts.ip && pPort === parts.port;
  }) || null;
}

function displayDeviceName(serial) {
  const profile = profileForSerial(serial);
  return profile?.name || serial;
}

function deviceSubtitle(device) {
  const bits = [];
  if (device?.model) bits.push(device.model);
  if (device?.device) bits.push(device.device);
  if (device?.detail) bits.push(device.detail);
  return bits.join(' · ') || 'Sin detalles';
}

function renderActive(status) {
  const box = $('activeDeviceBox');
  const active = status.active;
  const info = status.active_info;
  if (!active) {
    box.innerHTML = '<span class="empty">Sin dispositivo activo</span>';
    return;
  }
  const statusText = info?.status ? ` · ${info.status}` : ' · no visible';
  const shownName = displayDeviceName(active);
  const serialLine = shownName === active ? '' : `${active} · `;
  box.innerHTML = `<strong>${escapeHtml(shownName)}</strong><small>${escapeHtml(serialLine)}${escapeHtml(deviceSubtitle(info))}${escapeHtml(statusText)}</small>`;
}

function renderProfiles(status) {
  const profiles = status.profiles || {};
  const activeProfileId = status.state?.active_profile_id || '';
  const items = Object.values(profiles);
  const container = $('profilesList');
  if (!items.length) {
    container.innerHTML = '<p class="hint">Sin perfiles guardados.</p>';
    return;
  }
  container.innerHTML = items.map(profile => {
    const endpoint = profile.ip ? `${profile.ip}:${profile.port || 5555}` : 'sin IP';
    const active = activeProfileId === profile.id;
    return `
      <div class="item-card ${active ? 'active' : ''}">
        <div class="item-main">
          <div>
            <h4>${escapeHtml(profile.name || profile.id)}</h4>
            <p>${escapeHtml(endpoint)}${profile.mac ? ` · ${escapeHtml(profile.mac)}` : ''}</p>
            ${profile.notes ? `<p>${escapeHtml(profile.notes)}</p>` : ''}
          </div>
        </div>
        <div class="item-actions">
          <button type="button" class="primary" data-profile-connect="${escapeHtml(profile.id)}">Conectar</button>
          <button type="button" class="secondary" data-profile-edit="${escapeHtml(profile.id)}">Editar</button>
          <button type="button" class="danger-soft" data-profile-delete="${escapeHtml(profile.id)}">Borrar</button>
        </div>
      </div>`;
  }).join('');
}

function renderDevices(status) {
  const devices = status.devices || [];
  const active = status.active || '';
  const container = $('devicesList');
  if (!devices.length) {
    container.innerHTML = '<p class="hint">No hay dispositivos conectados. Por USB acepta RSA; por Wi‑Fi conecta desde Inicio o Red.</p>';
  } else {
    container.innerHTML = devices.map(device => {
      const isActive = active === device.serial;
      const profile = profileForSerial(device.serial);
      const title = profile?.name || device.serial;
      const details = profile ? `${device.serial} · ${device.kind} · ${device.status} · ${deviceSubtitle(device)}` : `${device.kind} · ${device.status} · ${deviceSubtitle(device)}`;
      return `
        <div class="item-card ${isActive ? 'active' : ''}">
          <div class="item-main">
            <div>
              <h4>${escapeHtml(title)}</h4>
              <p>${escapeHtml(details)}</p>
            </div>
          </div>
          <div class="item-actions">
            <button type="button" class="primary" data-device-active="${escapeHtml(device.serial)}">Usar</button>
            ${device.kind === 'wifi' ? `<button type="button" class="secondary" data-device-disconnect="${escapeHtml(device.serial)}">Desconectar</button>` : ''}
          </div>
        </div>`;
    }).join('');
  }
  renderRecentDevices(status);
}

function renderRecentDevices(status) {
  const connectedSerials = new Set((status.devices || []).map(d => d.serial));
  const recents = (status.recent_devices || []).filter(item => item.serial && !connectedSerials.has(item.serial));
  const container = $('recentDevicesList');
  if (!container) return;
  if (!recents.length) {
    container.innerHTML = '<p class="hint">Cuando desconectes un dispositivo Wi‑Fi aparecerá aquí para reconectarlo rápido.</p>';
    return;
  }
  container.innerHTML = recents.map(item => {
    const name = displayDeviceName(item.serial);
    const line = name === item.serial ? `Último uso: ${item.last_seen || 'sin fecha'}` : `${item.serial} · Último uso: ${item.last_seen || 'sin fecha'}`;
    return `
    <div class="item-card">
      <div class="item-main">
        <div>
          <h4>${escapeHtml(name)}</h4>
          <p>${escapeHtml(line)}</p>
        </div>
      </div>
      <div class="item-actions">
        <button type="button" class="primary" data-recent-connect="${escapeHtml(item.serial)}">Reconectar</button>
        <button type="button" class="danger-soft" data-recent-remove="${escapeHtml(item.serial)}">Eliminar</button>
      </div>
    </div>`;
  }).join('');
}

function renderCommands(status) {
  const commands = status.quick_commands || {};
  const renderGroup = (target, ids) => {
    const el = $(target);
    el.innerHTML = ids
      .filter(id => commands[id])
      .map(id => `<button type="button" class="secondary" data-command="${escapeHtml(id)}">${escapeHtml(commands[id])}</button>`)
      .join('') || '<p class="hint">Sin comandos.</p>';
  };
  renderGroup('controlCommands', commandGroups.control);
  renderGroup('appCommands', commandGroups.apps);
}


function scrcpyViewerUrl() {
  const port = state.status?.screen?.visor?.public_port || 20010;
  return `${location.protocol}//${location.hostname}:${port}/`;
}

function readScrcpySettingsForm() {
  return {
    max_size: Number($('scrcpyMaxSize')?.value || 800),
    max_fps: Number($('scrcpyMaxFps')?.value || 20),
    video_bit_rate: $('scrcpyBitrate')?.value || '4M',
    audio: Boolean($('scrcpyAudio')?.checked),
    control: Boolean($('scrcpyControl')?.checked),
    turn_screen_off: Boolean($('scrcpyTurnOff')?.checked),
    stay_awake: Boolean($('scrcpyStayAwake')?.checked),
    fullscreen: Boolean($('scrcpyFullscreen')?.checked),
  };
}

function applyScrcpySettings(settings = {}) {
  if ($('scrcpyMaxSize')) $('scrcpyMaxSize').value = String(settings.max_size || 800);
  if ($('scrcpyMaxFps')) $('scrcpyMaxFps').value = String(settings.max_fps || 20);
  if ($('scrcpyBitrate')) $('scrcpyBitrate').value = String(settings.video_bit_rate || '4M');
  if ($('scrcpyAudio')) $('scrcpyAudio').checked = Boolean(settings.audio);
  if ($('scrcpyControl')) $('scrcpyControl').checked = settings.control !== false;
  if ($('scrcpyTurnOff')) $('scrcpyTurnOff').checked = Boolean(settings.turn_screen_off);
  if ($('scrcpyStayAwake')) $('scrcpyStayAwake').checked = settings.stay_awake !== false;
  if ($('scrcpyFullscreen')) $('scrcpyFullscreen').checked = settings.fullscreen !== false;
}

function renderScreen(status) {
  const screen = status.screen || {mode: 'none', active: false, interval_ms: 1200};
  const text = $('screenStatusText');
  const img = $('screenImage');
  const placeholder = $('screenPlaceholder');
  const interval = $('screenInterval');
  const openButton = $('scrcpyOpenButton');

  if (interval && screen.interval_ms) interval.value = String(screen.interval_ms);
  applyScrcpySettings(screen.scrcpy_settings || {});

  const visor = screen.visor || {};
  const scrcpyRunning = Boolean(visor.running);
  const visorReady = Boolean(visor.ok);
  const url = scrcpyViewerUrl();

  if (openButton) openButton.disabled = !visorReady;

  if (scrcpyRunning) {
    text.textContent = `Scrcpy activo · ${displayDeviceName(visor.serial) || visor.serial} · abre el visor en 20010`;
  } else {
    const visorText = visorReady ? 'Visor ADB listo en puerto 20010.' : `Visor ADB no disponible: ${visor.message || 'sin respuesta'}`;
    text.textContent = status.active
      ? `${visorText} Dispositivo activo: ${displayDeviceName(status.active) || status.active}.`
      : `${visorText} Selecciona un dispositivo activo.`;
  }

  if (screen.mode === 'light' && status.active) {
    placeholder?.classList.add('hidden');
    img?.classList.add('visible');
  } else {
    placeholder?.classList.remove('hidden');
    img?.classList.remove('visible');
    img?.removeAttribute('src');
  }
  syncScreenLoop();
}

function screenIsActive() {
  return state.status?.screen?.mode === 'light' && Boolean(state.status?.active);
}

function selectedScreenInterval() {
  const value = Number($('screenInterval')?.value || state.status?.screen?.interval_ms || 1200);
  return Math.max(500, Math.min(value || 1200, 5000));
}

function updateScreenFrame(force = false) {
  if (!screenIsActive()) return;
  const img = $('screenImage');
  if (!img) return;
  const now = Date.now();
  const interval = selectedScreenInterval();
  if (!force && now - state.screenLastFrameAt < Math.max(250, interval - 80)) return;
  state.screenLastFrameAt = now;
  img.src = `/api/screen/frame?ts=${now}`;
}

function stopScreenLoop() {
  if (state.screenTimer) {
    clearInterval(state.screenTimer);
    state.screenTimer = null;
  }
}

function syncScreenLoop() {
  stopScreenLoop();
  if (state.currentTab !== 'screen' || !screenIsActive()) return;
  updateScreenFrame(true);
  state.screenTimer = setInterval(() => updateScreenFrame(false), selectedScreenInterval());
}



function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = n;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function renderAndroidFiles(data) {
  state.files.androidPath = data.path || state.files.androidPath || '/sdcard';
  state.files.androidParent = data.parent || '/sdcard';
  $('androidPathInput').value = state.files.androidPath;
  $('androidPathLabel').textContent = state.files.androidPath;
  const list = $('androidFilesList');
  const items = data.items || [];
  if (!items.length) {
    list.innerHTML = `<p class="hint">${data.ok === false ? escapeHtml(data.message || 'No se pudo abrir esta ruta.') : 'Carpeta vacía o sin permisos.'}</p>`;
    return;
  }
  list.innerHTML = items.map(item => `
    <div class="file-row ${item.is_dir ? 'dir' : 'file'}">
      <button type="button" class="file-name" data-android-open="${escapeHtml(item.path)}" data-is-dir="${item.is_dir ? '1' : '0'}" title="${escapeHtml(item.path)}">
        <span class="file-icon">${item.is_dir ? '📁' : '📄'}</span>
        <span>${escapeHtml(item.name)}</span>
      </button>
      <span class="file-meta">${item.is_dir ? 'carpeta' : formatBytes(item.size)}${item.date ? ` · ${escapeHtml(item.date)}` : ''}</span>
      <div class="file-actions-row">
        ${item.is_dir ? `<button type="button" class="secondary" data-android-download="${escapeHtml(item.path)}">Descargar ZIP</button>` : ''}
        <button type="button" class="danger-soft" data-android-delete="${escapeHtml(item.path)}">Borrar</button>
      </div>
    </div>`).join('');
}

async function refreshAndroidFiles(button = null) {
  return runAction(button || $('filesRefresh'), async () => {
    const data = await api(`/api/files/android/list?path=${encodeURIComponent(state.files.androidPath || '/sdcard')}`);
    renderAndroidFiles(data);
    return {...data, message: data.ok ? `Android: ${(data.items || []).length} elementos.` : data.message};
  }, {tab: 'files'});
}

async function refreshFiles(button = null) {
  return refreshAndroidFiles(button || $('filesRefresh'));
}

function filenameFromDisposition(disposition, fallback = 'android-file') {
  const text = String(disposition || '');
  const utf = text.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf) return decodeURIComponent(utf[1]);
  const ascii = text.match(/filename="?([^";]+)"?/i);
  if (ascii) return ascii[1];
  return fallback;
}

async function downloadAndroidPath(button, path) {
  setBusy(button, true);
  try {
    appendTabLog('files', `Descargando temporalmente: ${path}`, true);
    const res = await fetch('/api/files/android/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path}),
    });
    const contentType = res.headers.get('content-type') || '';
    if (!res.ok || contentType.includes('application/json')) {
      let data = null;
      try { data = await res.json(); } catch { data = {message: await res.text()}; }
      appendTabLog('files', data?.message || `HTTP ${res.status}`, false);
      return;
    }
    const blob = await res.blob();
    const name = filenameFromDisposition(res.headers.get('content-disposition'), path.split('/').pop() || 'android-file');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    appendTabLog('files', `Descargado al navegador: ${name}`, true);
  } catch (error) {
    appendTabLog('files', error.message || String(error), false);
  } finally {
    setBusy(button, false);
  }
}

async function uploadAndroidFile(button, file) {
  if (!file) return;
  const data = new FormData();
  data.append('file', file);
  data.append('path', state.files.androidPath || '/sdcard/Download');
  await runAction(button, () => uploadApi('/api/files/android/upload', data), {tab: 'files'});
  await refreshAndroidFiles();
}

function scriptCommandsToText(commands) {
  return (commands || []).join('\n');
}

function clearScriptForm() {
  state.scripts.editId = '';
  $('scriptForm').dataset.editId = '';
  $('scriptName').value = '';
  $('scriptDescription').value = '';
  $('scriptCommands').value = '';
  $('scriptSaveButton').textContent = 'Guardar script';
}

function fillScriptForm(script) {
  state.scripts.editId = script.id;
  $('scriptForm').dataset.editId = script.id;
  $('scriptName').value = script.name || '';
  $('scriptDescription').value = script.description || '';
  $('scriptCommands').value = scriptCommandsToText(script.commands || []);
  $('scriptSaveButton').textContent = 'Guardar cambios';
  setTab('scripts');
}

function fillNewScriptTemplate({name, description, commands}) {
  clearScriptForm();
  $('scriptName').value = name || '';
  $('scriptDescription').value = description || '';
  $('scriptCommands').value = scriptCommandsToText(commands || []);
  appendTabLog('scripts', `Plantilla cargada: ${name}. Revisa y pulsa Guardar script si la quieres conservar.`, true);
}

function loadInstallPermissionTemplate() {
  fillNewScriptTemplate({
    name: 'InstallPermission',
    description: 'Portado del BAT antiguo: intenta permitir instalación APK por ADB y desactivar verificadores.',
    commands: [
      'shell settings get global verifier_verify_adb_installs',
      'shell settings put global verifier_verify_adb_installs 0',
      'shell settings get global package_verifier_enable',
      'shell settings put global package_verifier_enable 0',
      'shell settings get global install_non_market_apps',
      'shell settings put global install_non_market_apps 1',
      'shell settings get global adb_enabled',
      'shell settings put global adb_enabled 1',
    ],
  });
}

function loadOpenLinkTemplate() {
  const url = ($('openLinkTemplateUrl')?.value || 'https://www.google.com').trim() || 'https://www.google.com';
  fillNewScriptTemplate({
    name: 'OpenLink',
    description: `Portado del BAT antiguo: abre ${url} en el navegador del Android.`,
    commands: [
      `shell am start -a android.intent.action.VIEW -d ${JSON.stringify(url)} com.android.chrome`,
    ],
  });
}

async function runOpenLink(button) {
  const url = prompt('Link para abrir en el Android activo:', 'https://');
  if (!url || !url.trim()) return;
  await runAction(button, () => api('/api/commands/open-link', {method: 'POST', body: {url: url.trim()}}), {tab: 'commands', output: 'commandOutput'});
}


function renderScripts(data = null) {
  if (data) {
    state.scripts.items = data.scripts || [];
    state.scripts.loaded = true;
  }
  const list = $('scriptsList');
  const summary = $('scriptsSummary');
  if (!list || !summary) return;
  const items = state.scripts.items || [];
  summary.textContent = `${items.length} scripts guardados`;
  if (!items.length) {
    list.innerHTML = '<p class="hint">No hay scripts todavía. Crea uno arriba.</p>';
    return;
  }
  list.innerHTML = items.map(script => `
    <div class="script-card">
      <div class="script-info">
        <h4>${escapeHtml(script.name || script.id)}</h4>
        <p>${escapeHtml(script.description || 'Sin descripción.')}</p>
        <pre>${escapeHtml(scriptCommandsToText(script.commands || []))}</pre>
      </div>
      <div class="script-actions">
        <button type="button" class="primary" data-script-run="${escapeHtml(script.id)}">Ejecutar</button>
        <button type="button" class="secondary" data-script-edit="${escapeHtml(script.id)}">Editar</button>
        <button type="button" class="secondary" data-script-duplicate="${escapeHtml(script.id)}">Duplicar</button>
        <button type="button" class="danger-soft" data-script-delete="${escapeHtml(script.id)}">Borrar</button>
      </div>
    </div>`).join('');
}

async function loadScripts(button = $('scriptsRefresh')) {
  return runAction(button, async () => {
    const data = await api('/api/scripts');
    renderScripts(data);
    return {...data, message: data.ok ? `${(data.scripts || []).length} scripts cargados.` : data.message};
  }, {tab: 'scripts'});
}

function findScript(id) {
  return (state.scripts.items || []).find(item => item.id === id);
}

async function saveScript(button) {
  const editId = $('scriptForm').dataset.editId || '';
  const body = {
    name: $('scriptName').value,
    description: $('scriptDescription').value,
    commands: $('scriptCommands').value,
  };
  return runAction(button, async () => {
    const data = await api(editId ? `/api/scripts/${encodeURIComponent(editId)}` : '/api/scripts', {
      method: 'POST',
      body,
    });
    if (data.ok) {
      clearScriptForm();
      if (data.scripts) renderScripts(data);
    }
    return data;
  }, {tab: 'scripts'});
}

function appKindLabel(kind) {
  if (kind === 'system') return 'Sistema';
  if (kind === 'all') return 'Todas';
  return 'Usuario';
}

function renderApps(data = null) {
  if (data) {
    state.apps = {
      items: data.apps || [],
      loaded: true,
      kind: data.kind || $('appsKind')?.value || 'user',
      query: data.query || $('appsSearch')?.value || '',
    };
  }
  const list = $('appsList');
  const summary = $('appsSummary');
  if (!list || !summary) return;
  const items = state.apps.items || [];
  if (!state.apps.loaded) {
    summary.textContent = 'Selecciona un dispositivo activo y carga sus apps.';
    list.innerHTML = '<p class="hint">Pulsa “Cargar apps” para ver las apps del Android activo.</p>';
    return;
  }
  summary.textContent = `${items.length} apps · ${appKindLabel(state.apps.kind)}${state.apps.query ? ` · filtro: ${state.apps.query}` : ''}`;
  if (!items.length) {
    list.innerHTML = '<p class="hint">No hay apps para ese filtro o no hay dispositivo activo.</p>';
    return;
  }
  list.innerHTML = items.map(app => {
    const packageName = app.package || '';
    const kind = app.kind === 'system' ? 'Sistema' : 'Usuario';
    const label = app.label || app.name || packageName;
    const packageLine = label === packageName ? packageName : packageName;
    return `
      <div class="app-card">
        <div class="app-info">
          <h4>${escapeHtml(label)}</h4>
          <p>${escapeHtml(kind)} · <span class="package-name">${escapeHtml(packageLine)}</span>${app.apk_path ? ` · ${escapeHtml(app.apk_path)}` : ''}</p>
        </div>
        <div class="app-actions">
          <button type="button" class="primary" data-app-open="${escapeHtml(packageName)}">Abrir</button>
          <button type="button" class="secondary" data-app-stop="${escapeHtml(packageName)}">Cerrar</button>
          <button type="button" class="secondary" data-app-kill="${escapeHtml(packageName)}">Kill</button>
          <button type="button" class="secondary" data-app-path="${escapeHtml(packageName)}">Ruta APK</button>
          <button type="button" class="secondary" data-app-copy="${escapeHtml(packageName)}">Copiar paquete</button>
          <button type="button" class="secondary" data-app-pull="${escapeHtml(packageName)}">Pull APK</button>
          <button type="button" class="danger-soft" data-app-cache="${escapeHtml(packageName)}">Borrar caché</button>
          <button type="button" class="danger-soft" data-app-clear="${escapeHtml(packageName)}">Borrar datos</button>
          <button type="button" class="danger-soft" data-app-uninstall="${escapeHtml(packageName)}">Desinstalar</button>
        </div>
      </div>`;
  }).join('');
}

async function loadApps(button = $('appsRefresh')) {
  const kind = $('appsKind')?.value || 'user';
  const query = $('appsSearch')?.value || '';
  await runAction(button, async () => {
    const data = await api(`/api/apps?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(query)}`);
    renderApps(data);
    return {...data, message: data.ok ? `${data.count || 0} apps cargadas.` : data.message};
  }, {tab: 'apps'});
}

function appEndpoint(packageName, action) {
  return `/api/apps/${encodeURIComponent(packageName)}/${action}`;
}

async function runAppPost(button, packageName, action, label, reload = false) {
  return runAction(button, async () => {
    const data = await api(appEndpoint(packageName, action), {method: 'POST'});
    if (reload && data.ok) setTimeout(() => loadApps($('appsRefresh')), 200);
    return data;
  }, {tab: 'apps'});
}

function networkUiState(item) {
  if (item.ui_state) return item.ui_state;
  if (item.active) return 'active';
  if (item.adb_connected) return 'connected';
  if (item.adb_port_open) return 'open';
  if ((item.profile_names || []).length) return 'profile';
  return 'detected';
}

function networkCardClass(item) {
  const classes = ['network-card', `state-${networkUiState(item)}`];
  return classes.join(' ');
}

function networkStatusLine(item) {
  const profileText = (item.profile_names || []).length ? ` · Perfil: ${(item.profile_names || []).join(', ')}` : '';
  const uiState = networkUiState(item);
  if (uiState === 'active') return `Activo${profileText}`;
  if (uiState === 'connected') return `ADB conectado${profileText}`;
  if (uiState === 'open') return `ADB abierto${profileText}`;
  if (uiState === 'profile') return `Perfil: ${(item.profile_names || []).join(', ')}`;
  return 'Detectado';
}

function renderNetwork(devices = [], meta = {}) {
  const container = $('networkList');
  const summary = $('networkSummary');
  const connected = devices.filter(d => d.adb_connected).length;
  const open = devices.filter(d => d.adb_port_open && !d.adb_connected).length;
  const active = devices.filter(d => d.active).length;
  const range = meta.range || meta.defaultNetwork || '';
  summary.textContent = devices.length
    ? `${devices.length} detectados · ${active} activo · ${connected} ADB conectado · ${open} ADB abierto${range ? ` · ${range}` : ''}`
    : `Sin escaneo cargado${range ? ` · auto: ${range}` : ''}`;

  if (!devices.length) {
    container.innerHTML = '<p class="hint">Abre ADB Agent en el PC cliente y pulsa Escanear red. En modo local se escanea la red del servidor.</p>';
    return;
  }

  container.innerHTML = devices.map(item => {
    const bits = [];
    if (item.mac) bits.push(item.mac);
    if (item.vendor) bits.push(item.vendor);
    if (item.source) bits.push(item.source);
    if (item.adb_status) bits.push(`ADB ${item.adb_status}`);
    return `
      <div class="${networkCardClass(item)}">
        <div class="network-main">
          <div>
            <h4>${escapeHtml(item.ip)}</h4>
            <p>${escapeHtml(networkStatusLine(item))}${bits.length ? ` · ${escapeHtml(bits.join(' · '))}` : ''}</p>
          </div>
        </div>
        <div class="item-actions">
          <button type="button" class="primary" data-network-connect="${escapeHtml(item.ip)}">Conectar ADB</button>
          <button type="button" class="secondary" data-network-profile="${escapeHtml(item.ip)}" data-network-mac="${escapeHtml(item.mac || '')}">Crear perfil</button>
        </div>
      </div>`;
  }).join('');
}

async function refreshAll() {
  try {
    const status = await api('/api/status');
    state.status = status;
    renderHealth(Boolean(status.ok));
    renderActive(status);
    renderProfiles(status);
    renderDevices(status);
    renderScreen(status);
    renderCommands(status);
    renderApps();
    renderScripts();
    if (status.client_agents) renderClientAgents(status.client_agents);
  } catch (error) {
    renderHealth(false);
  }
}

async function refreshNetworkLast() {
  const data = await api('/api/network/last');
  renderNetwork(data.devices || [], {
    range: data.last_network_range || data.default_network || '',
    defaultNetwork: data.default_network || '',
  });
}

async function scanNetwork(button = $('scanNetwork')) {
  await runAction(button, async () => {
    const data = await api('/api/network/scan', {
      method: 'POST',
      body: {network: '', port: 5555},
    });
    renderNetwork(data.devices || [], {range: data.network || ''});
    return {...data, message: data.ok ? `Escaneo terminado: ${(data.devices || []).length} dispositivos.` : data.message};
  }, {tab: 'network'});
}

function fillProfileForm(profile) {
  $('profileName').value = profile.name || '';
  $('profileIp').value = profile.ip || '';
  $('profilePort').value = profile.port || 5555;
  $('profileMac').value = profile.mac || '';
  $('profileNotes').value = profile.notes || '';
  $('profileForm').dataset.editId = profile.id || '';
  $('profileForm').querySelector('button[type="submit"]').textContent = 'Guardar cambios';
  appendTabLog('dashboard', `Editando perfil: ${profile.name || profile.id}.`, true);
  $('profileName').focus();
}

function clearProfileForm() {
  $('profileForm').reset();
  $('profilePort').value = 5555;
  $('profileForm').dataset.editId = '';
  $('profileForm').querySelector('button[type="submit"]').textContent = 'Guardar perfil';
}


function selectedFastbootSerial() {
  return $('fastbootSerial')?.value || '';
}

function setFastbootOutput(text) {
  const box = $('fastbootOutput');
  if (box) box.textContent = cleanMessage(text) || 'Sin salida.';
}

function renderFastboot(data) {
  const devices = data.devices || [];
  state.fastboot.devices = devices;
  state.fastboot.loaded = true;

  const select = $('fastbootSerial');
  if (select) {
    const old = select.value;
    select.innerHTML = '';
    if (!devices.length) {
      select.innerHTML = '<option value="">Sin dispositivos fastboot</option>';
    } else {
      devices.forEach(device => {
        if (!device.serial) return;
        const option = document.createElement('option');
        option.value = device.serial;
        option.textContent = `${device.serial} · ${device.status || 'fastboot'}`;
        select.appendChild(option);
      });
      if (old && devices.some(d => d.serial === old)) select.value = old;
    }
  }

  const list = $('fastbootDevices');
  if (list) {
    if (!devices.length) {
      list.innerHTML = `<p class="hint">No hay dispositivos fastboot. Puedes pulsar “ADB → Bootloader” con un Android activo.</p>`;
    } else {
      list.innerHTML = devices.map(d => `
        <div class="device-row">
          <div>
            <strong>${escapeHtml(d.serial || 'Sin serial')}</strong>
            <span>${escapeHtml(d.status || '')}</span>
            <small>${escapeHtml(d.detail || '')}</small>
          </div>
          ${d.serial ? `<button type="button" class="mini" data-fastboot-copy="${escapeHtml(d.serial)}">Copiar serial</button>` : ''}
        </div>
      `).join('');
    }
  }

  if (data.version) appendTabLog('fastboot', data.version, true);
}

async function refreshFastboot(button) {
  setBusy(button, true);
  try {
    const data = await api('/api/fastboot/status');
    renderFastboot(data);
    appendTabLog('fastboot', data.message || 'Fastboot actualizado.', Boolean(data.ok));
    return data;
  } finally {
    setBusy(button, false);
  }
}

function fastbootPayload(extra = {}) {
  return {serial: selectedFastbootSerial(), ...extra};
}

async function fastbootPost(button, endpoint, body = {}) {
  const result = await runAction(button, () => api(endpoint, {method: 'POST', body: fastbootPayload(body)}), {tab: 'fastboot', output: 'fastbootOutput'});
  if (result?.devices) renderFastboot(result);
  return result;
}

function uploadFastbootImage(button, endpoint, file, extra = {}) {
  const data = new FormData();
  data.append('serial', selectedFastbootSerial());
  data.append('image', file);
  Object.entries(extra).forEach(([key, value]) => data.append(key, value));
  return runAction(button, () => uploadApi(endpoint, data), {tab: 'fastboot', output: 'fastbootOutput'})
    .then(result => {
      if (result?.devices) renderFastboot(result);
      return result;
    });
}

function setFastbootSlot(button, slot) {
  const confirmText = prompt(`Para activar slot ${slot.toUpperCase()} escribe: SLOT ${slot.toUpperCase()}`);
  if (confirmText !== `SLOT ${slot.toUpperCase()}`) {
    appendTabLog('fastboot', 'Cambio de slot cancelado.', false);
    return;
  }
  fastbootPost(button, '/api/fastboot/set-active', {slot, confirm: confirmText});
}



const LOG_WIDTH_KEY = '9adb.logWidth';

function clampLogWidth(value) {
  const width = Number(value);
  if (!Number.isFinite(width)) return 470;
  return Math.max(240, Math.min(760, Math.round(width)));
}

function applyLogWidth(width) {
  const value = clampLogWidth(width);
  document.documentElement.style.setProperty('--log-width', `${value}px`);
  return value;
}

function loadLogWidth() {
  const saved = localStorage.getItem(LOG_WIDTH_KEY);
  if (saved) applyLogWidth(saved);
}

function bindLogResizers() {
  let dragging = null;

  document.querySelectorAll('[data-log-resizer]').forEach(handle => {
    handle.addEventListener('dblclick', () => {
      localStorage.removeItem(LOG_WIDTH_KEY);
      applyLogWidth(470);
    });

    handle.addEventListener('pointerdown', event => {
      if (window.matchMedia('(max-width: 920px)').matches) return;
      const layout = handle.closest('.tab-layout');
      if (!layout) return;
      const rect = layout.getBoundingClientRect();
      const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--log-width')) || 470;
      dragging = {
        pointerId: event.pointerId,
        layoutLeft: rect.left,
        layoutRight: rect.right,
        startX: event.clientX,
        startWidth: current,
      };
      handle.setPointerCapture?.(event.pointerId);
      document.body.classList.add('resizing-log');
      event.preventDefault();
    });
  });

  window.addEventListener('pointermove', event => {
    if (!dragging) return;
    const newWidth = dragging.layoutRight - event.clientX;
    const applied = applyLogWidth(newWidth);
    localStorage.setItem(LOG_WIDTH_KEY, String(applied));
  });

  window.addEventListener('pointerup', event => {
    if (!dragging) return;
    document.body.classList.remove('resizing-log');
    dragging = null;
  });

  window.addEventListener('pointercancel', () => {
    document.body.classList.remove('resizing-log');
    dragging = null;
  });
}



function renderClientAgents(agents = []) {
  state.clientAgents.items = agents;
  state.clientAgents.loaded = true;
  const container = $('clientAgentsList');
  if (!container) return;

  if (!agents.length) {
    container.innerHTML = '<p class="hint">No hay ningún agente conectado. Descarga el .exe, ábrelo y pulsa “Iniciar agente”.</p>';
    return;
  }

  container.innerHTML = agents.map(agent => {
    const devices = agent.devices || [];
    const status = agent.online ? 'online' : 'offline';
    const activeAgentId = state.status?.state?.active_client_agent_id || '';
    const selected = activeAgentId && activeAgentId === agent.id;
    const deviceHtml = devices.length ? devices.map(device => {
      return `
        <div class="client-device-row">
          <div>
            <strong>${escapeHtml(device.name || device.serial || `${device.ip}:${device.port}`)}</strong>
            <span>${escapeHtml(device.ip || '')}:${escapeHtml(String(device.port || 5555))}</span>
            <small>${device.tunnel_port ? `Túnel listo: 127.0.0.1:${escapeHtml(String(device.tunnel_port))}` : escapeHtml(device.status || 'detectado')}</small>
          </div>
        </div>
      `;
    }).join('') : '<p class="hint">Agente online, pero sin Androids detectados todavía.</p>';

    return `
      <article class="mini-card client-agent ${agent.online ? 'online' : 'offline'} ${selected ? 'selected' : ''}">
        <div class="agent-head">
          <div>
            <h4>${escapeHtml(agent.name || agent.id)} ${selected ? '· en uso' : ''}</h4>
            <p>${escapeHtml(agent.local_ip || '')} · ${escapeHtml(agent.network || '')}</p>
            <small>${escapeHtml(agent.platform || '')}</small>
          </div>
          <div class="agent-actions">
            <span class="pill ${agent.online ? 'ok' : 'bad'}">${status}</span>
            ${agent.online && !selected ? `<button type="button" class="mini primary" data-client-agent-select="${escapeHtml(agent.id)}">Usar este agente</button>` : ''}
            <button type="button" class="mini danger-soft" data-client-agent-remove="${escapeHtml(agent.id)}">Quitar</button>
          </div>
        </div>
        <div class="client-devices">${deviceHtml}</div>
        <div class="agent-foot">
          <small>Última señal: ${escapeHtml(agent.last_seen || '')}</small>
        </div>
      </article>
    `;
  }).join('');
}

async function refreshClientAgents(button) {
  setBusy(button, true);
  try {
    const data = await api('/api/client-agents');
    renderClientAgents(data.agents || []);
    appendTabLog('agent', data.agents?.length ? `Agentes cliente: ${data.agents.length}` : 'No hay agentes cliente.', Boolean(data.ok));
    return data;
  } finally {
    setBusy(button, false);
  }
}


function bindEvents() {
  $('clientAgentsRefresh')?.addEventListener('click', event => refreshClientAgents(event.currentTarget));
  loadLogWidth();
  bindLogResizers();
  document.querySelectorAll('.tab-button').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));
  $('collapseButton').addEventListener('click', () => $('appShell').classList.toggle('collapsed'));

  $('globalRefresh').addEventListener('click', event => runAction(event.currentTarget, async () => {
    await refreshAll();
    await refreshNetworkLast();
    return {ok: true, message: 'Estado actualizado.'};
  }, {tab: state.currentTab}));

  $('manualConnect').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/connect/manual', {
    method: 'POST',
    body: {host: $('manualHost').value, port: $('manualPort').value, activate: true},
  }), {tab: 'dashboard'}));

  $('prepareWifi').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/wifi/prepare', {
    method: 'POST',
    body: {port: $('tcpipPort').value},
  }), {tab: 'dashboard'}));

  $('pairButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/wifi/pair', {
    method: 'POST',
    body: {host: $('pairHost').value, port: $('pairPort').value, code: $('pairCode').value},
  }), {tab: 'dashboard'}));

  $('disconnectActive').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/disconnect', {method: 'POST', body: {}}), {tab: 'dashboard'}));
  $('restartAdb').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/adb/restart', {method: 'POST'}), {tab: 'dashboard'}));
  $('cancelProfileEdit').addEventListener('click', () => clearProfileForm());

  $('profileForm').addEventListener('submit', event => {
    event.preventDefault();
    const editId = $('profileForm').dataset.editId;
    const body = {
      name: $('profileName').value,
      ip: $('profileIp').value,
      port: $('profilePort').value,
      mac: $('profileMac').value,
      notes: $('profileNotes').value,
    };
    runAction(event.submitter || $('profileForm').querySelector('button[type="submit"]'), async () => {
      const result = await api(editId ? `/api/profiles/${encodeURIComponent(editId)}` : '/api/profiles', {
        method: editId ? 'POST' : 'POST',
        body,
      });
      if (result.ok) clearProfileForm();
      return result;
    }, {tab: 'dashboard'});
  });

  $('scanNetwork').addEventListener('click', event => scanNetwork(event.currentTarget));

  $('appsRefresh').addEventListener('click', event => loadApps(event.currentTarget));
  $('appsKind').addEventListener('change', () => loadApps($('appsRefresh')));
  $('appsSearch').addEventListener('keydown', event => {
    if (event.key === 'Enter') loadApps($('appsRefresh'));
  });

  $('scrcpyStartButton')?.addEventListener('click', event => runAction(event.currentTarget, () => api('/api/scrcpy/start', {method: 'POST'}), {tab: 'screen'}));
  $('scrcpyStopButton')?.addEventListener('click', event => runAction(event.currentTarget, () => api('/api/scrcpy/stop', {method: 'POST'}), {tab: 'screen'}));
  $('scrcpyOpenButton')?.addEventListener('click', () => {
    window.open(scrcpyViewerUrl(), '_blank', 'noopener');
  });
  $('scrcpySaveSettings')?.addEventListener('click', event => runAction(event.currentTarget, () => api('/api/scrcpy/settings', {
    method: 'POST',
    body: readScrcpySettingsForm(),
  }), {tab: 'screen'}));

  $('screenLightButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/screen/light/start', {
    method: 'POST',
    body: {interval_ms: selectedScreenInterval()},
  }), {tab: 'screen'}));
  $('screenStopButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/screen/stop', {method: 'POST'}), {tab: 'screen'}));
  $('screenRefreshButton').addEventListener('click', event => {
    appendTabLog('screen', 'Actualizando captura manual.', true);
    updateScreenFrame(true);
  });
  $('screenInterval').addEventListener('change', () => {
    appendTabLog('screen', `Refresco cambiado a ${selectedScreenInterval()} ms.`, true);
    if (screenIsActive()) {
      api('/api/screen/light/start', {method: 'POST', body: {interval_ms: selectedScreenInterval()}}).then(result => {
        appendTabLog('screen', result.message || 'Refresco actualizado.', Boolean(result.ok));
        refreshAll();
      });
    } else {
      syncScreenLoop();
    }
  });


  $('filesRefresh').addEventListener('click', event => refreshFiles(event.currentTarget));
  $('androidGoButton').addEventListener('click', event => {
    state.files.androidPath = $('androidPathInput').value || '/sdcard';
    refreshAndroidFiles(event.currentTarget);
  });
  $('androidPathInput').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      state.files.androidPath = $('androidPathInput').value || '/sdcard';
      refreshAndroidFiles($('androidGoButton'));
    }
  });
  $('androidUpButton').addEventListener('click', event => {
    state.files.androidPath = state.files.androidParent || '/sdcard';
    refreshAndroidFiles(event.currentTarget);
  });
  $('androidMkdirButton').addEventListener('click', event => {
    const name = prompt('Nombre de la carpeta Android:', 'Nueva_carpeta');
    if (!name) return;
    runAction(event.currentTarget, () => api('/api/files/android/mkdir', {method: 'POST', body: {path: state.files.androidPath, name}}), {tab: 'files'}).then(() => refreshAndroidFiles());
  });
  $('androidUploadButton').addEventListener('click', () => $('androidUploadFile').click());
  $('androidUploadFile').addEventListener('change', event => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    uploadAndroidFile($('androidUploadButton'), file);
    event.currentTarget.value = '';
  });

  $('scriptsRefresh').addEventListener('click', event => loadScripts(event.currentTarget));
  $('scriptNewButton').addEventListener('click', () => clearScriptForm());
  $('scriptCancelButton').addEventListener('click', () => clearScriptForm());
  $('scriptForm').addEventListener('submit', event => {
    event.preventDefault();
    saveScript(event.submitter || $('scriptSaveButton'));
  });

  $('infoButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/info', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('logcatButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/logcat', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('screenshotButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/screenshot', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('screenshotPullButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/screenshot-pull', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('screenrecordStartButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/screenrecord/start', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('screenrecordStopOnlyButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/screenrecord/stop-only', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('screenrecordStopButton').addEventListener('click', event => runAction(event.currentTarget, () => api('/api/commands/screenrecord/stop', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'}));
  $('installApkButton').addEventListener('click', () => $('apkFile').click());
  $('apkFile').addEventListener('change', event => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    const data = new FormData();
    data.append('apk', file);
    runAction($('installApkButton'), () => uploadApi('/api/commands/install-apk', data), {tab: 'commands', output: 'commandOutput'});
    event.currentTarget.value = '';
  });
  $('wallpaperButton').addEventListener('click', () => $('wallpaperFile').click());
  $('wallpaperFile').addEventListener('change', event => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    const data = new FormData();
    data.append('image', file);
    runAction($('wallpaperButton'), () => uploadApi('/api/commands/wallpaper', data), {tab: 'commands', output: 'commandOutput'});
    event.currentTarget.value = '';
  });
  $('openLinkButton').addEventListener('click', event => runOpenLink(event.currentTarget));
  $('rebootButton').addEventListener('click', event => {
    if (!confirm('¿Seguro que quieres reiniciar el Android activo?')) return;
    runAction(event.currentTarget, () => api('/api/commands/reboot', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'});
  });
  $('clearAllCacheButton').addEventListener('click', event => {
    if (!confirm('¿Borrar la caché global de todas las apps si Android lo permite? No debería borrar datos.')) return;
    runAction(event.currentTarget, () => api('/api/commands/clear-all-cache', {method: 'POST'}), {tab: 'commands', output: 'commandOutput'});
    setTimeout(async () => {
      const data = await api('/api/commands/clear-all-cache/status');
      if (data?.job?.message) appendTabLog('commands', data.job.message, true);
    }, 1800);
  });


  $('fastbootRefresh')?.addEventListener('click', event => refreshFastboot(event.currentTarget));
  $('fastbootRebootBootloader')?.addEventListener('click', event => {
    if (!confirm('¿Reiniciar el Android activo a bootloader/fastboot?')) return;
    fastbootPost(event.currentTarget, '/api/fastboot/reboot-bootloader');
    setTimeout(() => refreshFastboot($('fastbootRefresh')), 5000);
  });
  $('fastbootRebootSystem')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/reboot-system'));
  $('fastbootRebootBootloader2')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/reboot-bootloader-fastboot'));
  $('fastbootContinue')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/continue'));
  $('fastbootGetAll')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/getvar', {var: 'all'}));
  $('fastbootGetVar')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/getvar', {var: $('fastbootVar').value || 'all'}));
  $('fastbootOemInfo')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/oem-info'));
  $('fastbootSlotA')?.addEventListener('click', event => setFastbootSlot(event.currentTarget, 'a'));
  $('fastbootSlotB')?.addEventListener('click', event => setFastbootSlot(event.currentTarget, 'b'));

  $('fastbootBootImageButton')?.addEventListener('click', () => $('fastbootBootImageFile').click());
  $('fastbootBootImageFile')?.addEventListener('change', event => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    uploadFastbootImage($('fastbootBootImageButton'), '/api/fastboot/boot-image', file);
    event.currentTarget.value = '';
  });

  $('fastbootFlashImageButton')?.addEventListener('click', () => $('fastbootFlashImageFile').click());
  $('fastbootFlashImageFile')?.addEventListener('change', event => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    const partition = $('fastbootFlashPartition').value;
    const confirmText = $('fastbootFlashConfirm').value;
    if (confirmText !== 'FLASH') {
      appendTabLog('fastboot', 'Para flashear escribe FLASH.', false);
      event.currentTarget.value = '';
      return;
    }
    if (!confirm(`¿Flashear ${partition} con ${file.name}?`)) {
      event.currentTarget.value = '';
      return;
    }
    uploadFastbootImage($('fastbootFlashImageButton'), '/api/fastboot/flash-image', file, {partition, confirm: confirmText});
    event.currentTarget.value = '';
  });

  $('fastbootCustomRun')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/custom', {
    args: $('fastbootCustomArgs').value,
    confirm: $('fastbootCustomConfirm').value,
  }));

  $('fastbootUnlock')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/danger', {
    action: 'flashing_unlock',
    confirm: $('fastbootDangerConfirm').value,
  }));
  $('fastbootLock')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/danger', {
    action: 'flashing_lock',
    confirm: $('fastbootDangerConfirm').value,
  }));
  $('fastbootOemUnlock')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/danger', {
    action: 'oem_unlock',
    confirm: $('fastbootDangerConfirm').value,
  }));
  $('fastbootEraseButton')?.addEventListener('click', event => fastbootPost(event.currentTarget, '/api/fastboot/erase', {
    partition: $('fastbootErasePartition').value,
    confirm: $('fastbootEraseConfirm').value,
  }));


  document.addEventListener('click', event => {

    const fastbootCopy = event.target.closest('[data-fastboot-copy]');
    if (fastbootCopy) {
      navigator.clipboard?.writeText(fastbootCopy.dataset.fastbootCopy || '').then(() => {
        appendTabLog('fastboot', `Serial copiado: ${fastbootCopy.dataset.fastbootCopy}`, true);
      }).catch(() => appendTabLog('fastboot', `Serial: ${fastbootCopy.dataset.fastbootCopy}`, true));
      return;
    }

    const profileConnect = event.target.closest('[data-profile-connect]');
    if (profileConnect) {
      const id = profileConnect.dataset.profileConnect;
      runAction(profileConnect, () => api(`/api/profiles/${encodeURIComponent(id)}/connect`, {method: 'POST'}), {tab: 'dashboard'});
      return;
    }

    const profileEdit = event.target.closest('[data-profile-edit]');
    if (profileEdit) {
      const id = profileEdit.dataset.profileEdit;
      const profile = state.status?.profiles?.[id];
      if (profile) fillProfileForm(profile);
      return;
    }

    const profileDelete = event.target.closest('[data-profile-delete]');
    if (profileDelete) {
      const id = profileDelete.dataset.profileDelete;
      const profile = state.status?.profiles?.[id];
      if (!confirm(`¿Borrar perfil ${profile?.name || id}?`)) return;
      runAction(profileDelete, () => api(`/api/profiles/${encodeURIComponent(id)}/delete`, {method: 'POST'}), {tab: 'dashboard'});
      return;
    }

    const deviceActive = event.target.closest('[data-device-active]');
    if (deviceActive) {
      const serial = deviceActive.dataset.deviceActive;
      runAction(deviceActive, () => api('/api/active', {method: 'POST', body: {serial}}), {tab: 'network'});
      return;
    }

    const deviceDisconnect = event.target.closest('[data-device-disconnect]');
    if (deviceDisconnect) {
      const serial = deviceDisconnect.dataset.deviceDisconnect;
      runAction(deviceDisconnect, () => api('/api/disconnect', {method: 'POST', body: {serial}}), {tab: 'network'});
      return;
    }

    const recentConnect = event.target.closest('[data-recent-connect]');
    if (recentConnect) {
      const serial = recentConnect.dataset.recentConnect;
      runAction(recentConnect, () => api('/api/recent/connect', {method: 'POST', body: {serial}}), {tab: 'network'});
      return;
    }

    const recentRemove = event.target.closest('[data-recent-remove]');
    if (recentRemove) {
      const serial = recentRemove.dataset.recentRemove;
      runAction(recentRemove, () => api('/api/recent/remove', {method: 'POST', body: {serial}}), {tab: 'network'});
      return;
    }

    const netConnect = event.target.closest('[data-network-connect]');
    if (netConnect) {
      const ip = netConnect.dataset.networkConnect;
      runAction(netConnect, () => api('/api/network/connect', {method: 'POST', body: {ip, port: 5555}}), {tab: 'network'});
      return;
    }

    const netProfile = event.target.closest('[data-network-profile]');
    if (netProfile) {
      const ip = netProfile.dataset.networkProfile;
      const mac = netProfile.dataset.networkMac || '';
      const name = prompt('Nombre del perfil:', `Android ${ip}`);
      if (!name) return;
      runAction(netProfile, () => api('/api/network/profile', {method: 'POST', body: {ip, mac, name}}), {tab: 'network'});
      return;
    }


    const androidShortcut = event.target.closest('[data-android-shortcut]');
    if (androidShortcut) {
      state.files.androidPath = androidShortcut.dataset.androidShortcut || '/sdcard';
      refreshAndroidFiles(androidShortcut);
      return;
    }

    const androidOpen = event.target.closest('[data-android-open]');
    if (androidOpen) {
      const path = androidOpen.dataset.androidOpen;
      if (androidOpen.dataset.isDir === '1') {
        state.files.androidPath = path;
        refreshAndroidFiles(androidOpen);
      } else {
        downloadAndroidPath(androidOpen, path);
      }
      return;
    }

    const androidDownload = event.target.closest('[data-android-download]');
    if (androidDownload) {
      downloadAndroidPath(androidDownload, androidDownload.dataset.androidDownload);
      return;
    }

    const androidDelete = event.target.closest('[data-android-delete]');
    if (androidDelete) {
      const path = androidDelete.dataset.androidDelete;
      if (!confirm(`¿Borrar en Android?\n${path}`)) return;
      runAction(androidDelete, () => api('/api/files/android/delete', {method: 'POST', body: {path}}), {tab: 'files'}).then(() => refreshAndroidFiles());
      return;
    }


    const scriptRun = event.target.closest('[data-script-run]');
    if (scriptRun) {
      const id = scriptRun.dataset.scriptRun;
      runAction(scriptRun, () => api(`/api/scripts/${encodeURIComponent(id)}/run`, {method: 'POST'}), {tab: 'scripts'});
      return;
    }

    const scriptEdit = event.target.closest('[data-script-edit]');
    if (scriptEdit) {
      const script = findScript(scriptEdit.dataset.scriptEdit);
      if (script) fillScriptForm(script);
      return;
    }

    const scriptDuplicate = event.target.closest('[data-script-duplicate]');
    if (scriptDuplicate) {
      const id = scriptDuplicate.dataset.scriptDuplicate;
      runAction(scriptDuplicate, async () => {
        const data = await api(`/api/scripts/${encodeURIComponent(id)}/duplicate`, {method: 'POST'});
        if (data.scripts) renderScripts(data);
        return data;
      }, {tab: 'scripts'});
      return;
    }

    const scriptDelete = event.target.closest('[data-script-delete]');
    if (scriptDelete) {
      const id = scriptDelete.dataset.scriptDelete;
      const script = findScript(id);
      if (!confirm(`¿Borrar script ${script?.name || id}?`)) return;
      runAction(scriptDelete, async () => {
        const data = await api(`/api/scripts/${encodeURIComponent(id)}/delete`, {method: 'POST'});
        if (data.scripts) renderScripts(data);
        return data;
      }, {tab: 'scripts'});
      return;
    }


    const appOpen = event.target.closest('[data-app-open]');
    if (appOpen) {
      runAppPost(appOpen, appOpen.dataset.appOpen, 'open', 'Abrir app');
      return;
    }

    const appStop = event.target.closest('[data-app-stop]');
    if (appStop) {
      runAppPost(appStop, appStop.dataset.appStop, 'stop', 'Cerrar app');
      return;
    }

    const appKill = event.target.closest('[data-app-kill]');
    if (appKill) {
      runAppPost(appKill, appKill.dataset.appKill, 'kill', 'Kill app');
      return;
    }

    const appPath = event.target.closest('[data-app-path]');
    if (appPath) {
      const packageName = appPath.dataset.appPath;
      runAction(appPath, () => api(appEndpoint(packageName, 'path'), {method: 'GET'}), {tab: 'apps'});
      return;
    }

    const appCopy = event.target.closest('[data-app-copy]');
    if (appCopy) {
      const packageName = appCopy.dataset.appCopy;
      navigator.clipboard?.writeText(packageName).then(() => {
        appendTabLog('apps', `Paquete copiado: ${packageName}`, true);
      }).catch(() => {
        appendTabLog('apps', `No se pudo copiar automáticamente. Paquete: ${packageName}`, false);
      });
      return;
    }

    const appPull = event.target.closest('[data-app-pull]');
    if (appPull) {
      runAppPost(appPull, appPull.dataset.appPull, 'pull-apk', 'Pull APK');
      return;
    }

    const appCache = event.target.closest('[data-app-cache]');
    if (appCache) {
      const packageName = appCache.dataset.appCache;
      if (!confirm(`¿Borrar caché de ${packageName}? No debería borrar tus datos.`)) return;
      runAppPost(appCache, packageName, 'cache', 'Borrar caché');
      return;
    }

    const appClear = event.target.closest('[data-app-clear]');
    if (appClear) {
      const packageName = appClear.dataset.appClear;
      if (!confirm(`¿Borrar datos de ${packageName}?`)) return;
      runAppPost(appClear, packageName, 'clear', 'Borrar datos');
      return;
    }

    const appUninstall = event.target.closest('[data-app-uninstall]');
    if (appUninstall) {
      const packageName = appUninstall.dataset.appUninstall;
      if (!confirm(`¿Desinstalar ${packageName}?`)) return;
      runAppPost(appUninstall, packageName, 'uninstall', 'Desinstalar app', true);
      return;
    }

    const command = event.target.closest('[data-command]');
    if (command) {
      const id = command.dataset.command;
      runAction(command, () => api(`/api/commands/${encodeURIComponent(id)}`, {method: 'POST'}), {tab: 'commands', output: 'commandOutput'});
    }
  });
}

async function boot() {
  initLogs();
  bindEvents();
  await refreshAll();
  setInterval(refreshAll, 12000);
}

boot();
