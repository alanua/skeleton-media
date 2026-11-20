# Кіноатлас

Open **Кіноатлас** in the video selector and create a session on the phone. Open the provided display page on a trusted large-screen browser and enter the six-character code. The phone controls search, genre and year range, order, selected film, and camera. Drag the reel with touch or mouse, or focus it and use the arrow keys. Both roles can switch to a flat view locally, and reduced-motion settings select that view automatically.

**Закрити сесію** or **← Відео** closes the controller session and restores the prior selection in the video page. **До вибору відео** resolves the selected local job, closes the pair, and opens the regular video selector. Playback still requires its explicit existing action. Opening Atlas, joining a display, or closing a session has no playback or device-mode command.

## Source and limits

- Atlas uses the extracted service's endpoint TV history and existing jobs and cached posters. It reads the legacy history only when the endpoint file is absent. This path never writes a history migration. The projection excludes hidden, deleted, duplicate and missing records, and contains opaque item IDs, titles, years, genres and safe local poster paths. Film handoff and selector load independently recheck current visibility and job identity.
- The in-process store accepts up to 12 concurrent pairs with a fixed 30-minute lifetime, 20 write attempts per second per controller, and 24 pairing attempts per 10 seconds across the process. Browser writes require the page's exact origin. Each tab stores its own role capability and the controller's unsent changes in session storage. A service restart ends the sessions.
- The display confirms ownership on join and on each refresh using `home-edge-media-display-owner status`. Atlas admits an explicit display-off decision or confirmed stopped desktop playback. Paused foreground playback, ambiguous Android idle results, missing helpers and failed probes suppress the scene. This leaves the owner helper's existing screensaver policy intact. The display hides its scene when refresh fails and resumes from the latest snapshot when the guard and connection recover. A peer stops counting as connected after 12 seconds without contact.
- CSS perspective, yaw, pitch and scale render the poster reel at native browser resolution. There is no offscreen canvas or custom pixel-ratio allocation. The projection is capped at 240 films, and the scene at 24 posters. Cached images are checked for a 960 × 960 maximum, a 512 KiB file limit and one frame. Local input redraws are coalesced at 50 ms, and hidden tabs suspend scheduled polling and drawing.

## Review commands

Run in an isolated synthetic environment from the repository root.

```sh
pytest -q tests/test_movie_atlas.py
node tests/movie_atlas/state.test.js
pytest -q tests/movie_atlas/test_flask_routes.py
pytest -q tests/movie_atlas/browser_checks.py
```

Normal pytest discovery includes the Flask route tests, which require the repository's runtime dependencies. Run the two-client browser checks explicitly with Python Playwright and a provisioned Chromium. They start the actual Flask app on loopback with temporary synthetic catalog files, an injected owner response and intercepted playback endpoints. This checkout uses the extracted app's local history and job files. The live Home Edge build has separate video-selection and shared-history integration. Verify its deletion and visibility rules, prior selection restoration, paused/active ownership, and phone/TV reconnect and display performance before any separately approved runtime cutover.
