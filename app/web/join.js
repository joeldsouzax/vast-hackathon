'use strict';
let lease, stream, publisher, busy = false, pollingLease = false;
let code = new URLSearchParams(location.search).get('code');
let client = sessionStorage.getItem('breadcast-camera-client');
if (!client) { client = crypto.randomUUID().replaceAll('-', ''); sessionStorage.setItem('breadcast-camera-client', client); }
// A direct Join tab also resolves the current event code without taking a camera slot.
const joinReady = api('/api/viewer').then(state => {
  updateJoinNavigation(state.join_url);
  if (state.event_title) {
    document.querySelector('.join-description').textContent = `Join ${state.event_title}. Joining starts sharing and recording. The first ready camera goes live automatically. Up to ${state.camera_limit} cameras.`;
    document.title = `${state.event_title} · Join camera`;
  }
  if (!code) {
    code = new URL(state.join_url).searchParams.get('code');
    history.replaceState(null, '', publicPath('/join') + '?code=' + encodeURIComponent(code));
  }
}).catch(error => {if (!code) throw error;});
joinReady.catch(() => {});
const joinButton = document.querySelector('#join');
const stopButton = document.querySelector('#stop'), status = document.querySelector('#status');
for (const holder of document.querySelectorAll('[data-icon]')) holder.prepend(studioIcon(holder.dataset.icon));
function sharingState(value) {
  document.body.classList.toggle('camera-sharing', value !== 'idle');
  document.querySelector('.join-flow').dataset.state = value;
  document.querySelector('#preview-container').hidden = value === 'idle';
  document.querySelector('#share-actions').hidden = value === 'idle';
  joinButton.hidden = value !== 'idle';
  document.querySelector('#microphone').disabled = value !== 'idle';
}
async function release() {
  if (publisher) { publisher.close(); publisher = null; }
  if (stream) { stream.getTracks().forEach(track => track.stop()); stream = null; }
  document.querySelector('#preview').srcObject = null;
  sharingState('idle');
  if (lease) {
    const previous = lease; lease = null;
    await api(`/api/lease/${previous.lease_id}/release`, {}, previous.token);
  }
}
joinButton.onclick = async () => {
  if (busy) return;
  busy = true; joinButton.disabled = true; message();
  try {
    await joinReady;
    if (!window.isSecureContext || !navigator.mediaDevices) throw new Error('Camera access needs HTTPS. Use the trusted event URL.');
    lease = await api('/api/leases', {code, client});
    const joiningLease = lease;
    status.textContent = `Camera ${lease.slot} reserved. Allow camera permission.`;
    const acquired = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: {ideal: 'environment'}, width: {ideal: 1280}, height: {ideal: 720}, frameRate: {ideal: 30, max: 30}},
      audio: document.querySelector('#microphone').checked,
    });
    if (lease !== joiningLease) {
      acquired.getTracks().forEach(track => track.stop());
      throw new Error('Your camera slot expired. Select Join camera again.');
    }
    stream = acquired;
    document.querySelector('#preview').srcObject = stream;
    sharingState('connecting');
    stopButton.disabled = false;
    status.textContent = `Camera ${lease.slot} connecting. Sharing and recording start automatically.`;
    startSharing();
  } catch (error) {
    message(error.message);
    try { await release(); } catch (_) {}
    joinButton.disabled = false;
  } finally { busy = false; }
};
function startSharing() {
  if (!lease || !stream) return;
  message();
  const sharingLease = lease;
  publisher = new MediaMTXWebRTCPublisher({
    url: `${location.origin}${publicPath(`/media/${lease.source_path}/whip`)}`, token: lease.token, stream,
    videoCodec: 'h264', videoBitrate: 1500, audioCodec: 'opus', audioBitrate: 64, audioVoice: false,
    onConnected: () => { if (lease !== sharingLease) return; sharingState('sharing'); status.textContent = `Camera ${lease.slot} sharing · Recording automatically`; message(); },
    onError: error => { if (lease !== sharingLease) return; message(`Connection issue: ${error}. Reconnecting while this slot remains valid.`); },
  });
}
stopButton.onclick = async () => {
  try { await release(); status.textContent = 'Camera sharing is off.'; message(); }
  catch (error) { message(error.message); }
  stopButton.disabled = true; joinButton.disabled = false;
};
setInterval(async () => {
  if (!lease || pollingLease) return;
  const checkedLease = lease;
  pollingLease = true;
  try {
    const state = await api(`/api/lease/${checkedLease.lease_id}`, undefined, checkedLease.token);
    if (lease !== checkedLease) return;
    if (state.state === 'REVOKING') {
      const error = new Error('Camera removed'); error.status = 403; throw error;
    }
    if (publisher) status.textContent = `Camera ${state.slot} · ${cameraStateLabel(state)}${state.on_air ? ' · On air' : ''}`;
  } catch (error) {
    if (lease !== checkedLease) return;
    if (error.status === 403 || error.status === 404) {
      try { await release(); } catch (_) {}
      status.textContent = 'Camera sharing is off.';
      message('Your camera slot expired or was removed. Select Join camera again.');
      stopButton.disabled = true; joinButton.disabled = false;
    } else {
      status.textContent = `Camera ${checkedLease.slot} · Status unavailable. Retrying…`;
    }
  } finally {pollingLease = false;}
}, 1500);
window.addEventListener('pagehide', () => {
  if (publisher) publisher.close();
  if (stream) stream.getTracks().forEach(track => track.stop());
  if (lease) operatorFetch(`/api/lease/${lease.lease_id}/release`, {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${lease.token}`},
    body: '{}', keepalive: true,
  }, lease.token).catch(() => {});
});
