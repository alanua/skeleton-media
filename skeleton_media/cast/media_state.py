from __future__ import annotations
import json, os, time
import tempfile
from pathlib import Path
from typing import Any
HOME=Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
DIR=HOME/'.local/state/skeleton/media-state'
SNAPSHOT=DIR/'current.json'
EVENTS=DIR/'events.jsonl'
SCHEMA='skeleton.media.state.v1'

def _atomic_json(path:Path,value:dict[str,Any])->None:
    DIR.mkdir(parents=True,exist_ok=True)
    tmp_name=None
    try:
        with tempfile.NamedTemporaryFile(
            'w',
            encoding='utf-8',
            dir=path.parent,
            prefix=f'.{path.name}.',
            suffix='.tmp',
            delete=False,
        ) as fh:
            tmp_name=fh.name
            fh.write(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n')
        os.replace(tmp_name,path)
        tmp_name=None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

def publish(*,mode:dict[str,Any]|None=None,player:dict[str,Any]|None=None,volume:dict[str,Any]|None=None,reason:str='observe')->dict[str,Any]:
    try:
        old=json.loads(SNAPSHOT.read_text(encoding='utf-8'))
        if not isinstance(old,dict): old={}
    except Exception: old={}
    state={
      'schema':SCHEMA,'updated_at':time.time(),'reason':reason,
      'mode': mode if isinstance(mode,dict) else old.get('mode',{}),
      'player': player if isinstance(player,dict) else old.get('player',{}),
      'volume': volume if isinstance(volume,dict) else old.get('volume',{}),
    }
    comparable={k:state.get(k) for k in ('mode','player','volume')}
    previous={k:old.get(k) for k in ('mode','player','volume')}
    changed=[k for k in comparable if comparable[k]!=previous[k]]
    state['changed']=changed
    _atomic_json(SNAPSHOT,state)
    if changed:
        event={'schema':'skeleton.media.event.v1','at':state['updated_at'],'reason':reason,'changed':changed,'state':{k:state[k] for k in changed}}
        with EVENTS.open('a',encoding='utf-8') as fh: fh.write(json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n')
    return state
