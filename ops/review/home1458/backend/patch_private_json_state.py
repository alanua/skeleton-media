"""Patch the existing writer only; keep other functions and constants unchanged."""
import ast
def patch(source):
    tree=ast.parse(source)
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_atomic_private']
    if len(nodes)!=1:raise ValueError('Expected one canonical private writer')
    node=nodes[0]
    if 'path.with_suffix' not in ast.get_source_segment(source,node):raise ValueError('Writer baseline differs')
    signature=source.splitlines()[node.lineno-1]
    replacement=signature+'\n    from private_json_state import atomic_private_json\n    atomic_private_json(path, value)\n'
    lines=source.splitlines(keepends=True)
    output=''.join(lines[:node.lineno-1])+replacement+''.join(lines[node.end_lineno:])
    before=ast.parse(source);after=ast.parse(output)
    before.body=[n for n in before.body if not(isinstance(n,ast.FunctionDef) and n.name=='_atomic_private')]
    after.body=[n for n in after.body if not(isinstance(n,ast.FunctionDef) and n.name=='_atomic_private')]
    if ast.dump(before,include_attributes=False)!=ast.dump(after,include_attributes=False):raise ValueError('Unrelated code changed')
    compile(output,'<private writer repair>','exec')
    return output
