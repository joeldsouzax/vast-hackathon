// Software viewers and archive recall. Separate physical devices remain an open gate.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const folder = path.resolve(process.argv[2]);
const runtime = path.join(folder, 'runtime');
const access = JSON.parse(fs.readFileSync(path.join(runtime, 'access.json')));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROME_PATH || '/usr/bin/chromium', headless: true,
    args: ['--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage']});
  const contexts = [], viewers = [], samples = [], errors = [], checks = {};
  const report = {mode: 'three software viewers; no physical-device claim', passed: false, checks, samples};
  try {
    for (let i = 0; i < 3; i++) {
      const context = await browser.newContext(); contexts.push(context);
      const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
      await page.goto(access.broadcast_url);
      await page.waitForFunction(() => document.querySelector('#program').getVideoPlaybackQuality().totalVideoFrames > 10);
      await page.evaluate(() => {
        window.playback = {frames: 0, last: performance.now(), gaps: [], first: performance.now()};
        const video = document.querySelector('#program');
        const onFrame = (_, info) => {
          const now = performance.now(); if (now - window.playback.last > 1000) window.playback.gaps.push(now - window.playback.last);
          window.playback.last = now; window.playback.frames++; video.requestVideoFrameCallback(onFrame);
        }; video.requestVideoFrameCallback(onFrame);
      });
      viewers.push(page);
    }
    const studioContext = await browser.newContext(); contexts.push(studioContext);
    const studio = await studioContext.newPage(); studio.on('pageerror', e => errors.push(e.message));
    await studio.goto(access.operator_url); await studio.waitForFunction(() => document.querySelector('#program').videoWidth === 640);
    await studio.evaluate(() => {window.originalPlayer = document.querySelector('#program');});
    await studio.getByRole('button', {name: 'Replays', exact: true}).click();
    await studio.locator('#moment-query').fill('yellow ball');
    await studio.locator('#moment-search').getByRole('button', {name: 'Search', exact: true}).click();
    await studio.locator('#moment-results').getByRole('button', {name: 'Prepare replay', exact: true}).first().waitFor();
    checks.search = true;
    await studio.locator('#moment-results').getByRole('button', {name: 'Prepare replay', exact: true}).first().click();
    await studio.locator('#replays').getByRole('button', {name: 'Play replay', exact: true}).first().waitFor({timeout: 45000});
    checks.prepare = true;
    assert.equal(await studio.evaluate(() => window.originalPlayer === document.querySelector('#program')), true);
    checks.player_mounted = true;
    const preview = studio.locator('#replays video').first();
    await preview.evaluate(v => v.play()); await studio.waitForFunction(() => document.querySelector('#replays video').currentTime > 0.3);
    checks.preview = true;
    await studio.locator('#replays').getByRole('button', {name: 'Play replay', exact: true}).first().click();
    await studio.waitForFunction(() => document.querySelector('#program-status').textContent.includes('Replay'));
    checks.play = true;
    await studio.screenshot({path: path.join(folder, 'recall.png')});
    await studio.locator('#close-drawer').click();
    await studio.waitForFunction(() => !document.querySelector('#asset-drawer').open);
    // The mute button is local. Other viewers retain their own state.
    const otherMute = await Promise.all(viewers.slice(1).map(page => page.locator('#program').evaluate(v => v.muted)));
    await viewers[0].locator('#monitor-audio').click();
    assert.deepEqual(await Promise.all(viewers.slice(1).map(page => page.locator('#program').evaluate(v => v.muted))), otherMute);
    checks.local_mute = true;
    await viewers[0].reload();await viewers[0].waitForFunction(() => document.querySelector('#program').videoWidth === 640);
    checks.reload = true;
    await viewers[0].evaluate(() => {window.playback = {frames: 0, last: performance.now(), gaps: []};const v=document.querySelector('#program');const cb=()=>{const t=performance.now();if(t-window.playback.last>1000)window.playback.gaps.push(t-window.playback.last);window.playback.last=t;window.playback.frames++;v.requestVideoFrameCallback(cb);};v.requestVideoFrameCallback(cb);});
    fs.writeFileSync(path.join(folder, 'viewers-ready'), 'ready');
    while (!fs.existsSync(path.join(folder, 'viewers-stop'))) {
      const rows = [];
      for (const page of viewers) rows.push(await page.evaluate(() => {
        const v=document.querySelector('#program');return {...window.playback,time:v.currentTime,width:v.videoWidth,
          decoded:v.getVideoPlaybackQuality().totalVideoFrames,muted:v.muted};
      }));
      samples.push({utc_ms:Date.now(),viewers:rows});
      if (samples.length > 1) for (let i=0;i<3;i++) assert(rows[i].decoded > samples.at(-2).viewers[i].decoded, `Viewer ${i} did not advance`);
      await sleep(1000);
    }
    assert.equal(errors.length,0,errors.join('; '));
    for (const row of samples.at(-1).viewers) assert.equal(row.gaps.length,0,JSON.stringify(row.gaps));
    checks.three_viewers = samples.length > 1;checks.no_stalls = true;
    report.passed = true;
  } catch (error) {report.error = error.stack; process.exitCode = 1;}
  finally {report.errors=errors;fs.writeFileSync(path.join(folder, 'browser-report.json'), JSON.stringify(report,null,2));await browser.close();}
})();
