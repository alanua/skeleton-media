"""Offline regression contract for deployed Home history pagination and preference cache.

The only way to run the full baseline parity test is with a private copy of the
exact active runtime app.py. This file uses synthetic source instead.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import threading
import types
import hashlib

import pytest

from review.home_history_paged_prefcache_20261124 import (
    OLD_PREF, OLD_GET_ANCHOR, patch_source,
)


def fixture_source() -> str:
    return """
import json
import threading
import time
""" + OLD_PREF + """

class FakeApp:
    def get(self, x):
        def decorator(fn):
            return fn
        return decorator
app = FakeApp()
def _require():
    return None
def _history_response_signature():
    return ('synthetic',)
class Response:
    pass
""" + OLD_GET_ANCHOR + """
    _require()
    signature = _history_response_signature()
    now_mono = time.monotonic()
    with _HISTORY_RESPONSE_CACHE_LOCK:
        cached_payload = _HISTORY_RESPONSE_CACHE.get('payload')
        cached_at = float(_HISTORY_RESPONSE_CACHE.get('at') or 0.0)
        if isinstance(cached_payload, dict) and now_mono - cached_at < 3.0:
            return jsonify(cached_payload)
        if _HISTORY_RESPONSE_CACHE.get('signature') == signature and isinstance(cached_payload, dict):
            _HISTORY_RESPONSE_CACHE['at'] = now_mono
            return jsonify(cached_payload)
    items = [_history_item(group) for group in _video_history_groups().values()]
    items.sort(key=lambda item: (
        -max(int(item.get('last_viewed_at') or 0), int(item.get('added_at') or 0)),
        -int(item.get('last_viewed_at') or 0),
        -int(item.get('added_at') or 0),
        str(item.get('title') or '').casefold(),
    ))
    payload = {'items': items[:100], 'sort': 'added_or_viewed_desc'}
    with _HISTORY_RESPONSE_CACHE_LOCK:
        _HISTORY_RESPONSE_CACHE['signature'] = signature
        _HISTORY_RESPONSE_CACHE['payload'] = payload
        _HISTORY_RESPONSE_CACHE['at'] = time.monotonic()
    response = jsonify(payload)
    # At this point the large temporary history graph is no longer needed.
    _history_trim_heap()
    return response


def endpoint_history(values):
    public=values
    return {
        'items':public[:100],
        'schema':'endpoint.v2',
    }
"""


def patched_fixture():
    original = fixture_source()
    patched = patch_source(original, expected_sha=hashlib.sha256(original.encode()).hexdigest())
    return patched


def isolate_fn(source, name, globals_):
    node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = dict(globals_)
    exec(compile(module, "<synthetic-ast-test>", "exec"), ns)
    return ns[name]


class Args:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def paging():
    args=Args()
    fake_request=types.SimpleNamespace(args=args)
    fn=isolate_fn(patched_fixture(), "_history_page_payload", {"request":fake_request})
    items=[
        {"history_id":f"h{i}", "title":f"Фільм {i}",
         "original_title":f"Film {i}",
         "hidden":i%7==0,
         "source_state":"no_source" if i%2 else "full_release"}
        for i in range(225)
    ]
    return fn,args,{"items":items,"sort":"added_or_viewed_desc"}


def test_backwards_compatible_first_page_and_true_totals(paging):
    fn,args,base=paging
    result=fn(base)
    assert len(result["items"])==100
    assert result["counts"]["total"]==225
    assert result["counts"]["visible"]==sum(not row["hidden"] for row in base["items"])
    assert result["counts"]["hidden"]==225-result["counts"]["visible"]
    assert result["page"]["has_more"] is True


def test_second_page_covers_same_items_not_duplicates(paging):
    fn,args,base=paging
    response=fn(base)
    first=response["items"]
    revision=response["page"]["revision"]
    args.values={"offset":"100","revision":revision}
    second=fn(base)["items"]
    args.values={"offset":"200","revision":revision}
    last=fn(base)["items"]
    assert len(first)==len(second)==100
    assert len(last)==25
    assert set(x["history_id"] for x in first).isdisjoint(
        x["history_id"] for x in second
    )
    assert [x["history_id"] for x in first+second+last]==[
        x["history_id"] for x in base["items"]
    ]


def test_search_includes_last_item_outside_legacy_limit(paging):
    fn,args,base=paging
    args.values={"q":"Film 224"}
    p=fn(base)
    assert p["page"]["matching"]==1
    assert p["items"][0]["history_id"]=="h224"
    assert p["counts"]["total"]==225


def test_empty_and_overflow_offsets(paging):
    fn,args,base=paging
    args.values={"offset":"9999999"}
    assert fn(base)["items"]==[]
    assert fn(base)["page"]["matching"]==225


@pytest.mark.parametrize("limit,expected", [("1",1),("2000",100),("-1",1),("x",100)])
def test_bounded_limit(paging,limit,expected):
    fn,args,base=paging
    args.values={"limit":limit}
    assert len(fn(base)["items"])==expected


def test_query_casefolded_and_trimmed(paging):
    fn,args,base=paging
    args.values={"q":"  ФІЛЬМ 224 "}
    assert len(fn(base)["items"])==1


def test_hidden_flags_preserved_and_pending_not_marked_ready(paging):
    fn,args,base=paging
    page=fn(base)["items"]
    assert page[0]["hidden"] is True
    assert any(x["source_state"]=="no_source" for x in page)


def test_patch_keeps_flask_decorator_attached_to_endpoint():
    tr=ast.parse(patched_fixture())
    f=next(n for n in tr.body if isinstance(n,ast.FunctionDef) and n.name=="video_history_get")
    helper=next(n for n in tr.body if isinstance(n,ast.FunctionDef) and n.name=="_history_page_payload")
    assert len(f.decorator_list)==1
    assert not helper.decorator_list


def test_source_only_two_cached_return_sites_and_no_cap():
    s=patched_fixture()
    assert s.count("return jsonify(_history_page_payload(cached_payload))")==2
    assert "items[:100]" not in s


def test_source_parity_required():
    with pytest.raises(ValueError,match="DEPLOYED_SOURCE_PARITY_FAILED"):
        patch_source(fixture_source())


def test_preferences_cache_bounded_to_single_file_revision(tmp_path):
    source=patched_fixture()
    file=tmp_path/"preferences.json"
    file.write_text('{"h1":{"hidden":true}}')
    class ObservedFile:
        def __init__(self, path):
            self.path=path
            self.read_count=0
        def stat(self):return self.path.stat()
        def read_text(self, **kwargs):
            self.read_count+=1
            return self.path.read_text(**kwargs)
    monitored=ObservedFile(file)
    globals_={
        "VIDEO_HISTORY_PREFERENCES":monitored,
        "json":json,
        "threading":threading,
        "_HISTORY_PREF_CACHE_LOCK":threading.RLock(),
        "_HISTORY_PREF_CACHE":{},
    }
    f=isolate_fn(source,"_history_preferences_read",globals_)
    assert f()["h1"]["hidden"] is True
    for _ in range(200):
        assert f()["h1"]["hidden"] is True
    assert monitored.read_count==1
    f()["h2"]={"hidden":True}
    assert "h2" not in f()
    replacement=tmp_path/"new.json"
    replacement.write_text('{"h1":{"hidden":false}}')
    os.replace(replacement,file)
    assert f()["h1"]["hidden"] is False
    assert monitored.read_count==2


def test_missing_preference_file_returns_empty(tmp_path):
    globals_={
        "VIDEO_HISTORY_PREFERENCES":tmp_path/"not-there",
        "json":json,"threading":threading,
        "_HISTORY_PREF_CACHE_LOCK":threading.RLock(),
        "_HISTORY_PREF_CACHE":{},
    }
    f=isolate_fn(patched_fixture(),"_history_preferences_read",globals_)
    assert f()=={}


def test_second_endpoint_history_no_longer_truncates_at_100():
    source=patched_fixture()
    f=isolate_fn(source,"endpoint_history",{})
    values=[
        {"history_key":f"synthetic-{i}","hidden":bool(i%9==0)}
        for i in range(225)
    ]
    result=f(values)
    assert len(result["items"])==225
    assert result["counts"]["total"]==225
    assert result["counts"]["visible"]==sum(not row["hidden"] for row in values)
    assert result["schema"]=="endpoint.v2"


def test_private_source_patcher_requires_both_caps_in_baseline():
    source=fixture_source().replace("public[:100]","public[:99]")
    with pytest.raises(ValueError,match="PATCH_SOURCE_ANCHOR_MISMATCH"):
        patch_source(source,expected_sha=hashlib.sha256(source.encode()).hexdigest())


@pytest.mark.parametrize("change",["insert","delete","reorder","hidden"])
def test_revision_restart(paging,change):
    fn,args,base=paging
    rev=fn(base)["page"]["revision"]
    if change=="insert":base["items"].insert(0,{"history_id":"new","title":"New"})
    elif change=="delete":base["items"].pop(0)
    elif change=="reorder":base["items"].reverse()
    else:base["items"][1]["hidden"]=True
    args.values={"offset":"100","revision":rev}
    assert fn(base)["page"]["restart_required"] is True
    assert fn(base)["items"]==[]
    args.values={}
    assert fn(base)["page"]["revision"]!=rev

def test_unlimited_catalog(paging):
    fn,args,base=paging
    base["items"]=[{"history_id":str(i),"title":str(i),"hidden":i%7==0} for i in range(10001)]
    r=fn(base)
    rev=r["page"]["revision"]
    out=[]
    while True:
        assert not r["page"]["restart_required"]
        assert r["counts"]["total"]==10001
        out.extend(r["items"])
        if not r["page"]["has_more"]:break
        args.values={"offset":str(len(out)),"revision":rev}
        r=fn(base)
    assert out==base["items"]

def test_continuation_requires_matching_query_revision(paging):
    fn,args,base=paging
    rev=fn(base)["page"]["revision"]
    for args.values in ({"offset":"100"},{"offset":"100","revision":rev,"q":"Film"}):
        assert fn(base)["page"]["restart_required"] is True
