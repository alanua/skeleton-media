"""Bounded HLS recovery transaction. Callers supply endpoint-locked adapters.
No threads, timers, network access or automatic activation are created here.
"""
from copy import deepcopy
import math
import threading


class RecoveryCancelled(RuntimeError):
    pass


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


IDENTITY = ('endpoint', 'mode', 'session', 'job_id', 'source_id',
            'season', 'episode', 'voice', 'sid', 'aid', 'sub_visibility')


def same(before, after):
    return all(before.get(k) == after.get(k) for k in IDENTITY)


class HLSRecovery:
    """One attempt per invocation; two attempts per session, >=60s apart.

    snapshot() is authoritative and freshly collected. mutation_guard() must be
    the same lock used by endpoint mode mutations. command() must implement the
    reviewed expected_revision fence. resolve() receives a detached job and a
    finite timeout and must never persist it. commit() is revision-conditional.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._attempts = {}

    def run(self, *, job, source, snapshot, revision, resolve, command,
            mutation_guard, commit, clock, sleep, transport_options=None):
        if not self._lock.acquire(blocking=False):
            return {'state': 'busy', 'loaded': False}
        loaded = False
        try:
            first = snapshot()
            rev = revision()
            pos = number(first.get('position'))
            if (type(rev) is not int or rev < 0 or first.get('mode') != 'mpv'
                    or first.get('endpoint') != 'home_edge_01'
                    or first.get('pause') is not False
                    or first.get('seeking') is not False
                    or first.get('paused_for_cache') is not True
                    or first.get('live') is not False
                    or not first.get('session') or not first.get('job_id')
                    or not first.get('source_id') or pos is None or pos < 0
                    or first.get('job_id') != job.get('id')
                    or first.get('source_id') != source.get('source_id')
                    or source.get('has_drm') or source.get('drm')
                    or not str(source.get('url', '')).startswith(('https://', 'http://'))
                    or source.get('kind') != 'hls'):
                return {'state': 'ineligible', 'loaded': False}
            # Two independent starvation samples: a single buffering sample is
            # insufficient. An operator intent at any point invalidates work.
            def check(current, *, after_load=False, staged=False):
                p = number(current.get('position'))
                if (revision() != rev or not same(first, current)
                        or (current.get('pause') is not False and not (staged and current.get('pause') is True))
                        or current.get('seeking') is not False or p is None
                        or (not after_load and abs(p-pos) > .5)):
                    raise RecoveryCancelled()
                return p
            sleep(3)
            check(snapshot())
            second = snapshot()
            check(second)
            if second.get('paused_for_cache') is not True:
                return {'state': 'no_longer_starved', 'loaded': False}
            key = (first['endpoint'], first['session'])
            now = clock()
            count, previous = self._attempts.get(key, (0, float('-inf')))
            if count >= 2:
                return {'state': 'retry_exhausted', 'loaded': False}
            if now - previous < 60:
                return {'state': 'cooldown', 'loaded': False}
            # Failed resolve also consumes an attempt. Bounded state lifetime:
            # stop admitting new sessions at the capacity bound.
            if key not in self._attempts and len(self._attempts) >= 128:
                return {'state': 'session_capacity', 'loaded': False}
            self._attempts[key] = (count+1, now)
            detached = deepcopy(job)
            fresh = resolve(detached, deepcopy(source), timeout=12)
            if (not isinstance(fresh, dict) or not isinstance(fresh.get('url'), str)
                    or not fresh['url'].startswith(('https://', 'http://'))
                    or fresh.get('has_drm') or fresh.get('drm')
                    or fresh.get('is_trailer') or fresh.get('live')
                    or fresh.get('season', first.get('season')) != first.get('season')
                    or fresh.get('episode', first.get('episode')) != first.get('episode')
                    or str(fresh.get('group') or fresh.get('translation') or '') != first.get('voice')):
                raise RecoveryCancelled()
            # No ordinary play()/stop() path: those may perform cold start,
            # select another voice or unpause after the operator has paused.
            # Transport options must be supplied by the reviewed adapter, never
            # taken as arbitrary MPV properties from resolver data.
            options = dict(transport_options(fresh) if transport_options else {})
            if set(options) - {'referrer', 'user-agent', 'http-header-fields'}:
                raise RecoveryCancelled()
            options.update({'start': str(pos), 'pause': 'yes',
                            'sid': str(first.get('sid', 'no')),
                            'aid': str(first.get('aid', 'auto')),
                            'sub-visibility': 'yes' if first.get('sub_visibility') else 'no'})
            def send(parts):
                result = command(parts, timeout=2, expected_revision=rev)
                if not isinstance(result, dict) or result.get('error') != 'success':
                    raise RuntimeError('MPV rejected recovery command')
            with mutation_guard():
                check(snapshot())
                send(['loadfile', fresh['url'], 'replace', -1, options])
                loaded = True
            # Load is staged paused; never resume before exact path validation.
            deadline = clock()+8
            while True:
                current = snapshot()
                # The load's staged pause is allowed only under the captured
                # revision. Operator pause invalidates that revision at entry.
                check(current, after_load=True, staged=True)
                if current.get('path') == fresh['url'] and current.get('loaded') is True:
                    if current.get('pause') is not True or abs(number(current.get('position'))-pos) > 2:
                        return {'state': 'preservation_unverified', 'loaded': True}
                    break
                if clock() >= deadline:
                    return {'state': 'load_unverified', 'loaded': True}
                sleep(.2)
            with mutation_guard():
                staged = snapshot()
                staged_pos = check(staged, after_load=True, staged=True)
                if (staged.get('path') != fresh['url'] or staged.get('loaded') is not True
                        or staged.get('pause') is not True or abs(staged_pos-pos) > 2):
                    return {'state': 'preservation_unverified', 'loaded': True}
                send(['set_property', 'pause', False])
            start = snapshot()
            check(start, after_load=True)
            started = clock()
            sleep(3)
            end = snapshot()
            end_pos = check(end, after_load=True)
            start_pos = number(start.get('position'))
            f0, f1 = number(start.get('frames')), number(end.get('frames'))
            elapsed = clock() - started
            if (elapsed < 3 or elapsed > 10 or start_pos is None or end_pos-start_pos < .5
                    or end_pos-start_pos > max(12, elapsed*2)
                    or abs(start_pos-pos) > 2 or f0 is None or f1 is None or f1 <= f0
                    or end.get('paused_for_cache') is not False
                    or end.get('path') != fresh['url']):
                return {'state': 'progress_unverified', 'loaded': True}
            with mutation_guard():
                final = snapshot()
                final_pos = check(final, after_load=True)
                if (final.get('path') != fresh['url'] or final.get('loaded') is not True
                        or final.get('paused_for_cache') is not False
                        or final_pos < end_pos-.05 or final_pos > end_pos+2):
                    return {'state': 'progress_unverified', 'loaded': True}
                commit(detached, fresh, expected_revision=rev)
            return {'state': 'recovered', 'loaded': True, 'attempt': count+1}
        except RecoveryCancelled:
            return {'state': 'cancelled', 'loaded': loaded}
        except Exception:
            # No raw resolver/MPV exception (may contain signed stream URLs).
            return {'state': 'failed', 'loaded': loaded}
        finally:
            self._lock.release()
