'use strict';
// Remove credentials saved by the previous build.
sessionStorage.removeItem('breadcast-operator-key');
sessionStorage.removeItem('breadcast-viewer-key');
if (location.hash) history.replaceState(null, '', location.pathname + location.search);
async function api(path, body, token = '') {
  const headers = {'Content-Type': 'application/json'};
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers,
                                    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return result;
}
function message(value = '') {
  const element = document.querySelector('#message');
  const modal = [...document.querySelectorAll('dialog[open]')].at(-1);
  if (modal && !modal.contains(element)) (modal.querySelector('.modal-body') || modal).prepend(element);
  if (element.textContent !== value) element.textContent = value;
  if (element.hidden !== !value) element.hidden = !value;
}
function playProgram(video, error) {
  const reader = new MediaMTXWebRTCReader({
    url: `${location.origin}/media/program/whep`,
    onError: error,
    onTrack: (event) => { video.srcObject = event.streams[0]; },
  });
  window.addEventListener('pagehide', () => reader.close(), {once: true});
  return reader;
}

// One navigation shell for Broadcast, Studio, and the camera join page.
const pageHeader = document.querySelector('.studio-header[data-page]');
if (pageHeader) {
  const current = pageHeader.dataset.page;
  pageHeader.innerHTML = `<a class="brand" href="/" aria-label="Breadcast home"><img src="/brand/breadcast-mark.svg" alt=""><span>breadcast<span class="brand-dot">.</span></span></a><nav class="page-tabs" aria-label="Studio pages">${[['broadcast','/','Broadcast'],['studio','/operator','Studio'],['join','/join','Join']].map(([page,href,label]) => `<a href="${href}"${page === current ? ' aria-current="page"' : ''}${page === 'join' ? ' data-join-nav' : ''}>${label}</a>`).join('')}</nav>`;
  for (const [index, link] of [...pageHeader.querySelectorAll('.page-tabs a')].entries()) link.prepend(studioIcon(['radio','clapperboard','camera'][index]));
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
  use.setAttribute('href', `/vendor/lucide/icons.svg#${name}`); svg.append(use); return svg;
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
