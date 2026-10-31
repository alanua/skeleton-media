"""Synthetic source-only contracts; no server is imported or started."""
import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from skeleton_media.cast.source_page_provenance import (
    source_page_provenance as provenance,
    validated_page_url,
)


def group():
    return {"jobs": [
        (300, {"job_id": "episode-1", "page_url": "https://original.org/episode/1",
               "sources": [
                   {"source_id": "dub", "discovery_page_url": "https://mirror.org/ep1/dub"},
                   {"source_id": "sub", "source_page_url": "https://mirror.org/ep1/sub"}]}),
        (200, {"job_id": "episode-2", "page_url": "https://original.org/episode/2",
               "sources": [{"source_id": "dub", "history_page_url": "https://mirror.org/ep2/dub"}]}),
    ]}


def selected(value=None, **kwargs):
    return provenance(group() if value is None else value, job_id="episode-1", source_id="dub", **kwargs)


def test_exact_pair_distinguishes_episode_and_voice():
    assert selected() == {"source_page_url": "https://mirror.org/ep1/dub"}
    assert provenance(group(), job_id="episode-1", source_id="sub") == {
        "source_page_url": "https://mirror.org/ep1/sub"}
    assert provenance(group(), job_id="episode-2", source_id="dub") == {
        "source_page_url": "https://mirror.org/ep2/dub"}
    for ids in ({}, {"source_id": "dub"}, {"job_id": "episode-1"},
                {"job_id": "episode-2", "source_id": "sub"},
                {"job_id": "episode-1", "source_id": 1}):
        assert provenance(group(), **ids) == {}


def test_priority_and_original_job_fallback():
    data = group()
    source = data["jobs"][0][1]["sources"][0]
    source.update(source_page_url="https://second.org/watch/1", history_page_url="https://third.org/watch/1")
    assert selected(data)["source_page_url"] == "https://mirror.org/ep1/dub"
    source["discovery_page_url"] = "https://private.local/watch"
    assert selected(data)["source_page_url"] == "https://second.org/watch/1"
    del source["source_page_url"]
    assert selected(data)["source_page_url"] == "https://third.org/watch/1"
    del source["history_page_url"]
    assert selected(data)["source_page_url"] == "https://original.org/episode/1"
    data["jobs"][0][1]["page_url"] = "https://cdn.org/video.mp4"
    data.update(page_url="https://unrelated.org/catalog", preferred={"page_url": "https://unrelated.org/mirror"})
    source.update(stream_url="https://cdn.org/watch", referer="https://ref.org/watch",
                  poster="https://poster.org/watch", trailer="https://trailer.org/watch",
                  page_url="https://arbitrary.org/watch")
    assert selected(data) == {}


def test_verified_object_and_pair_constraints():
    source = {"source_page_url": "https://verified.org/watch/1"}
    assert provenance({}, verified_source=source) == source
    assert provenance(group(), verified_source=source, job_id="episode-1") == {}
    assert provenance(group(), verified_source={"job_id": "episode-1", "source_id": "dub"}) == {
        "source_page_url": "https://original.org/episode/1"}
    assert provenance(group(), verified_source={"job_id": "missing", "source_id": "dub"}) == {}


def test_sites_are_opt_in_bounded_deduplicated_and_in_origin_order():
    data = group()
    source = data["jobs"][0][1]["sources"][0]
    source["source_sites"] = ["https://mirror.org/ep1/dub", {"source_page_url": "https://legacy.org/1"},
                              "https://legacy.org/1", None, {}, "https://127.0.0.1/watch"]
    assert "source_sites" not in selected(data)
    assert selected(data, include_source_sites=True)["source_sites"] == [
        {"source_page_url": "https://mirror.org/ep1/dub"},
        {"source_page_url": "https://legacy.org/1"}]
    result = provenance(data, job_id="episode-2", source_id="dub", include_source_sites=True,
                        all_verified_sites=True)
    assert result["source_page_url"] == "https://mirror.org/ep2/dub"
    assert [site["source_page_url"] for site in result["source_sites"]] == [
        "https://mirror.org/ep1/dub", "https://legacy.org/1", "https://mirror.org/ep1/sub", "https://mirror.org/ep2/dub"]
    assert len(selected(data, include_source_sites=True, all_verified_sites=True, max_sites=1)["source_sites"]) == 1
    assert provenance(data, include_source_sites=True, all_verified_sites=True) == {}
    data["source_sites"] = ["https://unrelated.org/mirror"]
    assert "unrelated" not in json.dumps(selected(data, include_source_sites=True, all_verified_sites=True))


@pytest.mark.parametrize("value", [None, 3, [], {}, "", " https://public.org/watch",
    "http://localhost/watch", "http://foo.localhost/watch", "http://host.local/watch",
    "http://host.internal/watch", "http://printer/watch", "http://127.0.0.1/watch",
    "http://10.0.0.1/watch", "http://169.254.169.254/watch", "http://[::1]/watch",
    "http://[2606:4700:4700::1111]/watch", "http://2130706433/watch",
    "https://user:password@public.org/watch", "https://public.org@evil.org/watch",
    "https://public.org:bad/watch", "https://public.org:8080/watch", "https://public.org./watch",
    "https://public.org/a\nwatch", "https://public.org/%0dwatch", "https://public.org/%250awatch",
    "https://public.org/a\\b", "https://public.org/%2fsecret", "https://public.org/%zz",
    "file:///watch", "//public.org/watch", "https://public.org/video.M3U8?episode=1",
    "https://public.org/video.mp4", "https://public.org/video.mpd/segment",
    "https://public.org/watch?token=secret", "https://public.org/watch?access_token=secret",
    "https://public.org/watch?sig=secret", "https://public.org/watch?key=secret",
    "https://public.org/watch?url=https://evil.org", "https://public.org/watch#token=secret",
    "https://public.org/watch?id=secret", "https://public.org/watch?id=1&id=2",
    "https://youtube.com/watch?v=bad", "https://youtube.com/watch?v=dQw4w9WgXcQ&token=secret"])
def test_unsafe_url_rejected(value):
    assert validated_page_url(value) is None


@pytest.mark.parametrize("url", ["https://public.org/watch/one", "http://public.org/episode.html",
    "https://public.org/watch?id=123&episode=2&season=1&page=3",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
def test_safe_page_candidates(url):
    assert validated_page_url(url) == url


@pytest.mark.parametrize("data", [None, [], {}, {"jobs": None}, {"jobs": {}},
    {"jobs": [None, (), (1,), (1, None), (1, {"job_id": "episode-1", "sources": {}}),
              (1, {"job_id": "episode-1", "sources": [None, [], {}, {"source_id": "dub", "job_id": "wrong"}]})]}])
def test_malformed_groups(data):
    assert provenance(data, job_id="episode-1", source_id="dub") == {}


def test_historical_duplicates_fail_closed_on_conflicting_pages():
    data = group()
    data["jobs"].append(deepcopy(data["jobs"][0]))
    assert selected(data) == selected()
    data["jobs"][-1][1]["sources"][0]["discovery_page_url"] = "https://different.org/episode"
    assert selected(data) == {}


def test_json_contract_no_mutation_and_hard_site_bound():
    data = group()
    data["jobs"][0][1]["sources"][0]["source_sites"] = [f"https://public.org/watch/{i}" for i in range(40)]
    before = deepcopy(data)
    result = selected(data, include_source_sites=True, max_sites=100)
    assert data == before
    assert json.loads(json.dumps(result)) == result
    assert set(result) == {"source_page_url", "source_sites"}
    assert len(result["source_sites"]) == 16
    assert all(set(site) == {"source_page_url"} and isinstance(site["source_page_url"], str)
               for site in result["source_sites"])
    result["source_sites"][0]["source_page_url"] = "changed"
    assert data == before


def test_integration_contract_repository_does_not_own_history_get():
    root = Path(__file__).resolve().parents[1]
    helper = root / "skeleton_media/cast/source_page_provenance.py"
    for folder in (root / "skeleton_media", root / "ops"):
        for path in folder.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # No Python production source owns the literal endpoint, nor the
            # live shared-catalog functions. Consumers elsewhere are permitted.
            assert not any(isinstance(node, ast.Constant) and node.value == "/api/video/history"
                           for node in ast.walk(tree)), str(path)
            assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name in {"_video_history_groups", "_history_item", "_history_merged_job"}
                           for node in ast.walk(tree)), str(path)
            if path != helper:
                assert "source_page_provenance" not in path.read_text(encoding="utf-8"), str(path)
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    imports = {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert imports <= {"collections", "ipaddress", "re", "urllib"}
    assert not any(isinstance(node, ast.FunctionDef) and node.decorator_list for node in ast.walk(tree))
