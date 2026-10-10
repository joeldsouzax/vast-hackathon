'use strict';
// Remove credentials saved by the previous build.
sessionStorage.removeItem('breadcast-operator-key');
sessionStorage.removeItem('breadcast-viewer-key');
if (location.hash) history.replaceState(null, '', location.pathname + location.search);
const publicPrefix = document.querySelector('meta[name="breadcast-prefix"]')?.content || '';
const operatorAuthMode = document.querySelector('meta[name="breadcast-operator-auth"]')?.content || 'local';
const isOperatorPage = document.querySelector('.studio-header[data-page="studio"]') !== null;
let operatorToken = '', operatorAuthorized = operatorAuthMode !== 'token', operatorSessionRevision = 0;
function publicPath(path) {
  if (!path.startsWith('/') || path.startsWith('//')) return path;
  if (!publicPrefix || path === publicPrefix || path.startsWith(publicPrefix + '/')) return path;
  return publicPrefix + path;
}
function requireOperatorLogin() {
  operatorToken = ''; operatorAuthorized = false; ++operatorSessionRevision;
  const form = document.querySelector('#operator-login');
  if (form) form.hidden = false;
  const workspace = document.querySelector('#workspace');
  if (workspace) workspace.hidden = true;
  document.querySelector('#operator-lock')?.setAttribute('hidden', '');
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  clearProtectedAssets();
  window.dispatchEvent(new Event('breadcast-operator-locked'));
}
async function operatorFetch(path, options = {}, token = undefined) {
  const url = new URL(publicPath(path), location.origin);
  // Credentials must never follow a provider-supplied or external URL.
  if (url.origin !== location.origin) throw new Error('Cross-origin API request rejected');
  const headers = new Headers(options.headers);
  const credential = token === undefined ? (isOperatorPage ? operatorToken : '') : token;
  if (credential) headers.set('Authorization', `Bearer ${credential}`);
  const revision = operatorSessionRevision;
  const response = await fetch(url.href, {...options, headers, redirect: 'error'});
  if (response.status === 403 && isOperatorPage && operatorAuthMode === 'token' && token === undefined && revision === operatorSessionRevision) requireOperatorLogin();
  return response;
}
async function api(path, body, token = undefined) {
  const revision = operatorSessionRevision;
  const response = await operatorFetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)}, token);
  const result = await response.json();
  if (isOperatorPage && revision !== operatorSessionRevision) {
    const error = new Error('Studio locked. Enter the operator credential.'); error.status = 403; throw error;
  }
  if (!response.ok) {
    const error = new Error(result.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return result;
}
const protectedAssets = new Map();
async function loadProtectedAsset(element, path) {
  if (element.dataset.loadingAsset) return;
  if (isOperatorPage && !operatorAuthorized) return;
  const revision = operatorSessionRevision;
  element.dataset.loadingAsset = 'true';
  try {
    const response = await operatorFetch(path);
    if (!response.ok) throw new Error(`Preview unavailable (${response.status})`);
    const blob = await response.blob();
    if (!element.isConnected || (isOperatorPage && (!operatorAuthorized || revision !== operatorSessionRevision))) return;
    const old = protectedAssets.get(element);
    if (old) URL.revokeObjectURL(old);
    const url = URL.createObjectURL(blob);
    protectedAssets.set(element, url); element.src = url;
  } finally {delete element.dataset.loadingAsset;}
}
function clearProtectedAssets() {
  for (const [element, url] of protectedAssets) {
    if (typeof element.pause === 'function') element.pause();
    element.removeAttribute('src');
    if (typeof element.load === 'function') element.load();
    URL.revokeObjectURL(url);
  }
  protectedAssets.clear();
}
// Removed previews release decoded media and their object URLs.
new MutationObserver(() => {
  for (const [element, url] of protectedAssets) {
    if (!element.isConnected) {URL.revokeObjectURL(url); protectedAssets.delete(element);}
  }
}).observe(document.body, {childList: true, subtree: true});
window.addEventListener('pagehide', () => {
  operatorToken = '';
  clearProtectedAssets();
});
const operatorLogin = document.querySelector('#operator-login');
if (operatorLogin) {
  operatorLogin.hidden = operatorAuthMode !== 'token';
  if (!operatorAuthorized) document.querySelector('#workspace').hidden = true;
  operatorLogin.onsubmit = async event => {
    event.preventDefault();
    const field = document.querySelector('#operator-token'), button = operatorLogin.querySelector('button');
    const revision = ++operatorSessionRevision;
    operatorToken = field.value; field.value = ''; button.disabled = true;
    try {
      await api('/api/status');
      if (revision !== operatorSessionRevision) return;
      operatorAuthorized = true; operatorLogin.hidden = true;
      document.querySelector('#operator-login-status').textContent = '';
      document.querySelector('#operator-lock').hidden = false;
      window.dispatchEvent(new Event('breadcast-operator-ready'));
    } catch (error) {
      if (revision === operatorSessionRevision) requireOperatorLogin();
      if (!operatorAuthorized) document.querySelector('#operator-login-status').textContent = error.message;
    } finally {button.disabled = false;}
  };
  document.querySelector('#operator-lock').onclick = requireOperatorLogin;
}
function message(value = '') {
  const element = document.querySelector('#message');
  const modal = [...document.querySelectorAll('dialog[open]')].at(-1);
  if (modal && !modal.contains(element)) (modal.querySelector('.modal-body') || modal).prepend(element);
  if (element.textContent !== value) element.textContent = value;
  if (element.hidden !== !value) element.hidden = !value;
}
async function startProgramPlayback(video) {
  try { await video.play(); }
  catch (error) {
    if (error.name !== 'NotAllowedError' || video.muted) return;
    // Keep the picture moving when the browser requires a gesture for sound.
    video.dataset.soundBlocked = 'true';
    video.muted = true;
    video.dispatchEvent(new Event('volumechange'));
    try { await video.play(); } catch (_) {}
  }
}
function setupProgramAudio(video, button) {
  function update() {
    const blocked = video.muted && video.dataset.soundBlocked === 'true';
    button.replaceChildren(studioIcon(video.muted ? 'volume-x' : 'volume-2'));
    if (blocked) button.append(document.createTextNode('Tap for sound'));
    button.classList.toggle('sound-blocked', blocked);
    button.setAttribute('aria-pressed', String(!video.muted));
    button.setAttribute('aria-label', video.muted ? 'Listen to program audio' : 'Mute local playback');
    button.title = button.dataset.tooltip = blocked ? 'Your browser needs a tap to play sound.' :
      video.muted ? 'Listen in this browser only.' : 'Mute playback in this browser only.';
  }
  video.addEventListener('volumechange', update);
  button.onclick = () => {
    delete video.dataset.soundBlocked;
    video.muted = !video.muted;
    update();
    startProgramPlayback(video);
  };
  update();
}
let hlsLibrary;
function loadHlsLibrary() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLibrary) hlsLibrary = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    // Use the hls.js version bundled in the pinned MediaMTX image.
    script.src = publicPath('/media/program/hls/hls.min.js');
    script.onload = () => resolve(window.Hls);
    script.onerror = () => {script.remove(); hlsLibrary = null; reject(new Error('Cannot load the video player'));};
    document.head.append(script);
  });
  return hlsLibrary;
}
function playHlsProgram(video, error) {
  let closed = false, player, retryTimer, failed = false;
  const revision = operatorSessionRevision;
  const active = () => !closed && (!isOperatorPage || (operatorAuthorized && revision === operatorSessionRevision));
  const url = publicPath('/media/program/hls/index.m3u8');
  function recovered() {if (active() && failed) {failed = false; error('');}}
  function retry() {
    if (!active() || retryTimer) return;
    failed = true; error('Stream unavailable. Retrying…');
    player?.destroy(); player = undefined;
    retryTimer = setTimeout(() => {retryTimer = undefined; start();}, 2000);
  }
  async function start() {
    if (!active()) return;
    try {
      const Hls = await loadHlsLibrary();
      if (!active()) return;
      if (Hls?.isSupported()) {
        player = new Hls({lowLatencyMode: false, maxLiveSyncPlaybackRate: 1.5});
        player.on(Hls.Events.ERROR, (_, data) => {if (data.fatal) retry();});
        player.on(Hls.Events.MEDIA_ATTACHED, () => player?.loadSource(url));
        player.on(Hls.Events.MANIFEST_PARSED, () => {if (active()) startProgramPlayback(video);});
        player.attachMedia(video);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url; startProgramPlayback(video);
      } else {
        error('This browser cannot play the stream. Use a current Chrome or Safari browser.');
      }
    } catch (_) {retry();}
  }
  video.addEventListener('playing', recovered);
  video.addEventListener('error', retry);
  const reader = {close() {
    closed = true; clearTimeout(retryTimer); player?.destroy();
    video.removeEventListener('playing', recovered); video.removeEventListener('error', retry);
    video.removeAttribute('src'); video.load();
  }};
  window.addEventListener('pagehide', () => reader.close(), {once: true});
  start(); return reader;
}
function playProgram(video, error) {
  if (document.querySelector('meta[name="breadcast-viewer-transport"]')?.content === 'hls') {
    return playHlsProgram(video, error);
  }
  const revision = operatorSessionRevision;
  const reader = new MediaMTXWebRTCReader({
    url: `${location.origin}${publicPath('/media/program/whep')}`,
    onError: value => {if (!isOperatorPage || (operatorAuthorized && revision === operatorSessionRevision)) error(value);},
    onTrack: (event) => {if (!isOperatorPage || (operatorAuthorized && revision === operatorSessionRevision)) {
      video.srcObject = event.streams[0]; startProgramPlayback(video);
    }},
  });
  window.addEventListener('pagehide', () => reader.close(), {once: true});
  return reader;
}

// One navigation shell for Broadcast, Studio, and the camera join page.
const pageHeader = document.querySelector('.studio-header[data-page]');
if (pageHeader) {
  const current = pageHeader.dataset.page;
  pageHeader.innerHTML = `<a class="brand" href="${publicPath('/')}" aria-label="Breadcast home"><img src="${publicPath('/brand/breadcast-mark.svg')}" alt=""><span>breadcast<span class="brand-dot">.</span></span></a><nav class="page-tabs" aria-label="Studio pages">${[['broadcast','/','Broadcast'],['studio','/operator','Studio'],['join','/join','Join']].map(([page,href,label]) => `<a href="${publicPath(href)}"${page === current ? ' aria-current="page"' : ''}${page === 'join' ? ' data-join-nav' : ''}>${label}</a>`).join('')}</nav>`;
  for (const [index, link] of [...pageHeader.querySelectorAll('.page-tabs a')].entries()) link.prepend(studioIcon(['radio','clapperboard','camera'][index]));
}
function cameraStateLabel(camera) {
  if (camera.state === 'ACTIVE') return camera.buffer_ready === false ? 'Buffering' : 'Live';
  return {RESERVED: 'Reserved', RECONNECTING: 'Reconnecting', REVOKING: 'Removing'}[camera.state] || 'Unknown';
}
function updateJoinNavigation(url) {
  const link = document.querySelector('[data-join-nav]');
  if (link && url) link.href = url;
}
function studioIcon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('studio-icon'); svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', publicPath(`/vendor/lucide/icons.svg#${name}`)); svg.append(use); return svg;
}
function fitProgramFrame(video, reserve = 0) {
  const main = document.querySelector('.main-surface'), panel = document.querySelector('.program-panel');
  const ratio = video.videoWidth && video.videoHeight ? video.videoWidth / video.videoHeight : 16/9;
  panel.style.setProperty('--program-ratio', ratio);
  if (innerWidth <= 900) {panel.style.removeProperty('--program-width'); return;}
  const width = Math.min(main.clientWidth, Math.max(100, main.clientHeight - reserve) * ratio);
  panel.style.setProperty('--program-width', Math.floor(width) + 'px');
}
function trapDialogFocus(dialog) {
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const items = [...dialog.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,video[controls],[tabindex]:not([tabindex="-1"])')]
      .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    const first = items[0], last = items.at(-1);
    if (first && ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last))) {
      event.preventDefault(); (event.shiftKey ? last : first).focus();
    }
  });
}
