from __future__ import annotations
import json, os, re, sqlite3, subprocess, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

HOME=Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
STATE=HOME/'.local/state/skeleton-cast'
DB=STATE/'trakt-sync.sqlite3'
CFG=HOME/'.config/skeleton-cast/trakt/client.age'
TOK=HOME/'.config/skeleton-cast/trakt/tokens.age'
AGE_ID=HOME/'.config/skeleton/secrets/age-identity.txt'
API='https://api.trakt.tv'
AUTH='https://auth.trakt.tv'
API_VERSION='2'

def _db():
    c=sqlite3.connect(DB, timeout=8)
    c.execute('pragma journal_mode=wal')
    c.execute('''create table if not exists pending(
      content_key text primary key, action text not null, media_type text not null,
      tmdb_id integer, imdb_id text, season integer, episode integer,
      title text, year integer, progress real not null, reason text,
      updated_at integer not null, completed_at integer, attempts integer not null default 0,
      next_attempt_at integer not null default 0, last_error text)''')
    c.execute('''create table if not exists sent(
      content_key text not null, action text not null, sent_at integer not null,
      progress real, response_code integer, primary key(content_key,action,sent_at))''')
    c.commit(); return c

def _int(v: Any)->int|None:
    try:
        n=int(v)
        return n if n>0 else None
    except Exception: return None

def _episode_num(v: Any)->int|None:
    n=_int(v)
    if n: return n
    m=re.search(r'(\d+)',str(v or ''))
    return int(m.group(1)) if m and int(m.group(1))>0 else None

def _identity(job:dict[str,Any], source:dict[str,Any], entry:dict[str,Any])->dict[str,Any]|None:
    catalog=job.get('catalog') if isinstance(job.get('catalog'),dict) else {}
    media_type=str(catalog.get('media_type') or job.get('history_media_type') or '').lower()
    if media_type not in ('movie','tv'):
        media_type='tv' if (source.get('season') is not None or source.get('episode') is not None) else 'movie'
    tmdb=_int(catalog.get('tmdb_id') or job.get('tmdb_id'))
    imdb=str(catalog.get('imdb_id') or job.get('imdb_id') or '').strip() or None
    if not tmdb and not imdb: return None
    season=_int(source.get('season') or job.get('season')) if media_type=='tv' else None
    episode=_episode_num(source.get('episode') or job.get('episode')) if media_type=='tv' else None
    if media_type=='tv' and (season is None or episode is None): return None
    return {'content_key':str(entry.get('content_key') or ''),'media_type':media_type,'tmdb_id':tmdb,'imdb_id':imdb,
            'season':season,'episode':episode,'title':str(entry.get('display_title') or catalog.get('title') or job.get('title') or ''),
            'year':_int(catalog.get('year') or job.get('year'))}

def enqueue_progress(job:dict[str,Any], source:dict[str,Any], entry:dict[str,Any], paused:bool, reason:str)->bool:
    if source.get('live') or source.get('backend')=='iptv' or job.get('live'): return False
    ident=_identity(job,source,entry)
    if not ident or not ident['content_key']: return False
    pos=float(entry.get('last_observed_position_seconds') or entry.get('position_seconds') or 0.0)
    dur=float(entry.get('duration_seconds') or source.get('duration') or 0.0)
    progress=max(0.0,min(100.0,(pos/dur*100.0) if dur>0 else 0.0))
    completed=bool(entry.get('completed'))
    if completed:
        action='history'; progress=100.0
    elif reason in ('stop','before_switch') or str(reason).startswith('before_mode_switch'):
        action='stop'
    elif paused:
        action='pause'
    else:
        action='start'
    now=int(time.time())
    c=_db()
    c.execute('''insert into pending(content_key,action,media_type,tmdb_id,imdb_id,season,episode,title,year,progress,reason,updated_at,completed_at,attempts,next_attempt_at,last_error)
      values(?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,null)
      on conflict(content_key) do update set action=excluded.action,media_type=excluded.media_type,tmdb_id=excluded.tmdb_id,
      imdb_id=excluded.imdb_id,season=excluded.season,episode=excluded.episode,title=excluded.title,year=excluded.year,
      progress=excluded.progress,reason=excluded.reason,updated_at=excluded.updated_at,completed_at=excluded.completed_at,
      attempts=0,next_attempt_at=0,last_error=null''',
      (ident['content_key'],action,ident['media_type'],ident['tmdb_id'],ident['imdb_id'],ident['season'],ident['episode'],ident['title'],ident['year'],round(progress,3),reason,now,now if completed else None))
    c.commit(); c.close(); return True

def _decrypt(path:Path)->dict[str,Any]:
    if not path.exists() or not AGE_ID.exists(): return {}
    r=subprocess.run(['age','-d','-i',str(AGE_ID),str(path)],capture_output=True,text=True,timeout=10,check=False)
    if r.returncode: return {}
    try: return json.loads(r.stdout)
    except Exception: return {}

def _headers(client_id:str, access:str)->dict[str,str]:
    return {'Content-Type':'application/json','trakt-api-version':API_VERSION,'trakt-api-key':client_id,'Authorization':'Bearer '+access,'User-Agent':'SkeletonHome/1.0'}

def _post(url:str, body:dict[str,Any], headers:dict[str,str])->tuple[int,dict[str,Any]]:
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers=headers,method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as r:
            raw=r.read().decode('utf-8','replace')
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw=e.read().decode('utf-8','replace')
        try: data=json.loads(raw) if raw else {}
        except Exception: data={'error':raw[:500]}
        return e.code,data

def status()->dict[str,Any]:
    cfg=_decrypt(CFG); tok=_decrypt(TOK)
    c=_db(); row=c.execute('select count(*),coalesce(min(updated_at),0),coalesce(max(updated_at),0) from pending').fetchone(); c.close()
    return {'configured':bool(cfg.get('client_id') and cfg.get('client_secret')),'authorized':bool(tok.get('access_token')),
            'pending':int(row[0]),'oldest_pending_at':int(row[1]),'newest_pending_at':int(row[2]),
            'state':'ready' if cfg.get('client_id') and tok.get('access_token') else ('blocked_oauth' if cfg.get('client_id') else 'blocked_client_credentials')}

def _payload(row:sqlite3.Row)->tuple[str,dict[str,Any]]:
    ids={}
    if row['tmdb_id']: ids['tmdb']=int(row['tmdb_id'])
    if row['imdb_id']: ids['imdb']=str(row['imdb_id'])
    if row['media_type']=='movie':
        media={'ids':ids}
        if row['title']: media['title']=row['title']
        if row['year']: media['year']=int(row['year'])
        if row['action']=='history': return '/sync/history', {'movies':[{'ids':ids,'watched_at':time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime(row['completed_at'] or row['updated_at']))}]}
        return '/scrobble/'+row['action'], {'movie':media,'progress':float(row['progress'])}
    show={'ids':ids}
    if row['title']: show['title']=row['title']
    if row['year']: show['year']=int(row['year'])
    ep={'season':int(row['season']),'number':int(row['episode'])}
    if row['action']=='history':
        ep2={'number':int(row['episode']),'watched_at':time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime(row['completed_at'] or row['updated_at']))}
        return '/sync/history', {'shows':[{'ids':ids,'seasons':[{'number':int(row['season']),'episodes':[ep2]}]}]}
    return '/scrobble/'+row['action'], {'show':show,'episode':ep,'progress':float(row['progress'])}

def flush(limit:int=20)->dict[str,Any]:
    st=status()
    if st['state']!='ready': return {**st,'sent':0}
    cfg=_decrypt(CFG); tok=_decrypt(TOK); now=int(time.time())
    c=_db(); c.row_factory=sqlite3.Row
    rows=c.execute('select * from pending where next_attempt_at<=? order by updated_at asc limit ?',(now,limit)).fetchall()
    sent=0
    for row in rows:
        path,body=_payload(row)
        code,data=_post(API+path,body,_headers(str(cfg['client_id']),str(tok['access_token'])))
        if 200<=code<300:
            c.execute('delete from pending where content_key=? and updated_at=?',(row['content_key'],row['updated_at']))
            c.execute('insert or ignore into sent(content_key,action,sent_at,progress,response_code) values(?,?,?,?,?)',(row['content_key'],row['action'],int(time.time()),row['progress'],code))
            sent+=1
        else:
            attempts=int(row['attempts'])+1
            delay=min(3600,30*(2**min(attempts,6)))
            c.execute('update pending set attempts=?,next_attempt_at=?,last_error=? where content_key=?',(attempts,now+delay,(str(data)[:500] or f'HTTP {code}'),row['content_key']))
            if code in (401,403): break
    c.commit(); c.close(); return {**status(),'sent':sent}
