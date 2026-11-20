'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {transport, SessionSync, visibleItems} = require('../../ops/skeleton_cast/runtime/static/movie_atlas/state.js');

const movie = {id: 'one', title: 'Фільм', year: '2024', genres: ['Драма']};
const state = {
  query: '', sort: 'recent', genre: '', year_min: null, year_max: null, selected_id: 'one',
  camera: {yaw: 0, pitch: 0, zoom: 1},
};
const controller = {role: 'controller', code: 'ABC234', token: 'controller-fixture'};
const display = {...controller, role: 'display', token: 'display-fixture'};

test('selection, search, genre and year filters, sorting, and visible limit', () => {
  const catalog = [movie, {id: 'two', title: 'Архів', year: '2020', genres: ['Драма']}, {id: 'three', title: 'Комедія'}];
  assert.deepEqual(visibleItems(catalog, {...state, sort: 'title'}).map(x => x.id), ['two', 'three', 'one']);
  assert.deepEqual(visibleItems(catalog, {...state, sort: 'year'}).map(x => x.id), ['one', 'two', 'three']);
  assert.deepEqual(visibleItems(catalog, {...state, query: 'ФІЛ', genre: 'Драма', year_min: 2022, year_max: 2024}), [movie]);
  assert.deepEqual(visibleItems(catalog, {...state, genre: 'Драма', year_max: 2019}), []);
  assert.equal(visibleItems(Array(240).fill(movie), state).length, 24);
});

test('coalesced updates survive conflicts, outage and reload with separate credentials', async () => {
  let snapshot = {code: controller.code, revision: 1, state};
  let conflict = false, offline = false, release;
  const calls = [];
  const api = transport(async (url, options) => {
    const {headers, method, body} = options;
    calls.push({url, method, token: headers['X-Atlas-Token']});
    if (offline) throw new TypeError('offline');
    if (method === 'PUT') {
      const {revision, state: patch} = JSON.parse(body);
      assert.equal(headers['X-Atlas-Token'], controller.token);
      if (conflict) {
        conflict = false;
        snapshot = {...snapshot, revision: snapshot.revision + 1, state: {...snapshot.state, genre: 'Драма'}};
        return {ok: false, status: 409, json: async () => ({code: 'revision_conflict'})};
      }
      if (patch.query === 'first') await new Promise(resolve => { release = resolve; });
      assert.equal(revision, snapshot.revision);
      snapshot = {...snapshot, revision: revision + 1, state: {...snapshot.state, ...patch}};
    }
    return {ok: true, json: async () => snapshot};
  });
  let saved;
  const phone = new SessionSync(api, controller, snapshot, {}, pending => { saved = JSON.stringify(pending); });
  phone.change({query: 'first'});
  const inflight = phone.step();
  phone.change({query: 'second'});
  phone.change({query: 'final', selected_id: null});
  release();
  await inflight;
  assert.equal(phone.state.query, 'final');
  assert.equal(phone.state.selected_id, null);

  conflict = true;
  await assert.rejects(phone.step(), {status: 409});
  offline = true;
  await assert.rejects(phone.step(), TypeError);
  offline = false;
  const reloaded = new SessionSync(api, controller, null, JSON.parse(saved));
  await reloaded.step();
  await reloaded.step();
  reloaded.change({camera: {yaw: 15, pitch: 5, zoom: 1.2}});
  await reloaded.step();
  reloaded.change({sort: 'year'});
  await reloaded.step();
  assert.equal(reloaded.snapshot.state.query, 'final');
  assert.equal(reloaded.snapshot.state.genre, 'Драма');
  assert.equal(reloaded.snapshot.state.selected_id, null);
  assert.equal(reloaded.snapshot.state.camera.yaw, 15);

  const screen = new SessionSync(api, display);
  screen.change({query: 'must not write'});
  await screen.step();
  await screen.step();
  assert.deepEqual(screen.snapshot, reloaded.snapshot);
  assert.ok(calls.slice(0, -2).every(call => call.token === controller.token));
  assert.ok(calls.slice(-2).every(call => call.method === 'GET' && call.token === display.token && call.url.endsWith('?role=display')));
});

test('a rejected patch reports the error and retains newer input', async () => {
  const initial = {revision: 1, state};
  let reject;
  const api = {
    read: async () => initial,
    update: async (_capability, _revision, patch) => {
      if (patch.selected_id === 'bad') return new Promise((_resolve, failed) => { reject = failed; });
      return {revision: 2, state: {...state, ...patch}};
    },
  };
  const phone = new SessionSync(api, controller, initial);
  phone.change({selected_id: 'bad', query: 'old'});
  const inflight = phone.step();
  phone.change({query: 'new'});
  reject(Object.assign(new Error('invalid'), {status: 400}));
  await assert.rejects(inflight, {status: 400});
  assert.deepEqual(phone.pending, {query: 'new'});
  await phone.step();
  await phone.step();
  assert.equal(phone.snapshot.state.query, 'new');
  assert.equal(phone.snapshot.state.selected_id, 'one');
});
