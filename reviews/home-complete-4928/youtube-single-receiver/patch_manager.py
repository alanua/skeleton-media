"""Add only guarded opt-in enforcement to the current connection manager."""
import ast,hashlib,sys
from pathlib import Path
src=Path(sys.argv[1]);helper=Path(sys.argv[2]).read_text();out=Path(sys.argv[3])
text=src.read_text()
anchor="threading.Thread(target=ipc_server,daemon=True,name='youtube-ipc').start()"
assert text.count(anchor)==1
assert 'def manual_receiver_policy' not in text
result=text.replace(anchor,helper+'\n'+anchor)
anchor2="   with DIAL_LOCK:\n    DIAL_STATE['screen_id']"
assert result.count(anchor2)==1
result=result.replace(anchor2,"   policy=manual_receiver_policy(ws,seq)\n"+anchor2)
anchor3="dial_ready=bool(DIAL_STATE.get('screen_id')))"
assert result.count(anchor3)==1
result=result.replace(anchor3,"dial_ready=bool(DIAL_STATE.get('screen_id')),manual_receiver_policy=policy)")
old=ast.parse(text);new=ast.parse(result)
functions=lambda tree:{n.name:ast.dump(n,include_attributes=False) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
before=functions(old);after=functions(new)
assert all(after[k]==v for k,v in before.items())
assert set(after)-set(before)=={'manual_receiver_policy'}
assert 'DIAL_DISCOVERY_ENABLED=False' in result
out.write_text(result);out.chmod(0o600)
print({'existing_functions_preserved':len(before),'new_functions':1,'before_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'after_sha256':hashlib.sha256(out.read_bytes()).hexdigest()})
