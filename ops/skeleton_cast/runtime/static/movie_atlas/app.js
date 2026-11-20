(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const {transport, SessionSync, visibleItems, centerCamera} = MovieAtlasState;
  const api = transport(fetch.bind(window));
  const role = location.pathname === '/movie-atlas/display' ? 'display' : 'controller';
  const controller = role === 'controller';
  const storageKey = 'movie-atlas:' + role;
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const canRotate = CSS.supports('perspective', '1000px') && CSS.supports('transform-style', 'preserve-3d');
  let sync, expiresAt, timer, request, frame, transitioning = false, displayBlocked = false;
  let posterKey, catalogKey, flat = false, drag, suppressClick = false;

  document.body.dataset.role = role;
  $('join').hidden = controller;
  $('controllerPair').hidden = !controller;
  $('tools').hidden = !controller;
  $('displayLink').hidden = !controller;
  $('selectionActions').hidden = !controller;
  $('fullscreen').hidden = controller || !document.fullscreenEnabled;
  $('exit').textContent = controller ? 'Закрити сесію' : 'Від’єднати екран';

  function showError(message = '') {
    $('error').textContent = message;
    $('error').hidden = !message;
  }

  function save(pending) {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({
        ...sync.capability, expiresAt, pending,
      }));
    } catch {
      showError('Ця вкладка не може зберегти підключення для перезавантаження.');
    }
  }

  function forget() {
    clearTimeout(timer);
    sync = null;
    displayBlocked = false;
    try { sessionStorage.removeItem(storageKey); } catch {}
    render();
  }

  function handleError(error) {
    if (error.status === 410 || error.status === 403) forget();
    if (sync && !controller) displayBlocked = true;
    showError(error.name !== 'AbortError' && error.name !== 'TypeError' ? error.message : 'Зв’язок перервано. Перевірте підключення.');
    if (sync) $('connection').textContent = 'Відновлюємо зв’язок';
    render();
  }

  function setSceneMode() {
    const rotate = canRotate && !flat && !motion.matches;
    document.body.classList.toggle('scene', rotate);
    $('cameraHint').hidden = !rotate || !controller;
    $('cameraTools').hidden = !rotate;
    $('flat').hidden = !canRotate || motion.matches;
    $('flat').setAttribute('aria-pressed', String(flat));
  }

  function renderOptions(catalog) {
    const genres = [...new Set(catalog.flatMap(item => item.genres || []))].sort((a, b) => a.localeCompare(b, 'uk'));
    const years = [...new Set(catalog.map(item => item.year).filter(Boolean))].sort();
    const key = JSON.stringify([genres, years]);
    if (key === catalogKey) return;
    catalogKey = key;
    $('genre').replaceChildren(new Option('Усі жанри', ''), ...genres.map(genre => new Option(genre, genre)));
    for (const id of ['yearMin', 'yearMax']) {
      $(id).replaceChildren(new Option('Будь-який', ''), ...years.map(year => new Option(year, year)));
    }
  }

  function renderPosters(items, selectedId) {
    const key = JSON.stringify(items);
    if (key !== posterKey) {
      posterKey = key;
      $('posters').replaceChildren(...items.map((item, index) => {
        const row = document.createElement('li');
        row.style.setProperty('--depth', (24 - (index % 6) * 10) + 'px');
        const poster = document.createElement(controller ? 'button' : 'div');
        poster.className = 'poster';
        poster.dataset.id = item.id;
        const cover = document.createElement(item.poster ? 'img' : 'div');
        cover.className = 'cover';
        if (item.poster) {
          cover.alt = '';
          cover.loading = 'lazy';
          cover.decoding = 'async';
          cover.src = item.poster;
          cover.addEventListener('error', () => cover.replaceWith(Object.assign(document.createElement('div'), {className: 'cover'})));
        }
        const title = document.createElement('span');
        title.textContent = item.title;
        poster.append(cover, title);
        if (item.year) {
          const year = document.createElement('time');
          year.textContent = item.year;
          poster.append(year);
        }
        row.append(poster);
        return row;
      }));
    }
    for (const poster of $('posters').querySelectorAll('.poster')) {
      const selected = poster.dataset.id === selectedId;
      poster.classList.toggle('selected', selected);
      if (controller) poster.setAttribute('aria-pressed', String(selected));
    }
  }

  function render() {
    const snapshot = sync?.snapshot;
    $('tools').hidden = !controller || !snapshot;
    $('pair').hidden = Boolean(sync);
    $('atlas').hidden = !sync;
    for (const id of ['viewport', 'detail', 'empty', 'flat', 'fullscreen']) {
      if (displayBlocked || !snapshot) $(id).hidden = true;
    }
    if (!sync) $('connection').textContent = 'Не під’єднано';
    if (document.hidden || displayBlocked || !snapshot) {
      $('pairCode').textContent = sync?.capability.code || '';
      return;
    }
    $('viewport').hidden = false;
    $('detail').hidden = false;
    $('fullscreen').hidden = controller || !document.fullscreenEnabled;
    setSceneMode();
    const {catalog} = snapshot;
    const state = sync.state;
    const currentPeer = controller ? snapshot.display_connected : snapshot.controller_connected;
    $('connection').textContent = !$('error').hidden ? 'Проблема синхронізації' : currentPeer
      ? 'Під’єднано' : controller && !snapshot.paired ? 'Очікуємо екран' : 'Очікуємо відновлення зв’язку';
    $('pairCode').textContent = snapshot.code;
    if (controller) {
      renderOptions(catalog);
      for (const [id, value] of Object.entries({
        query: state.query, genre: state.genre, sort: state.sort, yearMin: state.year_min, yearMax: state.year_max,
      })) {
        if ($(id).value !== String(value ?? '')) $(id).value = value ?? '';
      }
    }
    const selected = catalog.find(item => item.id === state.selected_id);
    $('selectedTitle').textContent = selected?.title || 'Фільм не вибрано';
    const meta = selected ? [selected.year, ...(selected.genres || [])].filter(Boolean).join(' · ') : '';
    $('selectedMeta').textContent = meta;
    $('selectedMeta').hidden = !meta;
    $('clearSelection').disabled = !selected;
    $('choose').disabled = !selected;
    const items = visibleItems(catalog, state);
    renderPosters(items, state.selected_id);
    $('empty').hidden = items.length > 0;
    $('empty').textContent = catalog.length ? 'За цими фільтрами фільмів немає.' : 'Каталог порожній.';
    const {yaw, pitch, zoom} = state.camera;
    $('posters').style.setProperty('--yaw', yaw + 'deg');
    $('posters').style.setProperty('--pitch', pitch + 'deg');
    $('posters').style.setProperty('--zoom', zoom);
  }

  function schedule(delay) {
    clearTimeout(timer);
    if (!sync || transitioning || request || document.hidden) return;
    timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (transitioning || request || document.hidden || !sync) return;
    if (Date.now() >= expiresAt) {
      forget();
      showError('Термін дії сесії минув.');
      return;
    }
    let delay = 1600;
    request = sync.step();
    try {
      await request;
      expiresAt = Date.now() + sync.snapshot.expires_in * 1000;
      displayBlocked = false;
      if (!Object.keys(sync.pending).length) showError();
      save(sync.pending);
      render();
      if (Object.keys(sync.pending).length) delay = 100;
    } catch (error) {
      handleError(error);
      if (error.code === 'revision_conflict') delay = 180;
    } finally {
      request = null;
      schedule(delay);
    }
  }

  function start(capability, snapshot = null, pending = {}) {
    sync = new SessionSync(api, capability, snapshot, pending, save);
    render();
    schedule(snapshot ? 1600 : 0);
  }

  async function pair(open) {
    if (transitioning || sync) return;
    transitioning = true;
    showError();
    try {
      const {controller_token, display_token, ...snapshot} = await open();
      expiresAt = Date.now() + snapshot.expires_in * 1000;
      start({code: snapshot.code, role, token: controller ? controller_token : display_token}, snapshot);
      save({});
    } catch (error) {
      handleError(error);
    } finally {
      transitioning = false;
      schedule(1600);
    }
  }

  function change(patch) {
    if (!sync?.snapshot || !controller || transitioning) return;
    const idle = !Object.keys(sync.pending).length;
    sync.change(patch);
    if (!frame && !document.hidden) {
      frame = setTimeout(() => {
        frame = null;
        render();
      }, 50);
    }
    if (idle) schedule(100);
  }

  async function leave(destination, handoff = false) {
    if (transitioning) return;
    transitioning = true;
    clearTimeout(timer);
    try {
      // Let the in-flight revision settle before resolving the selected film.
      await request?.catch(() => {});
      if (handoff && sync) {
        for (let remaining = 5; remaining && (sync.refresh || Object.keys(sync.pending).length); remaining--) {
          try { await sync.step(); }
          catch (error) { if (error.code !== 'revision_conflict') throw error; }
        }
        if (sync.refresh || Object.keys(sync.pending).length) throw new Error('Вибір ще синхронізується.');
        const result = await api.handoff(sync.capability);
        destination = result.selection_url;
      }
      if (sync && controller) {
        try { await api.close(sync.capability); }
        catch (error) { if (error.status !== 410) throw error; }
      }
      forget();
      showError();
      if (destination) location.assign(destination);
    } catch (error) {
      handleError(error);
    } finally {
      transitioning = false;
      schedule(1600);
    }
  }

  function setYear(key, other, event) {
    const value = event.target.value ? Number(event.target.value) : null;
    const patch = {[key]: value};
    const bound = sync.state[other];
    if (value !== null && bound !== null && (key === 'year_min' ? value > bound : value < bound)) patch[other] = value;
    change(patch);
  }

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  function rotate(camera, yaw, pitch) {
    change({camera: {...camera, yaw: clamp(yaw, -55, 55), pitch: clamp(pitch, -35, 35)}});
  }
  function zoom(delta) {
    const camera = sync.state.camera;
    change({camera: {...camera, zoom: clamp(camera.zoom + delta, .65, 1.6)}});
  }

  $('create').onclick = () => pair(api.create);
  $('join').onsubmit = event => {
    event.preventDefault();
    pair(() => api.join($('code').value.trim().toUpperCase()));
  };
  $('back').onclick = event => {
    if (sync) {
      event.preventDefault();
      leave($('back').getAttribute('href'));
    }
  };
  $('exit').onclick = () => leave(controller ? $('back').getAttribute('href') : null);
  $('choose').onclick = () => leave(null, true);
  $('clearSelection').onclick = () => change({selected_id: null});
  $('query').oninput = event => change({query: event.target.value});
  $('genre').onchange = event => change({genre: event.target.value});
  $('sort').onchange = event => change({sort: event.target.value});
  $('yearMin').onchange = event => setYear('year_min', 'year_max', event);
  $('yearMax').onchange = event => setYear('year_max', 'year_min', event);
  $('zoomIn').onclick = () => zoom(.15);
  $('zoomOut').onclick = () => zoom(-.15);
  $('resetCamera').onclick = () => change({camera: {...centerCamera}});
  $('flat').onclick = () => { flat = !flat; setSceneMode(); };
  $('fullscreen').onclick = () => {
    const action = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    action.catch(() => showError('Повноекранний режим недоступний.'));
  };
  $('posters').onclick = event => {
    const poster = event.target.closest('button[data-id]');
    if (poster && (!suppressClick || event.detail === 0)) change({selected_id: poster.dataset.id});
    suppressClick = false;
  };

  const viewport = $('viewport');
  viewport.onpointerdown = event => {
    suppressClick = false;
    if (!controller || !document.body.classList.contains('scene') || !event.isPrimary || event.button !== 0) return;
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, camera: sync.state.camera};
  };
  viewport.onpointermove = event => {
    if (drag?.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!suppressClick && Math.hypot(dx, dy) < 6) return;
    suppressClick = true;
    viewport.setPointerCapture(drag.id);
    viewport.classList.add('dragging');
    rotate(drag.camera, drag.camera.yaw + dx * .28, drag.camera.pitch - dy * .28);
  };
  function endDrag(event) {
    if (drag?.id !== event.pointerId) return;
    drag = null;
    viewport.classList.remove('dragging');
  }
  viewport.onpointerup = endDrag;
  viewport.onpointercancel = endDrag;
  viewport.onlostpointercapture = endDrag;
  viewport.onpointerleave = event => { if (!suppressClick) endDrag(event); };
  viewport.onkeydown = event => {
    if (!controller || event.target !== viewport || !document.body.classList.contains('scene')) return;
    const delta = {ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, 5], ArrowDown: [0, -5]}[event.key];
    if (!delta) return;
    event.preventDefault();
    const camera = sync.state.camera;
    rotate(camera, camera.yaw + delta[0], camera.pitch + delta[1]);
  };

  motion.addEventListener('change', setSceneMode);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) schedule(0);
    else if (!controller && sync) {
      displayBlocked = true;
      render();
    }
  });
  addEventListener('pagehide', () => clearTimeout(timer));
  addEventListener('pageshow', () => schedule(0));

  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    if (saved?.role === role && /^[A-Z2-9]{6}$/.test(saved.code) && /^[A-Za-z0-9_-]{24,80}$/.test(saved.token)) {
      expiresAt = saved.expiresAt;
      if (Date.now() < expiresAt) start({code: saved.code, role, token: saved.token}, null, saved.pending);
      else {
        forget();
        showError('Термін дії сесії минув.');
      }
    }
  } catch {
    showError('Не вдалося відновити підключення цієї вкладки.');
  }
})();
