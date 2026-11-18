"""One bounded resolver request. Invoked privately by BoundedResolver only."""
import json
from pathlib import Path
import sys


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        origin = request.get('page_url')
        if not isinstance(origin, str) or not origin.startswith(('http://', 'https://')):
            raise ValueError()
        # Import only the existing sibling resolver, never app.py and its
        # watchers, Flask setup, account configuration or player startup.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from resolver import resolve_page
        result = resolve_page(origin)
        if not isinstance(result, dict) or not isinstance(result.get('sources'), list):
            raise ValueError()
        output = json.dumps({'ok': True, 'result': result}, ensure_ascii=False)
        if len(output.encode('utf-8')) > 4*1024*1024:
            raise ValueError()
        sys.stdout.write(output)
        return 0
    except Exception:
        sys.stdout.write('{"ok":false}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
