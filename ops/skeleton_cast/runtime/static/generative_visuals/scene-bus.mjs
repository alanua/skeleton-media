export const SCENE_FRAME_EVENT = 'lavalamp:scene-frame';
export const SCENE_FRAME_SCHEMA = 'lavalamp.scene_frame.v1';

export function validateSceneFrame(frame) {
  if (!frame || typeof frame !== 'object') return false;
  if (frame.schema_version !== SCENE_FRAME_SCHEMA) return false;
  if (typeof frame.scene_id !== 'string' || !frame.scene_id) return false;
  if (!Number.isFinite(frame.timestamp_ms) || frame.timestamp_ms < 0) return false;
  if (!Number.isInteger(frame.seed) || frame.seed <= 0) return false;
  if (!Array.isArray(frame.palette) || frame.palette.length < 2 || frame.palette.length > 5) return false;
  for (const color of frame.palette) {
    if (!Array.isArray(color) || color.length !== 3 || color.some((v) => !inRange(v, 0, 1))) return false;
  }
  if (!['brightness', 'energy', 'tempo', 'phase', 'accent'].every((k) => inRange(frame[k], 0, 1))) return false;
  if (!frame.direction || !inRange(frame.direction.x, -1, 1) || !inRange(frame.direction.y, -1, 1)) return false;
  return frame.source === 'generative';
}

export function makeSceneFrame(state) {
  const frame = { schema_version: SCENE_FRAME_SCHEMA, ...state };
  if (!validateSceneFrame(frame)) throw new Error('invalid lavalamp.scene_frame.v1 payload');
  return frame;
}

export function emitSceneFrame(target, state) {
  const frame = makeSceneFrame(state);
  target.dispatchEvent(new CustomEvent(SCENE_FRAME_EVENT, { detail: frame }));
  return frame;
}

export function subscribeSceneFrames(target, callback) {
  const handler = (event) => callback(event.detail);
  target.addEventListener(SCENE_FRAME_EVENT, handler);
  return () => target.removeEventListener(SCENE_FRAME_EVENT, handler);
}

function inRange(value, low, high) {
  return Number.isFinite(value) && value >= low && value <= high;
}
