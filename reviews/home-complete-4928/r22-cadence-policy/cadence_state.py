"""Owned persistent opt-in settings and non-evicting cadence/probe budget.

No process, display operation, executor or timer is started here.
"""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

SCHEMA='skeleton.cadence.opt_in.v1'

def private_json(path):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        st=os.fstat(fd)
        if st.st_uid!=os.getuid() or st.st_mode&0o077 or not stat.S_ISREG(st.st_mode) or st.st_size>16384:
            raise ValueError('owned private JSON required')
        with os.fdopen(fd,'r',closefd=False) as stream:value=json.load(stream)
        if not isinstance(value,dict):raise ValueError('object required')
        return value
    finally:os.close(fd)

def atomic_private(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    parent=path.parent
    if parent.is_symlink() or parent.stat().st_uid!=os.getuid() or parent.stat().st_mode&0o077:
        raise ValueError('owned private directory required')
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def read_opt_in(path):
    try:value=private_json(path)
    except FileNotFoundError:return {'schema':SCHEMA,'enabled':False}
    if value.get('schema')!=SCHEMA or type(value.get('enabled')) is not bool:
        raise ValueError('invalid opt-in settings')
    return value

class CadenceStore:
    """Keep all 128 sessions; a newer session cannot erase an older retry cap."""
    def __init__(self,root,capacity=128):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid!=os.getuid() or self.root.stat().st_mode&0o077:
            raise ValueError('owned private state root required')
        self.path=self.root/'cadence.sqlite3';self.capacity=capacity
        if self.path.is_symlink():raise ValueError('symlink database refused')
        self.c=sqlite3.connect(self.path,timeout=.2)
        os.chmod(self.path,0o600)
        self.c.execute('create table if not exists sessions(session text primary key,payload text not null)')
        self.c.execute('create table if not exists meta(key text primary key,payload text not null)')
        self.c.commit()

    def close(self):self.c.close()
    def _meta(self,key):
        row=self.c.execute('select payload from meta where key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def read(self,session):
        row=self.c.execute('select payload from sessions where session=?',(session,)).fetchone()
        result=json.loads(row[0]) if row else {}
        global_attempt=self._meta('attempt')
        if global_attempt:
            result.update(boot=global_attempt['boot'],last_attempt=global_attempt['last_attempt'])
        return result

    def write(self,value):
        if not isinstance(value,dict) or not isinstance(value.get('session'),str) or len(value['session'])!=64:
            raise ValueError('session required')
        attempts=value.get('attempts')
        if type(attempts) is not int or not 1<=attempts<=2:raise ValueError('bounded attempts required')
        with self.c:
            self.c.execute('begin immediate')
            old=self.c.execute('select payload from sessions where session=?',(value['session'],)).fetchone()
            if old:
                if json.loads(old[0]).get('attempts',0)>attempts:raise ValueError('retry counter regression')
            elif self.c.execute('select count(*) from sessions').fetchone()[0]>=self.capacity:
                raise ValueError('session capacity reached')
            self.c.execute('insert or replace into sessions values(?,?)',(value['session'],json.dumps(value,sort_keys=True)))
            self.c.execute('insert or replace into meta values(?,?)',('attempt',json.dumps({k:value[k] for k in ('boot','last_attempt')})))

    def reserve_probe(self,*,session,boot,now):
        """At most one short second-source connection per 60s, including failures."""
        with self.c:
            self.c.execute('begin immediate')
            previous=self._meta('probe')
            if previous.get('boot')==boot and now-previous['at']<60:return False
            row=self.c.execute('select payload from sessions where session=?',(session,)).fetchone()
            if row and json.loads(row[0]).get('attempts',0)>=2:return False
            if not row and self.c.execute('select count(*) from sessions').fetchone()[0]>=self.capacity:return False
            self.c.execute('insert or replace into meta values(?,?)',('probe',json.dumps(dict(session=session,boot=boot,at=now))))
        return self._meta('probe')==dict(session=session,boot=boot,at=now)
