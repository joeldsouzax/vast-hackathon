'use strict';
let reader, qrUrl, qrCode, posterUrl, refreshing = false, connectionFailed = false;
async function loadQR(joinUrl) {
  if (qrCode === joinUrl) return;
  const image = document.querySelector('#qr'), status = document.querySelector('#qr-status');
  image.hidden = true; status.hidden = false; status.textContent = 'Loading join code…';
  let candidate;
  try {
    const response = await fetch('/api/qr');
    if (!response.ok) throw new Error('QR unavailable');
    candidate = URL.createObjectURL(await response.blob()); image.src = candidate; await image.decode();
    if (qrUrl) URL.revokeObjectURL(qrUrl);
    qrUrl = candidate; qrCode = joinUrl; image.hidden = false; status.hidden = true;
  } catch (_) {
    if (candidate) URL.revokeObjectURL(candidate);
    status.textContent = 'Join code unavailable. Retrying…';
  }
}
async function loadPoster() {
  const response = await fetch('/api/preview/program');
  if (!response.ok) return;
  posterUrl = URL.createObjectURL(await response.blob()); document.querySelector('#program').poster = posterUrl;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const state = await api('/api/viewer', undefined);
    if (connectionFailed) { message(); connectionFailed = false; }
    document.querySelector('#broadcast').hidden = false;
    if (!reader) {
      loadPoster().catch(() => {});
      reader = playProgram(document.querySelector('#program'), error => message(`Video connection: ${error}`));
    }
    updateJoinNavigation(state.join_url);
    const joinOrigin = new URL(state.join_url);
    const local = ['localhost', '127.0.0.1', '[::1]'].includes(joinOrigin.hostname);
    document.querySelector('#join-hint').textContent = local
      ? 'Sharing from your phone? Connect this studio to a reachable HTTPS address first.'
      : joinOrigin.protocol === 'http:' ? 'Open the HTTPS demo link to share a phone camera.'
      : 'Scan to join. Allow camera access, then select Start sharing.';
    document.querySelector('#join-link').href = state.join_url; document.querySelector('#join-link').hidden = false;
    await loadQR(state.join_url);
  } catch (error) {
    connectionFailed = true; message('Cannot reach the broadcast. Retrying…');
  } finally {
    refreshing = false;
  }
}
refresh(); setInterval(refresh, 1500);

const viewerVideo = document.querySelector('#program'), viewerFrame = document.querySelector('.monitor-frame');
for (const button of document.querySelectorAll('[data-icon]')) button.append(studioIcon(button.dataset.icon));
document.querySelector('#monitor-audio').onclick = event => {
  viewerVideo.muted = !viewerVideo.muted;
  const button = event.currentTarget;
  button.replaceChildren(studioIcon(viewerVideo.muted ? 'volume-x' : 'volume-2'));
  button.setAttribute('aria-pressed', String(!viewerVideo.muted));
  button.setAttribute('aria-label', viewerVideo.muted ? 'Listen to program audio' : 'Mute local playback');
  button.title = viewerVideo.muted ? 'Listen in this browser only.' : 'Mute playback in this browser only.';
};
document.querySelector('#program-fullscreen').onclick = async () => {
  try {if (document.fullscreenElement) await document.exitFullscreen(); else await viewerFrame.requestFullscreen();} catch (error) {message(error.message);}
};
document.addEventListener('fullscreenchange', () => {
  const full = document.fullscreenElement === viewerFrame, button = document.querySelector('#program-fullscreen');
  button.replaceChildren(studioIcon(full ? 'minimize' : 'maximize'));
  button.setAttribute('aria-label', full ? 'Exit fullscreen' : 'Enter fullscreen');
  button.title = full ? 'Return to the broadcast.' : 'Expand the program monitor.';
});
const viewerSizer = new ResizeObserver(() => fitProgramFrame(viewerVideo));
viewerSizer.observe(document.querySelector('.main-surface'));
viewerVideo.addEventListener('loadedmetadata', () => fitProgramFrame(viewerVideo));
