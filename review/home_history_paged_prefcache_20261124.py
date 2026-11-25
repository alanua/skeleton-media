"""Review-only, SHA-pinned patch builder for deployed Home history.

This file intentionally does NOT edit live Home or deploy a service.
Use a private, isolated copy of the actual deployed app.py as input.
The output path MUST differ from the input; protected production paths
are not accepted. No runtime or credentials are part of this repository.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
from pathlib import Path

DEPLOYED_SHA256 = "14244d9c5cc2ad081a980d136a2894d3390e13f3a4dbd71cc9847bb04cb56dc8"

OLD_PREF = """def _history_preferences_read() -> dict[str, dict]:
    try:
        data = json.loads(VIDEO_HISTORY_PREFERENCES.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}"""

NEW_PREF = """_HISTORY_PREF_CACHE_LOCK = threading.RLock()
_HISTORY_PREF_CACHE: dict[str, object] = {}


def _history_preferences_read() -> dict[str, dict]:
    # Exact inode/stat signature invalidates on atomic replacement.
    # One physical JSON read per revision rather than one per work card.
    try:
        st = VIDEO_HISTORY_PREFERENCES.stat()
        signature = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        with _HISTORY_PREF_CACHE_LOCK:
            if _HISTORY_PREF_CACHE.get('signature') == signature:
                return dict(_HISTORY_PREF_CACHE['value'])
        data = json.loads(VIDEO_HISTORY_PREFERENCES.read_text(encoding='utf-8'))
        value = data if isinstance(data, dict) else {}
        with _HISTORY_PREF_CACHE_LOCK:
            _HISTORY_PREF_CACHE['signature'] = signature
            _HISTORY_PREF_CACHE['value'] = value
        return dict(value)
    except Exception:
        return {}"""

OLD_GET_ANCHOR = """@app.get('/api/video/history')
def video_history_get() -> Response:"""

NEW_GET_ANCHOR = """def _history_page_payload(base: dict) -> dict:
    # Build/sort canonical works once; bound the bytes sent on each request.
    all_items = base.get('items')
    if not isinstance(all_items, list):
        all_items = []
    query = str(request.args.get('q') or '').strip().casefold()[:120]
    try:
        offset = max(0, int(request.args.get('offset') or 0))
        limit = max(1, min(100, int(request.args.get('limit') or 100)))
    except (TypeError, ValueError):
        offset, limit = 0, 100
    matches = [
        item for item in all_items
        if not query or query in str(item.get('title') or '').casefold()
        or query in str(item.get('original_title') or '').casefold()
    ]
    visible = base.get('_visible_count')
    if visible is None:
        visible = sum(not bool(item.get('hidden')) for item in all_items)
    import hashlib
    import json
    catalog_revision = base.get('_catalog_revision') or hashlib.sha256(
        json.dumps(all_items, sort_keys=True, ensure_ascii=False,
                   separators=(',', ':')).encode('utf-8')).hexdigest()
    revision = hashlib.sha256((catalog_revision + ':' + query).encode('utf-8')).hexdigest()
    supplied = str(request.args.get('revision') or '')
    restart = (offset > len(matches) or (offset > 0 and supplied != revision)
               or (bool(supplied) and supplied != revision))
    page = [] if restart else matches[offset:offset + limit]
    return {
        'items': page,
        'sort': base.get('sort') or 'added_or_viewed_desc',
        'counts': {
            'total': len(all_items), 'visible': visible,
            'hidden': len(all_items) - visible,
        },
        'page': {
            'offset': offset, 'limit': limit, 'matching': len(matches),
            'returned': len(page), 'revision': revision, 'restart_required': restart,
            'has_more': not restart and offset + len(page) < len(matches),
        },
    }


@app.get('/api/video/history')
def video_history_get() -> Response:"""


def patch_source(source: str, *, expected_sha: str = DEPLOYED_SHA256) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != expected_sha:
        raise ValueError("DEPLOYED_SOURCE_PARITY_FAILED")
    edits = (
        (OLD_PREF, NEW_PREF),
        (OLD_GET_ANCHOR, NEW_GET_ANCHOR),
        ("    payload = {'items': items[:100], 'sort': 'added_or_viewed_desc'}",
         "    payload = {'items': items, 'sort': 'added_or_viewed_desc',\n"
         "               '_catalog_revision': hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest(),\n"
         "               '_visible_count': sum(not bool(item.get('hidden')) for item in items)}"),
        ("    response = jsonify(payload)\n    # At this point the large temporary history graph",
         "    response = jsonify(_history_page_payload(payload))\n    # At this point the large temporary history graph"),
        # Endpoint-scoped watched history must not silently truncate at 100.
        ("        'items':public[:100],",
         "        'items':public,\n"
         "        'counts': {'total': len(public),\n"
         "                   'visible': sum(not bool(item.get('hidden', False)) for item in public)},"),
    )
    updated = source
    for old, new in edits:
        if updated.count(old) != 1:
            raise ValueError("PATCH_SOURCE_ANCHOR_MISMATCH")
        updated = updated.replace(old, new, 1)
    cache_return = "return jsonify(cached_payload)"
    if updated.count(cache_return) != 2:
        raise ValueError("PATCH_CACHED_RESPONSE_ANCHOR_MISMATCH")
    updated = updated.replace(cache_return, "return jsonify(_history_page_payload(cached_payload))")
    compile(updated, "<isolated-reviewed-home-app>", "exec")
    return updated


def main() -> int:
    ap = argparse.ArgumentParser(description="Patch a private source copy only.")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--diff", type=Path, required=True)
    opts = ap.parse_args()
    input_path, output_path, diff_path = (
        opts.input.resolve(), opts.output.resolve(), opts.diff.resolve()
    )
    if len({input_path, output_path, diff_path}) != 3:
        raise ValueError("DISTINCT_PRIVATE_PATHS_REQUIRED")
    for path in (output_path, diff_path):
        if path.exists() or not str(path).startswith("/tmp/"):
            raise ValueError("ISOLATED_SCRATCH_OUTPUT_REQUIRED")
    original = input_path.read_text(encoding="utf-8")
    updated = patch_source(original)
    lines = list(difflib.unified_diff(
        original.splitlines(), updated.splitlines(),
        fromfile="a/active-app.py", tofile="b/active-app.py",
        lineterm="", n=3,
    ))
    diff = "\n".join(lines) + "\n"
    if not 2 <= diff.count("@@ -") <= 6:
        raise ValueError("UNEXPECTED_CHANGE_SCOPE")
    output_path.write_text(updated, encoding="utf-8")
    diff_path.write_text(diff, encoding="utf-8")
    output_path.chmod(0o600)
    diff_path.chmod(0o600)
    print("PATCH_TEST_INPUT_SHA256=" + DEPLOYED_SHA256)
    print("PATCH_HUNKS=" + str(diff.count("@@ -")))
    print("PATCH_SHA256=" + hashlib.sha256(diff.encode()).hexdigest())
    print("NO_LIVE_WRITE=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
