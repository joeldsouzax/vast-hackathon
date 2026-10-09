'use strict';
let graphicsCatalog = [], selectedGraphic, graphicsCategory = 'All', graphicsBusy = false;
let graphicPreviewUrl, graphicPreviewRevision = 0, graphicPreviewTimer, scoreLoaded = false, scoreClockDirty = false;
const el = id => document.getElementById(id);
const scoreFields = ['score-home', 'score-away', 'score-home-value', 'score-away-value', 'score-period', 'score-clock-value', 'score-clock-running'];
function scoreInput() {
  const number = id => el(id).value.trim() === '' ? null : Number(el(id).value);
  return {home: el('score-home').value, away: el('score-away').value,
    home_score: number('score-home-value'), away_score: number('score-away-value'),
    period: el('score-period').value, clock_seconds: number('score-clock-value'),
    clock_running: el('score-clock-running').checked, confirmed: el('score-confirmed').checked};
}
function graphicInput(preview = false) {
  const data = {op: 'cue', preset: selectedGraphic.id, title: el('graphic-title').value,
    subtitle: el('graphic-subtitle').value, duration_s: Number(el('graphic-duration').value)};
  if (preview && selectedGraphic.slot === 'score') data.score = scoreInput();
  return data;
}
async function updateGraphicPreview() {
  if (!selectedGraphic) return;
  const revision = ++graphicPreviewRevision;
  try {
    const response = await fetch('/api/graphics/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(graphicInput(true))});
    if (!response.ok) {
      if (revision === graphicPreviewRevision) message((await response.json()).error);
      return;
    }
    const blob = await response.blob();
    if (revision !== graphicPreviewRevision) return;
    if (graphicPreviewUrl) URL.revokeObjectURL(graphicPreviewUrl);
    graphicPreviewUrl = URL.createObjectURL(blob); el('graphic-preview').src = graphicPreviewUrl;
  } catch (error) { if (revision === graphicPreviewRevision) message('Cannot load the graphics preview.'); }
}
function scheduleGraphicPreview() {
  clearTimeout(graphicPreviewTimer); graphicPreviewTimer = setTimeout(updateGraphicPreview, 250);
}
function selectGraphic(spec) {
  selectedGraphic = spec; ++graphicPreviewRevision;
  el('graphic-name').textContent = spec.name;
  el('graphic-motion').textContent = spec.slot === 'stinger' ? '2s transition' : 'Animated';
  el('graphic-title').value = spec.title; el('graphic-subtitle').value = spec.subtitle;
  el('graphic-duration').value = spec.id === 'countdown' ? '30' : spec.slot === 'stinger' ? '2' : '0';
  el('graphic-duration').min = spec.id === 'countdown' ? '1' : '0';
  el('graphic-duration-label').hidden = ['stinger', 'score'].includes(spec.slot);
  el('graphic-copy').hidden = spec.slot === 'score';
  el('graphic-show').textContent = spec.slot === 'stinger' ? 'Play transition ↗' : 'Show graphic ↗';
  el('graphic-show').disabled = graphicsBusy;
  el('graphic-description').textContent = spec.slot === 'screen' ? 'Covers the program and mutes camera audio. Clear it or return to live to reveal the camera.' :
    spec.slot === 'stinger' ? 'A short animated treatment over the current picture. Clears itself after two seconds.' :
    spec.slot === 'score' ? 'Uses saved official values. Blank values show —. Current scores hide during replays. Use the official score form in Settings.' :
    'Adds a layer over the program. A new design replaces the design in the same layer.';
  if (spec.slot === 'score') el('score-controls').open = true;
  renderGraphicGallery(); updateGraphicPreview();
}
function renderGraphicGallery() {
  el('graphics-gallery').replaceChildren(...graphicsCatalog.filter(spec => graphicsCategory === 'All' || spec.category === graphicsCategory).map(spec => {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'graphic-tile';
    button.setAttribute('aria-pressed', String(selectedGraphic?.id === spec.id));
    const img = document.createElement('img'); img.src = `/api/graphics/thumbnail/${spec.id}`; img.alt = ''; img.loading = 'lazy';
    const name = document.createElement('span'); name.textContent = spec.name;
    const category = document.createElement('small'); category.textContent = spec.category;
    button.append(img, name, category); button.onclick = () => selectGraphic(spec); return button;
  }));
}
function graphicsState(program) {
  if (!program.graphics) return;
  const graphics = program.graphics, visible = graphics.applied.visible || [];
  const clear = el('graphics-clear'), unavailable = graphicsBusy || (!visible.length && !graphics.requested.length);
  clear.setAttribute('aria-disabled', String(unavailable));
  clear.dataset.tooltip = graphicsBusy ? 'A graphics command is applying.' : unavailable ? 'No active graphics to clear.' : 'Clear all graphics from the program.';
  const activeKey = JSON.stringify(visible.map(c => [c.cue_id, c.exiting, c.name, c.slot])) + graphicsBusy;
  if (el('graphics-active').dataset.key !== activeKey) {
    el('graphics-active').dataset.key = activeKey;
  el('graphics-active').replaceChildren(...visible.map(cue => {
    const button = document.createElement('button'); button.className = 'graphic-active';
    button.textContent = `${cue.name}${cue.exiting ? ' · clearing' : ' ×'}`;
    button.setAttribute('aria-label', `Clear ${cue.name}`); button.disabled = graphicsBusy || !!cue.exiting;
    button.onclick = () => sendGraphics({op: 'clear', slot: cue.slot}); return button;
  }));
  if (!visible.length) {
    const hint = document.createElement('span'); hint.className = 'muted';
    hint.textContent = graphics.requested.length ? 'Waiting for the next program frame or live view.' : 'The picture speaks for itself.';
    el('graphics-active').append(hint);
  }
  }
  if (!scoreLoaded) {
    const s = graphics.score;
    const fields = {'score-home': s.home, 'score-away': s.away, 'score-home-value': s.home_score,
      'score-away-value': s.away_score, 'score-period': s.period, 'score-clock-value': s.clock_seconds};
    for (const [id, value] of Object.entries(fields)) el(id).value = value ?? '';
    el('score-clock-running').checked = s.clock_running; scoreLoaded = true;
  }
  if (graphics.score.authority) el('score-status').textContent = `Saved official values · display clock ${graphics.applied.clock || '--:--'}. Blank fields stay unknown. Current scores hide during replays.`;
  if (graphics.error) message(`Graphics paused: ${graphics.error}`);
}
async function sendGraphics(graphics, extra = {}) {
  if (graphicsBusy) return false;
  graphicsBusy = true; el('graphic-show').disabled = true; el('graphics-clear').setAttribute('aria-disabled', 'true');
  try { return await command('graphics', {graphics, ...extra}); }
  finally { graphicsBusy = false; if (!ended) { el('graphic-show').disabled = !selectedGraphic; if (state) graphicsState(state.program); } }
}
el('graphic-form').onsubmit = async event => { event.preventDefault(); if (selectedGraphic) await sendGraphics(graphicInput()); };
el('graphics-clear').onclick = () => {if (el('graphics-clear').getAttribute('aria-disabled') !== 'true') sendGraphics({op: 'clear-all'});};
el('score-form').onsubmit = async event => {
  event.preventDefault();
  const score = scoreInput();
  if (!scoreClockDirty) { delete score.clock_seconds; delete score.clock_running; }
  if (await sendGraphics({op: 'score', score}, {effective_event_ms:el('score-event-time').value.trim() === '' ? null : Number(el('score-event-time').value)})) { scoreClockDirty = false; el('score-confirmed').checked = false; scheduleGraphicPreview(); }
};
for (const id of ['graphic-title', 'graphic-subtitle', 'graphic-duration']) el(id).addEventListener('input', scheduleGraphicPreview);
for (const id of scoreFields) el(id).addEventListener('input', () => { if (id.startsWith('score-clock')) scoreClockDirty = true; el('score-confirmed').checked = false; if (selectedGraphic?.slot === 'score') scheduleGraphicPreview(); });
async function loadGraphicsCatalog() {
  try {
    const catalog = await api('/api/graphics/catalog'); graphicsCatalog = catalog.assets;
    el('graphics-count').textContent = `${graphicsCatalog.length} designs · ready to serve`;
    const categories = ['All', ...new Set(graphicsCatalog.map(spec => spec.category))];
    el('graphics-filters').replaceChildren(...categories.map(category => {
      const button = document.createElement('button'); button.textContent = category; button.type = 'button';
      button.setAttribute('aria-pressed', String(category === graphicsCategory));
      button.onclick = () => { graphicsCategory = category; for (const item of el('graphics-filters').children) item.setAttribute('aria-pressed', String(item === button)); renderGraphicGallery(); };
      return button;
    }));
    selectGraphic(graphicsCatalog[0]);
  } catch (error) { el('graphics-count').textContent = 'Graphics unavailable'; message(error.message); }
}
loadGraphicsCatalog();
