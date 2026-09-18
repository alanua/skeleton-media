import { SCENES, PALETTE_FAMILIES, blendSceneStates, deterministicSceneState, normalizeSeed } from './scenes.mjs';
import { emitSceneFrame, subscribeSceneFrames } from './scene-bus.mjs';
import { GenerativeRenderer } from './renderer.mjs';

const canvas = document.querySelector('#stage');
const fatal = document.querySelector('#fatal');
const SCENE_BUS_INTERVAL_MS = 50;
const TARGET_RENDER_INTERVAL_MS = 1000 / 30;
let renderer;
try { renderer = new GenerativeRenderer(canvas); }
catch (error) { fatal.hidden = false; fatal.textContent = `WebGL2 is required: ${error.message}`; throw error; }

const params = new URLSearchParams(location.search);
const debug = params.get('debug') === '1';
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const requestedPalette = params.get('palette');
const settings = {
  seed: normalizeSeed(params.get('seed') || Date.now()),
  scene: Math.max(0, SCENES.findIndex((s) => s.id === params.get('scene'))),
  palette: requestedPalette && Object.hasOwn(PALETTE_FAMILIES, requestedPalette) ? requestedPalette : null,
  dwellMs: clamp(Number(params.get('dwell')) || 8 * 60_000, 5 * 60_000, 15 * 60_000),
  transitionMs: clamp(Number(params.get('transition')) || 3000, 2000, 4000),
  autoRotate: params.get('rotate') !== '0',
  renderScale: clamp(Number(params.get('scale')) || 1, 0.33, 1),
  qualityMode: 'auto',
  paused: false,
};
let sceneA = settings.scene;
let sceneB = settings.scene;
let transitionStart = 0;
let currentBlend = 1;
let nextSwitch = performance.now() + settings.dwellMs;
let lastFrame = performance.now();
let lastPaint = 0;
let avgFrame = 16.7;
let slowSince = 0;
let fastSince = 0;
let frameCounter = 0;
let fpsStamp = performance.now();
let fps = 0;
let hidden = document.hidden;

renderer.setSeed(settings.seed);
renderer.setPalette(paletteFor(sceneA));
setupDebug();
window.lavalampSceneBus = Object.freeze({ subscribe: (cb) => subscribeSceneFrames(window, cb) });
document.addEventListener('visibilitychange', () => { hidden = document.hidden; lastFrame = performance.now(); });
addEventListener('resize', resize);
resize();
requestAnimationFrame(frame);
setInterval(() => { if (!hidden && !settings.paused) emitBus(); }, SCENE_BUS_INTERVAL_MS);

function frame(now) {
  if (now - lastPaint < TARGET_RENDER_INTERVAL_MS) { requestAnimationFrame(frame); return; }
  lastPaint = now;
  const dt = Math.min(100, now - lastFrame); lastFrame = now;
  if (!hidden && !settings.paused) {
    avgFrame = avgFrame * 0.96 + dt * 0.04;
    adaptQuality(now);
    if (settings.autoRotate && now >= nextSwitch && sceneA === sceneB) startTransition((sceneA + 1) % SCENES.length, now);
    let blend = 1;
    if (sceneA !== sceneB) {
      blend = clamp((now - transitionStart) / settings.transitionMs, 0, 1);
      if (blend >= 1) { sceneA = sceneB; nextSwitch = now + settings.dwellMs; }
    }
    currentBlend = sceneA === sceneB ? 1 : blend;
    renderer.setScenes(sceneA, sceneB, currentBlend);
    renderer.setPalette(blendPalettes(paletteFor(sceneA), paletteFor(sceneB), currentBlend));
    renderer.setMotion(reducedMotion ? 0.28 : 1);
    const transitionQuality = sceneA === sceneB ? 1 : 0.82;
    renderer.setComplexity(clamp(lerp(complexityFor(sceneA), complexityFor(sceneB), currentBlend) * transitionQuality, 0.5, 1));
    resize(); renderer.render(now / 1000);
    frameCounter++;
    if (now - fpsStamp >= 1000) { fps = frameCounter * 1000 / (now - fpsStamp); frameCounter = 0; fpsStamp = now; updateTelemetry(); }
  }
  requestAnimationFrame(frame);
}

function startTransition(index, now = performance.now()) {
  if (index === sceneA && sceneA === sceneB) return;
  sceneB = (index + SCENES.length) % SCENES.length;
  transitionStart = now;
  currentBlend = 0;
  updateDebugSelection();
}

function resize() {
  const dpr = Math.min(devicePixelRatio || 1, 1.5);
  const scale = settings.renderScale * dpr;
  renderer.resize(Math.max(2, Math.floor(innerWidth * scale)), Math.max(2, Math.floor(innerHeight * scale)));
}

function adaptQuality(now) {
  if (settings.qualityMode !== 'auto') return;
  if (avgFrame > 34) { if (!slowSince) slowSince = now; fastSince = 0; if (now - slowSince > 3500) { settings.renderScale = nextScale(settings.renderScale, -1); slowSince = now; } }
  else if (avgFrame < 22) { if (!fastSince) fastSince = now; slowSince = 0; if (now - fastSince > 15000) { settings.renderScale = nextScale(settings.renderScale, 1); fastSince = now; } }
  else { slowSince = 0; fastSince = 0; }
}

function nextScale(current, dir) { const steps=[.5,.625,.75,.875,1]; let i=steps.reduce((best,v,idx)=>Math.abs(v-current)<Math.abs(steps[best]-current)?idx:best,0); return steps[clamp(i+dir,0,steps.length-1)]; }
function complexityFor(index) { const base=SCENES[index].complexity; const scaleFactor=.62+.38*settings.renderScale; return clamp((.7+base*.3)*scaleFactor,.5,1); }
function paletteFor(index) { const family=settings.palette || SCENES[index].family; return PALETTE_FAMILIES[family]; }
function blendPalettes(a,b,mixValue){ const out=[]; for(let i=0;i<5;i++){const x=a[Math.min(i,a.length-1)],y=b[Math.min(i,b.length-1)];out.push([lerp(x[0],y[0],mixValue),lerp(x[1],y[1],mixValue),lerp(x[2],y[2],mixValue)]);}return out; }
function emitBus() { const timestamp=Date.now(); const a=deterministicSceneState(SCENES[sceneA].id,settings.seed,timestamp,settings.palette); if(sceneA===sceneB){emitSceneFrame(window,a);return;} const b=deterministicSceneState(SCENES[sceneB].id,settings.seed,timestamp,settings.palette); emitSceneFrame(window,blendSceneStates(a,b,currentBlend)); }

function setupDebug() {
  const panel=document.querySelector('#debug'); panel.hidden=!debug; if(!debug)return;
  const sceneSelect=document.querySelector('#scene'); SCENES.forEach((s,i)=>sceneSelect.add(new Option(`${i+1}. ${s.name}`,s.id))); sceneSelect.value=SCENES[sceneB].id;
  sceneSelect.addEventListener('change',()=>startTransition(SCENES.findIndex((s)=>s.id===sceneSelect.value)));
  const seed=document.querySelector('#seed'); seed.value=String(settings.seed); seed.addEventListener('change',()=>{settings.seed=normalizeSeed(seed.value);renderer.setSeed(settings.seed);seed.value=String(settings.seed);});
  document.querySelector('#randomize').addEventListener('click',()=>{settings.seed=normalizeSeed(crypto.getRandomValues(new Uint32Array(1))[0]);renderer.setSeed(settings.seed);seed.value=String(settings.seed);});
  const pause=document.querySelector('#pause'); pause.addEventListener('click',()=>{settings.paused=!settings.paused;pause.textContent=settings.paused?'Resume':'Pause';});
  const auto=document.querySelector('#auto'); auto.checked=settings.autoRotate; auto.addEventListener('change',()=>settings.autoRotate=auto.checked);
  const quality=document.querySelector('#quality'); quality.value='auto'; quality.addEventListener('change',()=>{settings.qualityMode=quality.value;if(quality.value!=='auto')settings.renderScale=Number(quality.value);});
  const palette=document.querySelector('#palette'); palette.add(new Option('Scene default','')); Object.keys(PALETTE_FAMILIES).forEach((k)=>palette.add(new Option(k,k))); palette.value=settings.palette||''; palette.addEventListener('change',()=>{settings.palette=palette.value||null;renderer.setPalette(paletteFor(sceneB));});
}
function updateDebugSelection(){ if(!debug)return;document.querySelector('#scene').value=SCENES[sceneB].id; }
function updateTelemetry(){ if(!debug)return;document.querySelector('#telemetry').textContent=`${fps.toFixed(1)} FPS · ${avgFrame.toFixed(1)} ms · scale ${settings.renderScale.toFixed(3)} · ${SCENES[sceneB].id}`; }
function lerp(a,b,m){ return a+(b-a)*m; }
function clamp(value, low, high){ return Math.max(low,Math.min(high,value)); }
