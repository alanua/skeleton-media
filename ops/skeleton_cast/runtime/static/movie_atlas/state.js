(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MovieAtlasState = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const MAX_VISIBLE = 24;
  const centerCamera = {yaw: 0, pitch: 0, zoom: 1};

  function visibleItems(catalog, state) {
    const query = state.query.trim().toLocaleLowerCase('uk');
    const items = catalog.filter(item =>
      item.title.toLocaleLowerCase('uk').includes(query)
      && (!state.genre || item.genres?.includes(state.genre))
      && (state.year_min === null || (item.year && Number(item.year) >= state.year_min))
      && (state.year_max === null || (item.year && Number(item.year) <= state.year_max))
    );
    if (state.sort === 'title') items.sort((a, b) => a.title.localeCompare(b.title, 'uk'));
    if (state.sort === 'year') items.sort((a, b) => Number(b.year || 0) - Number(a.year || 0));
    return items.slice(0, MAX_VISIBLE);
  }

  function transport(fetchImpl) {
    async function call(path, method = 'GET', token, data) {
      const headers = {};
      if (token) headers['X-Atlas-Token'] = token;
      if (data) headers['Content-Type'] = 'application/json';
      const abort = new AbortController();
      const timeout = setTimeout(() => abort.abort(), 8000);
      try {
        const response = await fetchImpl('/api/movie-atlas' + path, {
          method, headers, body: data ? JSON.stringify(data) : undefined,
          credentials: 'same-origin', cache: 'no-store', signal: abort.signal,
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(body.error || 'Не вдалося зв’язатися з Кіноатласом.');
          error.status = response.status;
          error.code = body.code;
          throw error;
        }
        return body;
      } finally {
        clearTimeout(timeout);
      }
    }
    const path = code => '/sessions/' + encodeURIComponent(code);
    return {
      create: () => call('/sessions', 'POST'),
      join: code => call('/join', 'POST', null, {code}),
      read: ({code, role, token}) => call(path(code) + '?role=' + role, 'GET', token),
      update: ({code, token}, revision, state) => call(path(code), 'PUT', token, {revision, state}),
      close: ({code, token}) => call(path(code), 'DELETE', token),
      handoff: ({code, token}) => call(path(code) + '/handoff', 'POST', token),
    };
  }

  class SessionSync {
    constructor(api, capability, snapshot = null, pending = {}, save = () => {}) {
      this.api = api;
      this.capability = capability;
      this.snapshot = snapshot;
      this.pending = capability.role === 'controller' ? pending : {};
      this.save = save;
      this.refresh = !snapshot;
      this.busy = false;
    }

    get state() {
      return this.snapshot && {...this.snapshot.state, ...this.pending};
    }

    change(patch) {
      if (this.capability.role !== 'controller') return;
      this.pending = {...this.pending, ...patch};
      this.save(this.pending);
    }

    acknowledge(sent) {
      for (const key of Object.keys(sent)) {
        if (this.pending[key] === sent[key]) delete this.pending[key];
      }
      this.save(this.pending);
    }

    async step() {
      if (this.busy) return;
      this.busy = true;
      const sent = {...this.pending};
      try {
        if (this.refresh || !Object.keys(sent).length) {
          this.snapshot = await this.api.read(this.capability);
          this.refresh = false;
        } else {
          this.snapshot = await this.api.update(this.capability, this.snapshot.revision, sent);
          this.acknowledge(sent);
        }
      } catch (error) {
        this.refresh = true;
        if (error.status === 400) this.acknowledge(sent);
        throw error;
      } finally {
        this.busy = false;
      }
    }
  }

  return {centerCamera, visibleItems, transport, SessionSync};
});
