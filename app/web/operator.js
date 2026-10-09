'use strict';
let state, reader, posterUrl, refreshing = false, ended = false, connectionFailed = false;
const cameraCards = new Map(), replayCards = new Map();
let reviewedPlan;
async function loadProgramPoster() {
  const response = await fetch('/api/preview/program');
  if (!response.ok) return;
  posterUrl = URL.createObjectURL(await response.blob());
  document.querySelector('#program').poster = posterUrl;
}
async function studioAction(op, args = {}) {
  const request = {id: crypto.randomUUID(), op, args};
  if (state) {
    request.expected = {run_id: state.control.run_id, context_revision: state.control.context_revision, control_revision: state.control.control_revision,
      program_revision: state.program.revision, sources: actionSources(args)};
  }
  // Keep the exact request for retries after a lost HTTP response.
  sessionStorage.setItem('breadcast-pending-action', JSON.stringify(request));
  const record = await api('/api/actions', request);
  sessionStorage.removeItem('breadcast-pending-action');
  if (record.state === 'Rejected') throw new Error(record.reason + (record.takeover ? ' · Crew paused' : ''));
  return record;
}
function actionSources(args) {
  const slots = new Set([args.slot || state.program.primary_slot]);
  const replay = state.replays.find(r => r.id === args.replay_id);
  if (replay && replay.validation.plan.input_kind !== 'archive') replay.validation.plan.shots.forEach(s => slots.add(s.source?.slot || Number(s.source_id.replace('camera-', ''))));
  return [...slots].sort().flatMap(slot => {
    const camera = state.cameras.find(c => c.slot === slot);
    return camera && camera.epoch ? [{slot, source_path: camera.source_path, epoch: camera.epoch}] : [];
  });
}
async function command(action, extra = {}) {
  if (!state) return false;
  try { await studioAction(action, extra); message(); await refresh(); return true; }
  catch (error) { message(error.message); await refresh(); return false; }
}
document.querySelector('#return').onclick = event => {if (event.currentTarget.getAttribute('aria-disabled') !== 'true') command('live');};
document.querySelector('#hold').onclick = () => command('holding');
document.querySelector('#end').onclick = () => {document.querySelector('#end-confirm').dataset.runId = state.control.run_id; document.querySelector('#end-confirm').showModal();};
document.querySelector('#confirm-end').onclick = async () => {
  try {
    await api('/api/event/end', {confirm: 'End broadcast', run_id: document.querySelector('#end-confirm').dataset.runId}); ended = true; message();
    if (reader) reader.close();
    document.querySelector('#end-confirm').close();
    if (featureModal.open) featureModal.close();
    document.querySelector('#program-status').textContent = 'Broadcast ended';
    document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  } catch (error) { message(error.message); }
};
document.querySelector('#end-confirm').addEventListener('close', () => message(document.querySelector('#message').textContent));
document.querySelector('#rotate').onclick = async () => {
  try { await api('/api/join/rotate', {}); await refresh(); } catch (error) { message(error.message); }
};
document.querySelector('#render').onclick = async () => {
  try {
    await studioAction('prepare', {slot: Number(document.querySelector('#replay-camera').value),
      seconds: Number(document.querySelector('#seconds').value), speed: Number(document.querySelector('#speed').value),
      zoom: Number(document.querySelector('#zoom').value)});
    message(); await refresh();
  } catch (error) { message(error.message); }
};
async function preview(image, slot) {
  const response = await fetch(`/api/preview/${slot}`);
  if (!response.ok) return;
  const url = URL.createObjectURL(await response.blob());
  const old = image.dataset.blob;
  image.src = url; await image.decode(); image.hidden = false; image.dataset.blob = url;
  image.nextElementSibling.hidden = true;
  if (old) URL.revokeObjectURL(old);
}
async function refreshReplaySetup() {
  const status = document.querySelector('#replay-setup-status');
  try {
    const context = await api('/api/replay-context');
    const cameras = state.cameras.filter(camera => camera.buffer_ready);
    const mappings = context.mappings.filter(mapping => cameras.some(camera =>
      mapping.source_id === `camera-${camera.slot}` && mapping.source_epoch === camera.epoch && mapping.source_path === camera.source_path));
    const evidence = context.evidence.filter(record => record.status === 'active' && record.expires_at > Date.now()/1000 &&
      mappings.some(mapping => mapping.source_id === record.source_id && mapping.source_epoch === record.source_epoch && mapping.revision === record.mapping_revision));
    const counts = `${cameras.length} buffered cameras · ${new Set(mappings.map(mapping => mapping.source_id)).size} cameras with saved calibration · ${evidence.length} active evidence records.`;
    const next = cameras.length < 2 ? 'Connect at least two cameras for a multi-camera replay.' :
      !mappings.length ? 'Add shared-marker calibration and visual evidence below.' :
      !evidence.length ? 'Add visual evidence for the calibrated interval below.' : 'Review a plan to check timing, evidence, and retained media coverage.';
    status.textContent = `${counts} ${next}`;
  } catch (error) { status.textContent = `Cannot load replay setup: ${error.message}`; }
}
let tooltipTrigger, tooltipTimer;
const studioTooltip = document.querySelector('#studio-tooltip');
function hideStudioTooltip() {
  clearTimeout(tooltipTimer); studioTooltip.hidden = true;
  if (tooltipTrigger) tooltipTrigger.removeAttribute('aria-describedby');
  tooltipTrigger = undefined;
}
function placeStudioTooltip(button) {
  clearTimeout(tooltipTimer);
  if (tooltipTrigger && tooltipTrigger !== button) tooltipTrigger.removeAttribute('aria-describedby');
  tooltipTrigger = button;
  if (button.getAttribute('aria-describedby') !== studioTooltip.id) button.setAttribute('aria-describedby', studioTooltip.id);
  studioTooltip.hidden = false;
  const anchor = button.getBoundingClientRect(), box = studioTooltip.getBoundingClientRect();
  const above = button.closest('.program-toolbar') || anchor.bottom + box.height + 8 > innerHeight;
  studioTooltip.style.left = Math.max(8, Math.min(anchor.right - box.width, innerWidth - box.width - 8)) + 'px';
  studioTooltip.style.top = Math.max(8, above ? anchor.top - box.height - 8 : anchor.bottom + 8) + 'px';
}
function showControlTooltip(button) {
  const heading = document.createElement('strong'); heading.textContent = button.getAttribute('aria-label');
  const detail = document.createElement('p'); detail.textContent = button.dataset.tooltip;
  studioTooltip.replaceChildren(heading, detail); placeStudioTooltip(button);
}
function showCameraInfo(button, camera) {
  const p = state.program;
  const selected = p.audio_source_path === camera.source_path && !p.audio_muted;
  const heading = document.createElement('strong'); heading.textContent = `Camera ${camera.slot}`;
  const health = document.createElement('p'); health.textContent = `${camera.buffer_ready ? 'Ready' : camera.state} · ${camera.buffer_seconds || 0}s retained · Epoch ${camera.epoch ?? 'unknown'}`;
  const audio = document.createElement('p'); audio.textContent = !camera.has_audio ? 'No microphone audio available.' : selected ? 'Selected microphone. Other cameras are muted.' : 'Microphone muted. Unmute to use this camera.';
  const timing = document.createElement('p'); timing.className = 'muted'; timing.textContent = 'Live views are independent. Replay cuts need calibration.';
  studioTooltip.replaceChildren(heading, health, audio, timing);
  if (selected && (p.actual === 'REPLAY' || p.graphics.applied.covers_camera)) {
    const suppressed = document.createElement('p'); suppressed.textContent = 'Program audio is temporarily muted for replay or full-screen graphics.'; studioTooltip.append(suppressed);
  }
  placeStudioTooltip(button);
}
function deferStudioTooltipClose() {
  clearTimeout(tooltipTimer);
  tooltipTimer = setTimeout(() => {
    if (!studioTooltip.matches(':hover') && !tooltipTrigger?.matches(':hover, :focus')) hideStudioTooltip();
  }, 120);
}
studioTooltip.onmouseenter = () => clearTimeout(tooltipTimer);
studioTooltip.onmouseleave = deferStudioTooltipClose;
document.addEventListener('keydown', event => {if (event.key === 'Escape') hideStudioTooltip();});
document.addEventListener('pointerdown', event => {
  if (!studioTooltip.contains(event.target) && !tooltipTrigger?.contains(event.target)) hideStudioTooltip();
});
document.addEventListener('scroll', event => {if (event.target !== studioTooltip) hideStudioTooltip();}, true);
window.addEventListener('resize', hideStudioTooltip);
function cameraCard(camera) {
  let card = cameraCards.get(camera.slot);
  if (!card) {
    card = document.createElement('section'); card.className = 'camera-tile'; card.dataset.slot = camera.slot;
    card.innerHTML = `<div class="camera-toolbar"><h3></h3><div class="camera-buttons" role="group"><button class="select camera-button" type="button"></button><button class="audio camera-button" type="button"></button><button class="remove camera-button" type="button"></button></div><button class="camera-info camera-button" type="button"></button></div><div class="camera-picture"><img class="preview" hidden><p class="preview-wait">Connecting…</p></div>`;
    card.querySelector('.select').append(studioIcon('send'));
    card.querySelector('.remove').append(studioIcon('trash-2'));
    card.querySelector('.camera-info').append(studioIcon('info'));
    const reset=document.createElement('button');reset.className='frame-reset camera-button';reset.type='button';reset.setAttribute('aria-label','Reset live framing');reset.title='Full frame';reset.append(studioIcon('maximize'));reset.onclick=()=>command('reset_crop');card.querySelector('.camera-buttons').append(reset);
    document.querySelector('#cameras').append(card); cameraCards.set(camera.slot, card);
  }
  const p = state.program;
  const onAir = p.actual === 'LIVE' && p.actual_target.source_path === camera.source_path && !p.graphics.applied.covers_camera;
  card.querySelector('.frame-reset').hidden=!onAir || !p.framing;
  const microphone = p.audio_source_path === camera.source_path && !p.audio_muted && camera.has_audio;
  card.dataset.onAir = String(onAir); card.dataset.microphone = String(microphone);
  card.querySelector('h3').textContent = `Camera ${camera.slot}`;
  card.querySelector('h3').setAttribute('aria-label', `Camera ${camera.slot}${onAir ? ' · On air' : ''}`);
  card.querySelector('.camera-buttons').setAttribute('aria-label', `Camera ${camera.slot} controls`);
  card.querySelector('img').alt = `Camera ${camera.slot} preview`;
  const select = card.querySelector('.select');
  select.setAttribute('aria-label', `Take camera ${camera.slot} live`); select.title = `Take camera ${camera.slot} live · Independent view`;
  select.setAttribute('aria-pressed', String(onAir)); select.disabled = !camera.buffer_ready;
  select.onclick = () => command('live', {slot: camera.slot, independent: true});
  const audio = card.querySelector('.audio'), audioIcon = microphone ? 'mic' : 'mic-off';
  if (audio.dataset.icon !== audioIcon) {audio.replaceChildren(studioIcon(audioIcon)); audio.dataset.icon = audioIcon;}
  audio.setAttribute('aria-pressed', String(microphone));
  audio.setAttribute('aria-label', !camera.has_audio ? `Camera ${camera.slot} microphone unavailable` : microphone ? `Mute camera ${camera.slot} microphone` : `Use camera ${camera.slot} microphone`);
  audio.title = audio.getAttribute('aria-label'); audio.disabled = !camera.has_audio || !camera.buffer_ready;
  audio.onclick = () => command('audio', {slot: camera.slot, muted: microphone});
  const remove = card.querySelector('.remove'); remove.setAttribute('aria-label', `Remove camera ${camera.slot}`); remove.title = remove.getAttribute('aria-label');
  remove.onclick = async () => {
    try { await api(`/api/lease/${camera.lease_id}/release`, {}); await refresh(); } catch (error) { message(error.message); }
  };
  const info = card.querySelector('.camera-info'); info.setAttribute('aria-label', `Camera ${camera.slot} details`);
  info.onmouseenter = info.onfocus = info.onclick = () => showCameraInfo(info, camera);
  info.onmouseleave = info.onblur = deferStudioTooltipClose;
  if (tooltipTrigger === info) showCameraInfo(info, camera);
  preview(card.querySelector('img'), camera.slot).catch(() => {});
}
async function refresh() {
  if (refreshing || ended) return;
  refreshing = true;
  try {
    state = await api('/api/status', undefined);
    if (connectionFailed) { message(); connectionFailed = false; }
    document.querySelector('#workspace').hidden = false;
    document.querySelector('#end').hidden = false;
    if (!reader) {
      loadProgramPoster().catch(() => {});
      reader = playProgram(document.querySelector('#program'), error => { message(`Viewer: ${error}`); });
    }
    const p = state.program;
    const crewHealth=document.querySelector('#crew-health');crewHealth.hidden=!state.direction.enabled;
    crewHealth.textContent=`${state.direction.fixture ? 'Local fixture crew' : 'Provider crew'} · ${state.direction.reason}`;
    document.querySelector('#setup-status').textContent = state.direction.setup.ready ? `Graphics ready · Context ${state.direction.setup.context_revision}` : 'Event graphics need preparation for the current context.';
    document.querySelector('#framing-status').textContent = p.framing ? 'Static crop active · Use Full frame to reset.' : 'Full frame';
    if (!document.querySelector('#event-setup-form').dataset.loaded) {
      const context = state.event_context;
      for (const [id, value] of Object.entries({title:context.title || '', profile:context.profile, participants:context.participants.join('\n'),
        pronunciations:JSON.stringify(context.pronunciations),style:context.commentary_style,language:context.language,voice:context.voice_id || '',
        audio:context.audio_policy.designated || '',delay:state.delay_s})) document.querySelector(`#setup-${id}`).value=value;
      document.querySelector('#event-setup-form').dataset.loaded='true';
      document.querySelector('#event-setup-form').dataset.revision=String(context.revision);
    }
    graphicsState(p);
    crewState(state.control);
    const applying = state.control.actions.filter(a => a.state === 'Applying').map(a => `${a.op} · Applying`).join(' · ');
    const pending = document.querySelector('#pending-status'); pending.textContent = applying; pending.hidden = !applying;
    document.querySelector('#program-status').textContent = p.actual === 'LIVE' ? `Live · Camera ${p.actual_target.slot} · ${state.delay_s}s buffer` :
      p.actual === 'REPLAY' ? 'Replay playing · live cameras keep recording' : p.requested === 'LIVE' ? `Holding · Camera ${p.primary_slot} unavailable` : 'Holding · waiting for live camera';
    const screen = p.graphics.applied.visible.find(cue => cue.slot === 'screen');
    if (screen) document.querySelector('#program-status').textContent = `${screen.name} · live cameras keep recording`;
    document.querySelector('#program').setAttribute('aria-label', 'Program monitor · ' + document.querySelector('#program-status').textContent);
    const returnButton = document.querySelector('#return');
    const liveAvailable = state.cameras.some(camera => camera.slot === p.primary_slot && camera.buffer_ready && (!p.primary_source_path || camera.source_path === p.primary_source_path));
    returnButton.setAttribute('aria-disabled', String(!liveAvailable));
    returnButton.dataset.tooltip = liveAvailable ? 'Interrupt replay and return to the selected live camera.' : 'The selected live camera is unavailable. Choose a ready camera to go live.';
    updateJoinNavigation(state.join_url);
    const minutes = Math.ceil(state.join_remaining_s / 60);
    document.querySelector('#join-expiry').textContent = minutes > 0 ? `Join code valid for ${minutes >= 60 ? Math.ceil(minutes / 60) + ' hours' : minutes + ' min'}` : 'Camera join code expired. Replace it to allow new joins.';
    for (const button of document.querySelectorAll('[data-urgent]')) {
      const original = document.querySelector({takeover:'#takeover',live:'#return',clear:'#graphics-clear'}[button.dataset.urgent]);
      button.setAttribute('aria-label', original.getAttribute('aria-label')); button.title = original.dataset.tooltip; button.setAttribute('aria-disabled', original.getAttribute('aria-disabled') || 'false');
    }
    updateAudio();
    document.querySelector('#camera-empty').hidden = state.cameras.length > 0;
    const occupied = new Set(state.cameras.map(c => c.slot));
    for (const [slot, card] of cameraCards) if (!occupied.has(slot)) {
      if (card.querySelector('img').dataset.blob) URL.revokeObjectURL(card.querySelector('img').dataset.blob);
      if (tooltipTrigger && card.contains(tooltipTrigger)) hideStudioTooltip();
      card.remove(); cameraCards.delete(slot);
    }
    state.cameras.forEach(cameraCard);
    await refreshReplaySetup();
    const select = document.querySelector('#replay-camera'), selected = select.value;
    select.replaceChildren(...state.cameras.map(camera => {
      const option = document.createElement('option'); option.value = camera.slot; option.textContent = `Camera ${camera.slot}`; return option;
    }));
    if (!state.cameras.length) {
      const option = document.createElement('option'); option.value = ''; option.textContent = 'No cameras yet'; select.append(option);
    }
    if (occupied.has(Number(selected))) select.value = selected;
    document.querySelector('#render').disabled = !state.cameras.some(camera => camera.buffer_ready) || state.jobs.some(job => job.state === 'rendering');
    document.querySelector('#jobs').replaceChildren(...state.jobs.filter(job => job.state !== 'ready').map(job => {
      const row = document.createElement('div');
      const p = document.createElement('p'); p.textContent = `Replay preparation: ${job.canceled ? 'Canceled' : job.state === 'rendering' ? 'Preparing' : job.state}${job.error || job.reason ? ` — ${job.error || job.reason}` : ''}`; row.append(p);
      if (['queued', 'preparing', 'rendering'].includes(job.state)) {const cancel = document.createElement('button'); cancel.textContent = 'Cancel preparation'; cancel.onclick = () => command('cancel', {job_id: job.id}); row.append(cancel);}
      return row;
    }));
    const availableReplayIds = new Set(state.replays.map(r => r.id));
    for (const [id, card] of replayCards) if (!availableReplayIds.has(id)) {card.remove(); replayCards.delete(id);}
    for (const replay of state.replays) {
      if (replayCards.has(replay.id)) {
        const existing = replayCards.get(replay.id);
        existing.querySelector('button').disabled = !replay.eligible;
        existing.querySelector('.eligibility').textContent = replay.eligible ? 'Ready · Available' : replay.reason;
        continue;
      }
      const row = document.createElement('div'); row.className = 'replay';
      const label = document.createElement('p');
      const plan = replay.validation.plan;
      label.textContent = `${plan.fixture || replay.validation.evidence_origins?.includes('fixture') ? 'Fixture · ' : ''}${replay.duration_s.toFixed(2)}s · validated`;
      const list = document.createElement('ol');
      list.replaceChildren(...plan.shots.map(shotDescription));
      const reason = document.createElement('p'); reason.className = 'eligibility'; reason.textContent = replay.eligible ? 'Ready · Available' : replay.reason;
      const preview = document.createElement('video'); preview.controls = true; preview.muted = true;
      preview.preload = 'metadata'; preview.src = replay.preview_url; preview.className = 'preview';
      preview.setAttribute('aria-label', 'Rendered replay preview');
      const play = document.createElement('button'); play.textContent = 'Play replay';
      play.disabled = !replay.eligible;
      play.onclick = () => command('replay', {replay_id: replay.id});
      row.append(label, list, reason, preview, play);
      replayCards.set(replay.id, row); document.querySelector('#replays').append(row);
    }
    document.querySelector('#diagnostics').textContent = JSON.stringify({program: p, cameras: state.cameras, gateway_error: state.gateway_error, providers: state.providers, direction:state.direction}, null, 2);
  } catch (error) {
    connectionFailed = true; message('Cannot reach the studio. Retrying…');
  }
  finally {
    refreshing = false;
  }
}
refresh(); setInterval(refresh, 500);

document.querySelector('#event-setup-form').onsubmit=async event => {
  event.preventDefault();
  const form=event.currentTarget;
  const expected=Number(form.dataset.revision);
  const value=id=>document.querySelector(`#setup-${id}`).value.trim();
  try {
    const context={...state.event_context, revision:expected+1,title:value('title') || null,profile:value('profile'),
      participants:value('participants').split('\n').map(x=>x.trim()).filter(Boolean),pronunciations:JSON.parse(value('pronunciations') || '{}'),
      commentary_style:value('style'),language:value('language'),voice_id:value('voice') || null,
      audio_policy:{designated:value('audio') || 'Keep designated microphone'},broadcast_delay_s:Number(value('delay'))};
    const result=await api('/api/setup',{context,expected_revision:expected,operation_key:crypto.randomUUID()});
    form.dataset.revision=String(result.context.revision);message();await refresh();
  } catch(error) {message(error.message);}
};
document.querySelector('#framing-form').onsubmit=event=> {
  event.preventDefault();
  const rect={};for(const key of ['x','y','width','height'])rect[key]=Number(document.querySelector(`#crop-${key}`).value);
  command('crop',{rect,geometry_revision:state.program.actual_target.native?.timeline_revision});
};
document.querySelector('#crop-reset').onclick=()=>command('reset_crop');

function shotDescription(shot) {
  const item = document.createElement('li');
  if (shot.source && shot.native) {
    const [numerator, denominator] = shot.source.time_base.split('/').map(Number);
    item.textContent = `Original camera ${shot.source.slot} · ${(shot.native.start*numerator/denominator).toFixed(3)}–${(shot.native.end*numerator/denominator).toFixed(3)}s · ${shot.speed}× · ${shot.edit === 'repeat' ? 'Alternate angle repeat' : 'Continuous action'} · ${shot.reason}`;
    return item;
  }
  const start = shot.event_start_ms ?? shot.source_start_ms;
  const end = shot.event_end_ms ?? shot.source_end_ms;
  item.textContent = `${shot.source_id} · ${(start/1000).toFixed(3)}–${(end/1000).toFixed(3)}s · ${shot.speed}× · ${shot.edit === 'repeat' ? 'Alternate angle repeat' : 'Continuous action'} · ${shot.reason}`;
  return item;
}
document.querySelector('#replay-plan').oninput = () => {
  reviewedPlan = undefined; document.querySelector('#render-plan').disabled = true;
};
document.querySelector('#validate-plan').onclick = async () => {
  reviewedPlan = undefined; document.querySelector('#render-plan').disabled = true;
  try {
    const result = await api('/api/replay-validate', JSON.parse(document.querySelector('#replay-plan').value));
    reviewedPlan = result.plan;
    document.querySelector('#plan-shots').replaceChildren(...result.shots.map(shotDescription));
    document.querySelector('#plan-result').textContent = `${result.plan.fixture ? 'Fixture. ' : ''}${(result.plan.expected_duration_ms/1000).toFixed(2)}s. ${result.plan.selection_reason || 'Timing, evidence, and coverage passed.'}`;
    document.querySelector('#render-plan').disabled = false; message();
  } catch (error) { document.querySelector('#plan-result').textContent = `Rejected: ${error.message}`; }
};
document.querySelector('#render-plan').onclick = async () => {
  if (!reviewedPlan) return;
  try { await studioAction('prepare', {plan: reviewedPlan}); message(); await refresh(); }
  catch (error) { document.querySelector('#plan-result').textContent = `Rejected: ${error.message}`; }
};
document.querySelector('#cancel-render').onclick = async () => {
  try { const job = state.jobs.find(j => j.state === 'rendering');
    if (!job) throw new Error('No preparation is running');
    await studioAction('cancel', {job_id: job.id}); message('Cancellation requested for this preparation.'); }
  catch (error) { message(error.message); }
};
document.querySelector('#load-replay-context').onclick = async () => {
  try { document.querySelector('#replay-context').textContent = JSON.stringify(await api('/api/replay-context'), null, 2); }
  catch (error) { message(error.message); }
};
document.querySelector('#submit-replay-record').onclick = async () => {
  try {
    const kind = document.querySelector('#replay-record-type').value;
    const result = await api(`/api/replay-${kind}`, JSON.parse(document.querySelector('#replay-record').value));
    if (kind === 'select') {
      document.querySelector('#replay-plan').value = JSON.stringify(result, null, 2);
      reviewedPlan = undefined; document.querySelector('#render-plan').disabled = true;
      document.querySelector('#plan-result').textContent = `Fixture selection. ${result.selection_reason}`;
    } else if (kind === 'window') {
      document.querySelector('#replay-window').replaceChildren(...result.frames.map(frame => {
        const figure = document.createElement('figure'), image = document.createElement('img'), caption = document.createElement('figcaption');
        image.src = `data:image/jpeg;base64,${frame.jpeg_base64}`; image.className = 'preview'; image.alt = 'Timestamped source frame';
        caption.textContent = `${result.source_id} · PTS ${frame.pts} · ${frame.event_ms === null ? 'Event time unknown' : (frame.event_ms/1000).toFixed(3) + 's event'}`;
        figure.append(image, caption); return figure;
      }));
      const summary = {...result}; delete summary.frames;
      document.querySelector('#replay-context').textContent = JSON.stringify(summary, null, 2);
    } else document.querySelector('#replay-context').textContent = JSON.stringify(result, null, 2);
    message();
  } catch (error) { message(error.message); }
};

let drawerTrigger, activePanel;
const featureModal = document.querySelector('#asset-drawer');
trapDialogFocus(featureModal);
trapDialogFocus(document.querySelector('#end-confirm'));
async function openPanel(name, trigger) {
  if (document.fullscreenElement) await document.exitFullscreen().catch(error => message(error.message));
  if (name === 'crew') {
    if (featureModal.open) featureModal.close();
    document.querySelector('#crew-panel').hidden = false;
    document.querySelector('#workspace').classList.remove('crew-collapsed');
    document.querySelector('#crew-panel').scrollIntoView({block: 'nearest'});
    return;
  }
  if (!featureModal.open) drawerTrigger = trigger || document.activeElement;
  activePanel = name;
  hideStudioTooltip();
  for (const panel of document.querySelectorAll('.asset-panel')) panel.hidden = panel.id !== `panel-${name}`;
  const label = {settings: 'Event details', replays: 'Replays', graphics: 'Graphics', audio: 'Audio', commands: 'Commands'}[name];
  featureModal.dataset.activePanel = name;
  document.querySelector('#drawer-title').textContent = label;
  document.querySelector('#modal-icon').replaceChildren(studioIcon({replays:'clapperboard',graphics:'image',audio:'mic',settings:'settings',commands:'terminal'}[name]));
  for (const button of document.querySelectorAll('[data-panel]')) button.setAttribute('aria-pressed', String(button.dataset.panel === name));
  document.querySelector('#chat-help').setAttribute('aria-expanded', String(name === 'commands'));
  if (!featureModal.open) featureModal.showModal();
  const error = document.querySelector('#message');
  if (!error.hidden) featureModal.querySelector('.modal-body').prepend(error);
  featureModal.querySelector('.modal-body').scrollTop = 0;
  featureModal.querySelector(`#panel-${name}`).querySelector('input,select,textarea,summary,button:not(:disabled)')?.focus({preventScroll:true});
}
for (const button of document.querySelectorAll('[data-panel]')) {
  if (button.dataset.panel !== 'crew') {button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-controls','asset-drawer');}
  button.onclick = () => openPanel(button.dataset.panel, button);
}
document.querySelector('#close-drawer').onclick = () => featureModal.close();
featureModal.addEventListener('close', () => {
  for (const button of document.querySelectorAll('[data-panel]')) button.setAttribute('aria-pressed', 'false');
  document.querySelector('#chat-help').setAttribute('aria-expanded','false');
  document.body.insertBefore(document.querySelector('#message'),document.querySelector('#workspace'));
  drawerTrigger?.focus({preventScroll:true});
});
featureModal.addEventListener('click', event => {
  const rect = featureModal.getBoundingClientRect();
  if (event.target === featureModal && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) featureModal.close();
});
for (const button of document.querySelectorAll('[data-urgent]')) button.onclick = () => {
  const original = document.querySelector({takeover:'#takeover',live:'#return',clear:'#graphics-clear'}[button.dataset.urgent]);
  original.click();
};
document.querySelector('#collapse-crew').onclick = event => {
  const collapsed = document.querySelector('#workspace').classList.toggle('crew-collapsed');
  const button = event.currentTarget; button.replaceChildren(studioIcon(collapsed ? 'panel-right-open' : 'panel-right-close')); button.setAttribute('aria-label', collapsed ? 'Open crew' : 'Collapse crew'); button.dataset.tooltip = collapsed ? 'Show the Crew conversation.' : 'Hide the Crew panel to make more room for video.'; button.setAttribute('aria-expanded', String(!collapsed)); if (tooltipTrigger === button) showControlTooltip(button);
};
const programVideo = document.querySelector('#program'), programFrame = document.querySelector('.monitor-frame');
document.querySelector('#monitor-audio').onclick = event => {
  programVideo.muted = !programVideo.muted;
  const button = event.currentTarget; button.replaceChildren(studioIcon(programVideo.muted ? 'volume-x' : 'volume-2'));
  button.setAttribute('aria-pressed', String(!programVideo.muted)); button.setAttribute('aria-label', programVideo.muted ? 'Listen to program audio' : 'Mute local playback');
  button.dataset.tooltip = programVideo.muted ? 'Listen in this browser only. This does not change the broadcast microphone.' : 'Mute playback in this browser only. Broadcast audio stays unchanged.';
  if (tooltipTrigger === button) showControlTooltip(button);
};
document.querySelector('#program-fullscreen').onclick = async () => {
  try {if (document.fullscreenElement) await document.exitFullscreen(); else await programFrame.requestFullscreen();} catch (error) {message(error.message);}
};
document.addEventListener('fullscreenchange', () => {
  hideStudioTooltip(); const full = document.fullscreenElement === programFrame;
  const button = document.querySelector('#program-fullscreen'); button.replaceChildren(studioIcon(full ? 'minimize' : 'maximize')); button.setAttribute('aria-label', full ? 'Exit fullscreen' : 'Enter fullscreen'); button.dataset.tooltip = full ? 'Return to the studio workspace.' : 'Expand the program monitor.';
  (full ? programFrame : document.body).append(studioTooltip);
});
let programFitPending = false;
function fitProgram() {
  const main = document.querySelector('.main-surface');
  const gap = parseFloat(getComputedStyle(main).rowGap) || 0;
  const reserve = document.querySelector('.camera-section').getBoundingClientRect().height + gap;
  fitProgramFrame(programVideo, reserve);
}
function scheduleProgramFit() {
  if (programFitPending) return; programFitPending = true;
  requestAnimationFrame(() => {programFitPending = false; fitProgram();});
}
const programSizer = new ResizeObserver(scheduleProgramFit);
programSizer.observe(document.querySelector('.main-surface')); programSizer.observe(document.querySelector('.camera-section'));
programVideo.addEventListener('loadedmetadata', scheduleProgramFit);

document.querySelector('#takeover').onclick = () => runControl(state.control.crew_paused ? 'resume' : 'takeover');
document.querySelector('#start-rehearsal').onclick = () => runControl('rehearsal', {slot: Number(document.querySelector('#replay-camera').value) || state.program.primary_slot});
async function runControl(op, args) {
  try { await studioAction(op, args); message(); await refresh(); } catch (error) { message(error.message); await refresh(); }
}
const crewCards = new Map(), dismissedFailures = new Set();
function actionButton(label, icon, callback, iconOnly = false) {
  const button = document.createElement('button'); button.type = 'button';
  button.setAttribute('aria-label', label); button.title = label;
  button.append(studioIcon(icon));
  if (iconOnly) button.className = 'icon-button'; else button.append(document.createTextNode(label));
  button.onclick = callback; return button;
}
function actionLabel(action) {
  const slot = action.args.slot;
  const graphic = action.args.graphics;
  return {live: slot ? `Camera ${slot}` : 'Return live', audio: action.args.muted ? `Mute microphone ${slot}` : `Microphone ${slot}`,
    replay: 'Play replay', holding: 'Hold screen', prepare: 'Prepare replay', cancel: 'Cancel preparation',
    takeover: 'Take control', resume: 'Release control',
    policy: 'Crew policy', rehearsal: 'Local rehearsal', chat: 'Command',
    graphics: graphic?.op === 'clear-all' ? 'Clear graphics' : graphic?.op === 'score' ? 'Update official score' : 'Show graphic'}[action.op] || action.op;
}
function crewState(control) {
  const takeover = document.querySelector('#takeover');
  takeover.setAttribute('aria-pressed', String(control.crew_paused));
  takeover.setAttribute('aria-label', control.crew_paused ? 'Release control' : 'Take control');
  takeover.dataset.tooltip = control.crew_paused ? 'Release control so fresh crew proposals can run. The current picture keeps playing.' : 'Pause crew actions and keep the current picture. Direct controls remain available.';
  const rehearsal = control.rehearsal;
  const status = document.querySelector('#rehearsal-status');
  status.hidden = rehearsal.state === 'Stopped';
  status.textContent = `${rehearsal.state}${rehearsal.reason ? ' · ' + rehearsal.reason : ''}`;
  document.querySelector('#start-rehearsal').disabled = control.crew_paused || rehearsal.state === 'Running';
  const pending = new Set(['Queued', 'Preparing', 'Scheduled', 'Applying', 'On air']);
  const failures = new Set(['Failed', 'Rejected', 'Expired']);
  const records = control.actions.filter(a => !dismissedFailures.has(a.id)).sort((a,b) => a.created_at - b.created_at);
  const latestReady = [...records].reverse().find(a => a.state === 'Ready' && state.replays.some(r => r.id === a.job_id));
  const conversation = new Set(records.filter(a => a.input_text || a.op === 'chat').slice(-8).map(a => a.id));
  const feed = document.querySelector('#crew-feed');
  const wasAtBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
  const visible = new Set(records.map(a => a.id));
  let changed = false, historyCount = 0;
  const positions = {current: 0, history: 0};
  for (const [id, card] of crewCards) if (!visible.has(id)) {card.remove(); crewCards.delete(id); changed = true;}
  for (const action of records) {
    const archived = !pending.has(action.state) && !failures.has(action.state) && action !== latestReady && !conversation.has(action.id);
    if (archived) historyCount++;
    const parent = document.querySelector(archived ? '#crew-history-items' : '#crew-actions');
    const ready = state.replays.find(r => r.id === action.job_id);
    const version = `${action.updated_at}:${action.state}:${ready?.eligible}:${ready?.reason}:${archived}`;
    let card = crewCards.get(action.id);
    if (!card) {card = document.createElement('article'); crewCards.set(action.id, card); changed = true;}
    const position = positions[archived ? 'history' : 'current']++;
    if (parent.children[position] !== card) {parent.insertBefore(card, parent.children[position] || null); changed = true;}
    if (card.dataset.version === version) continue;
    card.dataset.version = version; card.dataset.actionId = action.id; card.dataset.state = action.state;
    card.className = archived ? 'action-card history-card' : 'action-card'; card.replaceChildren();
    if (!archived && (action.input_text || action.op === 'chat')) {
      const input = document.createElement('p'); input.className = 'chat-bubble human-message';
      input.textContent = action.input_text || action.args.text; card.append(input);
    }
    const result = document.createElement('div'); result.className = 'crew-result';
    const avatar = document.createElement('span'); avatar.className = 'result-icon';
    avatar.append(studioIcon(failures.has(action.state) ? 'circle-alert' : action.op === 'prepare' || action.op === 'replay' ? 'clapperboard' : action.actor === 'Human' ? 'check' : 'radio'));
    const body = document.createElement('div'); body.className = 'result-body';
    const heading = document.createElement('div'); heading.className = 'action-heading';
    const title = document.createElement('strong'); title.textContent = actionLabel(action);
    const badge = document.createElement('span'); badge.className = 'action-status'; badge.textContent = action.state;
    heading.append(title, badge); body.append(heading);
    if (!archived) {
      const actor = document.createElement('small'); actor.className = 'action-actor'; actor.textContent = action.actor === 'Human' ? 'You' : action.actor; body.append(actor);
      const reason = ready && !ready.eligible ? ready.reason : action.reason;
      if (reason) {const detail = document.createElement('p'); detail.className = 'action-detail'; detail.textContent = reason; body.append(detail);}
      if (ready) {
        const attachment = document.createElement('details'); attachment.className = 'replay-attachment';
        const summary = document.createElement('summary');
        summary.append(studioIcon('play'), document.createTextNode(`Replay · ${ready.duration_s.toFixed(1)}s`), studioIcon('chevron-down'));
        summary.setAttribute('aria-label', 'Preview replay');
        const preview = document.createElement('video'); preview.controls = true; preview.muted = true;
        preview.preload = 'metadata'; preview.setAttribute('aria-label','Crew replay preview');
        attachment.ontoggle = () => {if (attachment.open && !preview.getAttribute('src')) preview.src = ready.preview_url; if (!attachment.open) preview.pause();};
        attachment.append(summary, preview); body.append(attachment);
      }
      const buttons = document.createElement('div'); buttons.className = 'action-buttons'; buttons.setAttribute('role', 'group'); buttons.setAttribute('aria-label','Action controls');
      if (ready) {const play = actionButton('Play replay', 'play', () => command('replay', {replay_id: ready.id})); play.disabled = !ready.eligible; buttons.append(play);}
      if (action.state === 'Preparing') buttons.append(actionButton('Cancel preparation','square',() => runControl('cancel',{job_id:action.job_id})));
      if (failures.has(action.state)) buttons.append(actionButton('Dismiss failure','x',() => {dismissedFailures.add(action.id);crewState(state.control);},true));
      if (buttons.children.length) body.append(buttons);
    }
    const data = document.createElement('details'); data.className = 'action-data';
    const summary = document.createElement('summary'); summary.textContent = 'Details';
    const content = document.createElement('pre'); content.textContent = `${action.actor} · ${action.id}\n${JSON.stringify(action.args)}`;
    data.append(summary, content); body.append(data);
    result.append(avatar, body); card.append(result);
  }
  document.querySelector('#crew-history').hidden = historyCount === 0;
  document.querySelector('#crew-history-label').textContent = `Recent activity · ${historyCount}`;
  if (changed && wasAtBottom) feed.scrollTop = feed.scrollHeight;
}
function updateAudio() {
  const select = document.querySelector('#audio-source'), selected = select.value;
  const key = state.cameras.map(c => `${c.slot}:${c.has_audio}:${c.buffer_ready}`).join();
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    select.replaceChildren(...state.cameras.map(c => {const o = document.createElement('option');o.value = c.slot;o.textContent = `Camera ${c.slot} · ${c.has_audio && c.buffer_ready ? 'Available' : 'Unavailable'}`;o.disabled = !c.has_audio || !c.buffer_ready;return o;}));
    if (state.cameras.some(c => String(c.slot) === selected)) select.value = selected;
  }
  const designated = state.cameras.find(c => c.slot === state.program.audio_slot && c.source_path === state.program.audio_source_path && c.has_audio && c.buffer_ready);
  document.querySelector('#audio-status').textContent = designated ? `Designated source: Camera ${state.program.audio_slot}${state.program.audio_muted ? ' · Muted' : ' · Microphone active'}. Live audio is muted during replay and full-screen graphics.` : 'Designated microphone unavailable. Select a source.';
  document.querySelector('#select-audio').disabled = !state.cameras.some(c => String(c.slot) === select.value && c.has_audio && c.buffer_ready);
}
document.querySelector('#select-audio').onclick = () => command('audio', {slot: Number(document.querySelector('#audio-source').value)});
document.querySelector('#policy-form').onsubmit = event => {
  event.preventDefault(); runControl('policy', {minimum_shot_s: Number(document.querySelector('#policy-shot').value), replay_cooldown_s: Number(document.querySelector('#policy-cooldown').value), replays_enabled: document.querySelector('#policy-replays').checked});
};
const chatInput = document.querySelector('#chat-input');
let chatSending = false;
function updateComposer() {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(120, Math.max(56, chatInput.scrollHeight)) + 'px';
  document.querySelector('#chat-send').disabled = chatSending || !chatInput.value.trim();
}
chatInput.addEventListener('input', updateComposer);
chatInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); if (!document.querySelector('#chat-send').disabled) document.querySelector('#chat-form').requestSubmit();
  }
});
for (const button of document.querySelectorAll('[data-command]')) button.onclick = () => {chatInput.value = button.dataset.command; updateComposer(); chatInput.focus();};
document.querySelector('#chat-help').onclick = event => openPanel('commands', event.currentTarget);
document.querySelector('#chat-form').onsubmit = async event => {
  event.preventDefault(); if (chatSending || !state || !chatInput.value.trim()) return;
  const request = {id: crypto.randomUUID(), text: chatInput.value, run_id: state.control.run_id};
  chatSending = true; updateComposer();
  sessionStorage.setItem('breadcast-pending-chat', JSON.stringify(request));
  try {
    const record = await api('/api/chat', request); sessionStorage.removeItem('breadcast-pending-chat');
    if (chatInput.value === request.text) chatInput.value = '';
    if (record.state === 'Rejected') message(record.reason); else message();
    await refresh();
  } catch (error) {message(error.message);}
  finally {chatSending = false;updateComposer();chatInput.focus();}
};
for (const button of document.querySelectorAll('[data-tooltip]')) {button.onmouseenter = button.onfocus = () => showControlTooltip(button); button.onmouseleave = button.onblur = deferStudioTooltipClose;}
for (const holder of document.querySelectorAll('[data-icon]')) holder.prepend(studioIcon(holder.dataset.icon));
for (const [selector, icon] of [['#takeover','hand'],['#return','radio'],['#graphics-clear','x'],['[data-panel="replays"]','clapperboard'],['[data-panel="graphics"]','image'],['[data-panel="audio"]','mic'],['[data-panel="crew"]','message-square'],['#collapse-crew','panel-right-close'],['#close-drawer','x']]) {
  const button = document.querySelector(selector); if (button) button.prepend(studioIcon(icon));
}
(async () => {
  for (const [key, route] of [['breadcast-pending-action', '/api/actions'], ['breadcast-pending-chat', '/api/chat']]) {
    const saved = sessionStorage.getItem(key);
    if (saved) try {await api(route, JSON.parse(saved)); sessionStorage.removeItem(key);} catch (error) {message(`Retry failed: ${error.message}`);}
  }
})();

let searchGeneration = 0;
function showMoments(result) {
  const status = document.querySelector('#moment-status');
  status.textContent = result.reason || (result.hits.length ? `${result.hits.length} moments · ${result.ranking === 'simulated' ? 'Simulated fixture ranking' : 'Provider ranking'}` : 'No matching available moments.');
  document.querySelector('#moment-results').replaceChildren(...result.hits.map(hit => {
    const card = document.createElement('article'); card.className = 'replay';
    const title = document.createElement('p'); title.textContent = `${hit.claim_kind === 'inferred' ? 'Inferred · ' : hit.claim_kind === 'observed' ? 'Observed · ' : ''}${hit.description}`;
    const timing = document.createElement('p');
    const [num, den] = hit.source.time_base.split('/').map(Number);
    const fresh = result.freshness.find(x => x.source.source_id === hit.source.source_id && x.source.epoch === hit.source.epoch);
    timing.textContent = `Original camera ${hit.source.slot} · ${(hit.native.start*num/den).toFixed(2)}–${(hit.native.end*num/den).toFixed(2)}s · ${hit.available ? 'Available' : hit.reason || 'Unavailable'}${fresh ? ` · Indexed through ${(fresh.watermark*num/den).toFixed(2)}s` : ' · Index freshness unknown'}`;
    const prepare = document.createElement('button'); prepare.textContent = 'Prepare replay'; prepare.disabled = !hit.available;
    prepare.onclick = async () => {
      prepare.disabled = true;
      try {await studioAction('prepare', {search_id: result.id, scene_id: hit.scene_id, scene_revision: hit.scene_revision}); status.textContent = 'Replay queued. Live cameras keep recording.'; await refresh();}
      catch (error) {status.textContent = error.message; prepare.disabled = !hit.available;}
    };
    card.append(title, timing, prepare); return card;
  }));
}
document.querySelector('#moment-search').addEventListener('submit', async event => {
  event.preventDefault(); if (!state) return;
  const generation = ++searchGeneration;
  const text = document.querySelector('#moment-query').value.trim(); if (!text) return;
  const previous = JSON.parse(sessionStorage.getItem('breadcast-moment-query') || 'null');
  const request = previous?.text === text && previous.run_id === state.control.run_id ? previous : {id: crypto.randomUUID(), run_id: state.control.run_id, text, limit: 5};
  sessionStorage.setItem('breadcast-moment-query', JSON.stringify(request));
  document.querySelector('#moment-status').textContent = 'Finding retained moments…';
  try {const result = await api('/api/search', request); if (generation === searchGeneration) showMoments(result);}
  catch (error) {if (generation === searchGeneration) document.querySelector('#moment-status').textContent = error.message;}
});
(async () => {
  try {const stored = JSON.parse(sessionStorage.getItem('breadcast-moment-query') || 'null');
    if (stored) {document.querySelector('#moment-query').value = stored.text; showMoments(await api(`/api/search/${encodeURIComponent(stored.id)}`));}
  } catch (_) {document.querySelector('#moment-status').textContent = 'Submit a search for this run.';}
})();
