"""Exact current-source resolver injection patch; not a runtime deploy script."""
import ast
import hashlib
from pathlib import Path

BEFORE = '70af15e405e506d6bc4de3d92e545850fdf034aa31cc059bf5885de376106a38'


def patch(path):
    path = Path(path)
    source = path.read_text()
    signature = 'def _refresh_stale_source(job: dict, stale: dict, *, persist: bool = True) -> dict:'
    updated = signature.replace('persist: bool = True)', 'persist: bool = True, resolver=None)')
    result_line = '    result = resolve_page(page_url)'
    new_result = '    result = (resolver or resolve_page)(page_url)'
    if updated in source:
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == '_refresh_stale_source')
        if new_result not in ast.get_source_segment(source, node):
            raise ValueError('partial injection patch')
        original = source.replace(updated, signature, 1)
        # Only undo this function's changed line when validating idempotency.
        start = original.index(signature)
        end = original.find('\ndef ', start+1)
        if end < 0:
            end = len(original)
        original = original[:start] + original[start:end].replace(new_result, result_line, 1) + original[end:]
        if hashlib.sha256(original.encode()).hexdigest() != BEFORE:
            raise ValueError('patched source drift')
        return False
    if hashlib.sha256(source.encode()).hexdigest() != BEFORE:
        raise ValueError('current-source drift')
    before_tree = ast.parse(source)
    node = next(n for n in before_tree.body
                if isinstance(n, ast.FunctionDef) and n.name == '_refresh_stale_source')
    segment = ast.get_source_segment(source, node)
    assert segment.count(result_line) == 1
    changed = segment.replace(signature, updated, 1).replace(result_line, new_result, 1)
    new_source = source.replace(segment, changed, 1)
    after_tree = ast.parse(new_source)
    original_other = [ast.dump(n) for n in before_tree.body if n is not node]
    updated_other = [ast.dump(n) for n in after_tree.body
                     if not (isinstance(n, ast.FunctionDef) and n.name == node.name)]
    assert original_other == updated_other
    path.write_text(new_source)
    return True


if __name__ == '__main__':
    import sys
    patch(sys.argv[1])
