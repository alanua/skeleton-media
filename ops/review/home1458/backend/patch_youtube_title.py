"""Replace only the verified YouTube observation expression; preserve all other code."""
import ast
import hashlib

EXPECTED_OLD_JS_SHA256 = 'c018eb48eebdb28e9a662d23dfc4019685ec1133d9f4973a1b6b6aa110681dfc'

def status_assignment(tree):
    nodes=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STATUS_JS' for t in n.targets)]
    if len(nodes)!=1 or not isinstance(nodes[0].value,ast.Constant) or not isinstance(nodes[0].value.value,str):
        raise ValueError('Expected one literal STATUS_JS assignment')
    return nodes[0]

def remaining_ast(source):
    tree=ast.parse(source);node=status_assignment(tree);tree.body.remove(node)
    return ast.dump(tree,include_attributes=False)

def patch_status_js(source,js,expected_old_js_sha256=EXPECTED_OLD_JS_SHA256):
    tree=ast.parse(source);node=status_assignment(tree)
    if hashlib.sha256(node.value.value.encode()).hexdigest()!=expected_old_js_sha256:
        raise ValueError('YouTube status expression differs from reviewed baseline')
    if not isinstance(js,str) or not js.strip():raise ValueError('Missing status expression')
    lines=source.splitlines(keepends=True)
    result=''.join(lines[:node.lineno-1])+'STATUS_JS = '+repr(js)+'\n'+''.join(lines[node.end_lineno:])
    compile(result,'<status-only repair>','exec')
    if remaining_ast(source)!=remaining_ast(result):raise ValueError('Unrelated source changed')
    return result
