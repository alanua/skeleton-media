# Home Edge generative visuals

Local-first WebGL2 renderer for the Home Edge screensaver path. It has no runtime dependencies, remote assets, analytics, fetch/XHR/WebSocket calls, or device-control writes.

Run from repository root:

```sh
python3 -m http.server 8765
```

Open `http://127.0.0.1:8765/home_edge/generative_visuals/`. Add `?debug=1` for local controls. Optional query values: `scene=<id>`, `seed=<uint32>`, `palette=<family>`, `rotate=0`, `dwell=<milliseconds>` (clamped to 5-15 minutes), `transition=<milliseconds>` (clamped to 2-4 seconds), `scale=0.5..1`.

The renderer emits `lavalamp:scene-frame` at 20 Hz with schema `lavalamp.scene_frame.v1`. Consumers can subscribe without network access through `window.lavalampSceneBus.subscribe(callback)`.

Adaptive quality uses moving average frame time and hysteresis. It lowers internal render scale after sustained frame times above ~34 ms and recovers slowly after sustained headroom. Scene complexity is also reduced as render scale falls. Hidden tabs stop rendering work, and reduced-motion mode lowers animation speed.
