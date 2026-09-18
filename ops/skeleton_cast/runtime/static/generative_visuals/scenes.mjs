export const SCENES = Object.freeze([
  { id: 'infinite_layers', name: 'Infinite Layers', family: 'monochrome', complexity: 0.72 },
  { id: 'organic_sheet', name: 'Organic Sheet', family: 'monochrome', complexity: 0.78 },
  { id: 'topo_flow', name: 'Topo Flow', family: 'monochrome', complexity: 0.50 },
  { id: 'porous_sculpture', name: 'Porous Sculpture', family: 'monochrome', complexity: 0.82 },
  { id: 'reaction_diffusion', name: 'Reaction Diffusion', family: 'monochrome', complexity: 0.62 },
  { id: 'metaball_tunnel', name: 'Metaball Tunnel', family: 'cinematic', complexity: 0.78 },
  { id: 'prism_bloom', name: 'Prism Bloom', family: 'pastel', complexity: 0.76 },
  { id: 'spectral_flame', name: 'Spectral Flame', family: 'spectral', complexity: 0.72 },
  { id: 'accretion_horizon', name: 'Accretion Horizon', family: 'cinematic', complexity: 0.84 },
  { id: 'particle_veil', name: 'Particle Veil', family: 'pastel', complexity: 0.90 },
  { id: 'chromatic_glass', name: 'Chromatic Glass', family: 'spectral', complexity: 0.86 },
  { id: 'volumetric_loom', name: 'Volumetric Loom', family: 'pastel', complexity: 0.82 },
  { id: 'field_lines', name: 'Field Lines', family: 'copper', complexity: 0.68 },
]);

export const PALETTE_FAMILIES = Object.freeze({
  monochrome: [[0.015,0.018,0.022],[0.25,0.27,0.30],[0.72,0.75,0.78],[0.96,0.97,0.98]],
  pastel: [[0.05,0.07,0.09],[0.34,0.74,0.98],[0.98,0.52,0.72],[1.00,0.72,0.38],[0.94,0.94,0.88]],
  spectral: [[0.01,0.02,0.04],[0.05,0.78,1.00],[0.20,1.00,0.58],[1.00,0.88,0.12],[1.00,0.22,0.06]],
  cinematic: [[0.005,0.008,0.015],[0.05,0.25,0.70],[0.75,0.90,1.00],[1.00,0.38,0.04],[1.00,0.82,0.20]],
  copper: [[0.01,0.008,0.006],[0.22,0.05,0.03],[0.58,0.16,0.07],[0.92,0.43,0.16],[1.00,0.72,0.38]],
});

export function sceneById(id) {
  return SCENES.find((scene) => scene.id === id) || null;
}

export function normalizeSeed(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 1;
  return (Math.floor(Math.abs(n)) >>> 0) || 1;
}

function hash01(seed, salt) {
  let x = (normalizeSeed(seed) ^ Math.imul((salt + 1) >>> 0, 0x9e3779b1)) >>> 0;
  x ^= x >>> 16;
  x = Math.imul(x, 0x7feb352d) >>> 0;
  x ^= x >>> 15;
  x = Math.imul(x, 0x846ca68b) >>> 0;
  x ^= x >>> 16;
  return x / 4294967295;
}

export function deterministicSceneState(sceneId, seed, timestampMs, paletteOverride = null) {
  const sceneIndex = SCENES.findIndex((scene) => scene.id === sceneId);
  if (sceneIndex < 0) throw new Error(`unknown scene: ${sceneId}`);
  const scene = SCENES[sceneIndex];
  const safeSeed = normalizeSeed(seed);
  const t = Math.max(0, Number(timestampMs) || 0) / 1000;
  const slow = t * (0.006 + hash01(safeSeed, sceneIndex) * 0.004);
  const phase = ((slow + hash01(safeSeed, sceneIndex + 11)) % 1 + 1) % 1;
  const waveA = Math.sin((slow * 6.2831853) + hash01(safeSeed, sceneIndex + 20) * 6.2831853);
  const waveB = Math.cos((slow * 4.3982297) + hash01(safeSeed, sceneIndex + 30) * 6.2831853);
  const directionAngle = slow * 1.618 + hash01(safeSeed, sceneIndex + 40) * 6.2831853;
  const family = paletteOverride && Object.hasOwn(PALETTE_FAMILIES, paletteOverride) ? paletteOverride : scene.family;
  return {
    scene_id: scene.id,
    timestamp_ms: Math.floor(Math.max(0, Number(timestampMs) || 0)),
    seed: safeSeed,
    palette: PALETTE_FAMILIES[family].map((rgb) => [...rgb]),
    brightness: clamp01(0.50 + 0.22 * waveA + 0.08 * scene.complexity),
    energy: clamp01(0.34 + 0.30 * scene.complexity + 0.12 * waveB),
    tempo: clamp01(0.18 + 0.38 * scene.complexity),
    phase,
    direction: { x: Math.cos(directionAngle), y: Math.sin(directionAngle) },
    accent: clamp01(0.48 + 0.36 * Math.sin(directionAngle * 0.73 + sceneIndex)),
    source: 'generative',
  };
}

export function blendSceneStates(a, b, mixValue) {
  if (!a || !b || a.seed !== b.seed || a.timestamp_ms !== b.timestamp_ms) {
    throw new Error('scene states must share seed and timestamp');
  }
  const m = clamp01(Number(mixValue) || 0);
  if (m <= 0) return structuredCloneState(a);
  if (m >= 1) return structuredCloneState(b);
  return {
    scene_id: m < 0.5 ? a.scene_id : b.scene_id,
    timestamp_ms: a.timestamp_ms,
    seed: a.seed,
    palette: blendPalettes(a.palette, b.palette, m),
    brightness: lerp(a.brightness, b.brightness, m),
    energy: lerp(a.energy, b.energy, m),
    tempo: lerp(a.tempo, b.tempo, m),
    phase: blendPhase(a.phase, b.phase, m),
    direction: {
      x: lerp(a.direction.x, b.direction.x, m),
      y: lerp(a.direction.y, b.direction.y, m),
    },
    accent: lerp(a.accent, b.accent, m),
    source: 'generative',
  };
}

function blendPalettes(a, b, m) {
  const count = Math.max(a.length, b.length);
  return Array.from({ length: count }, (_, index) => {
    const x = a[Math.min(index, a.length - 1)];
    const y = b[Math.min(index, b.length - 1)];
    return [lerp(x[0], y[0], m), lerp(x[1], y[1], m), lerp(x[2], y[2], m)];
  });
}

function blendPhase(a, b, m) {
  let delta = b - a;
  if (delta > 0.5) delta -= 1;
  if (delta < -0.5) delta += 1;
  return ((a + delta * m) % 1 + 1) % 1;
}

function structuredCloneState(state) {
  return {
    ...state,
    palette: state.palette.map((rgb) => [...rgb]),
    direction: { ...state.direction },
  };
}

function lerp(a, b, m) {
  return a + (b - a) * m;
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}
