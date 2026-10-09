'use strict';
let lease, stream, publisher, busy = false;
let code = new URLSearchParams(location.search).get('code');
let client = sessionStorage.getItem('breadcast-camera-client');
if (!client) { client = crypto.randomUUID().replaceAll('-', ''); sessionStorage.setItem('breadcast-camera-client', client); }
// A direct Join tab also resolves the current event code without taking a camera slot.
const joinReady = api('/api/viewer').then(state => {
  updateJoinNavigation(state.join_url);
  if (!code) {
    code = new URL(state.join_url).searchParams.get('code');
    history.replaceState(null, '', publicPath('/join') + '?code=' + encodeURIComponent(code));
  }
}).catch(error => {if (!code) throw error;});
joinReady.catch(() => {});
const joinButton = document.querySelector('#join'), startButton = document.querySelector('#start');
const stopButton = document.querySelector('#stop'), status = document.querySelector('#status');
for (const holder of document.querySelectorAll('[data-icon]')) holder.prepend(studioIcon(holder.dataset.icon));
function sharingState(value) {
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
    status.textContent = `Camera ${lease.slot} reserved. Allow camera permission.`;
    stream = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: {ideal: 'environment'}, width: {ideal: 1280}, height: {ideal: 720}, frameRate: {ideal: 30, max: 30}},
      audio: document.querySelector('#microphone').checked,
    });
    document.querySelector('#preview').srcObject = stream;
    sharingState('preview');
    startButton.disabled = false; stopButton.disabled = false;
    status.textContent = `Camera ${lease.slot} preview. Select Start sharing within 60 seconds.`;
  } catch (error) {
    message(error.message);
    try { await release(); } catch (_) {}
    joinButton.disabled = false;
  } finally { busy = false; }
};
startButton.onclick = () => {
  if (!lease || !stream) return;
  startButton.disabled = true; message();
  publisher = new MediaMTXWebRTCPublisher({
    url: `${location.origin}${publicPath(`/media/${lease.source_path}/whip`)}`, token: lease.token, stream,
    videoCodec: 'h264', videoBitrate: 1500, audioCodec: 'opus', audioBitrate: 64, audioVoice: false,
    onConnected: () => { sharingState('sharing'); status.textContent = `Connected as Camera ${lease.slot}`; message(); },
    onError: error => { message(`Connection issue: ${error}. Reconnecting while this slot remains valid.`); },
  });
};
stopButton.onclick = async () => {
  try { await release(); status.textContent = 'Camera sharing is off.'; message(); }
  catch (error) { message(error.message); }
  startButton.disabled = true; stopButton.disabled = true; joinButton.disabled = false;
};
setInterval(async () => {
  if (!lease) return;
  try {
    const state = await api(`/api/lease/${lease.lease_id}`, undefined, lease.token);
    if (publisher) status.textContent = `Camera ${state.slot}: ${state.state.toLowerCase()}${state.on_air ? ' · ON AIR' : ''}`;
  } catch (error) {
    message('Your camera slot expired or was removed. Select Join camera again.');
    try { await release(); } catch (_) {}
    startButton.disabled = true; stopButton.disabled = true; joinButton.disabled = false;
  }
}, 1500);
window.addEventListener('pagehide', () => {
  if (publisher) publisher.close();
  if (stream) stream.getTracks().forEach(track => track.stop());
  if (lease) operatorFetch(`/api/lease/${lease.lease_id}/release`, {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${lease.token}`},
    body: '{}', keepalive: true,
  }, lease.token).catch(() => {});
});
