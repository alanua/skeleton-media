from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
import time
from typing import Any, Callable
from bs4 import BeautifulSoup
import media_discovery

DB = media_discovery.DB
SCHEMA = 'skeleton.home.media_release_monitor.v1'

def _con() -> sqlite3.Connection:
    con=sqlite3.connect(DB,timeout=20); con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON'); con.execute('PRAGMA busy_timeout=20000')
    return con

def ensure_schema() -> None:
    with _con() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS media_release_monitors (
          media_uid TEXT PRIMARY KEY,
          tmdb_id INTEGER NOT NULL,
          media_type TEXT NOT NULL,
          title TEXT NOT NULL,
          catalog_url TEXT,
          enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
          explicit_override TEXT,
          title_status TEXT,
          baseline_release_key TEXT,
          baseline_season INTEGER,
          baseline_episode INTEGER,
          baseline_air_date TEXT,
          pending_release_key TEXT,
          pending_season INTEGER,
          pending_episode INTEGER,
          pending_air_date TEXT,
          pending_episode_title TEXT,
          state TEXT NOT NULL DEFAULT 'MONITORED',
          localization_type TEXT,
          localization_source TEXT,
          alert_claim_occurrence TEXT,
          alert_claimed_at INTEGER,
          notified_release_key TEXT,
          telegram_message_id INTEGER,
          last_release_check INTEGER,
          last_localization_check INTEGER,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY(media_uid) REFERENCES media_items(media_uid) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS media_release_monitors_enabled_idx ON media_release_monitors(enabled,state);
        ''')

def _row(row):
    return {} if row is None else {k:row[k] for k in row.keys()}

def _get(media_uid:str)->dict[str,Any]:
    ensure_schema()
    with _con() as con: return _row(con.execute('SELECT * FROM media_release_monitors WHERE media_uid=?',(media_uid,)).fetchone())

def _identity(job:dict[str,Any])->dict[str,Any]:
    cat=job.get('catalog') if isinstance(job.get('catalog'),dict) else {}
    media_uid=str(cat.get('media_uid') or ''); tmdb_id=int(cat.get('tmdb_id') or 0)
    media_type=str(cat.get('media_type') or job.get('history_media_type') or '').lower()
    title=str(cat.get('title') or job.get('title') or '').strip()
    catalog_url=str(job.get('catalog_url') or cat.get('catalog_url') or cat.get('source_url') or '')
    if not media_uid and tmdb_id and media_type:
        with _con() as con:
            r=con.execute('SELECT media_uid,source_url,title,media_type FROM media_items WHERE tmdb_id=? AND media_type=? ORDER BY updated_at DESC LIMIT 1',(tmdb_id,media_type)).fetchone()
        if r:
            media_uid=str(r['media_uid']); catalog_url=catalog_url or str(r['source_url'] or ''); title=title or str(r['title'] or ''); media_type=media_type or str(r['media_type'] or '')
    if not media_uid or tmdb_id<=0 or media_type not in {'tv','movie'}:
        raise ValueError('Для моніторингу потрібна підтверджена TMDB-ідентичність твору.')
    return {'media_uid':media_uid,'tmdb_id':tmdb_id,'media_type':media_type,'title':title or 'Твір','catalog_url':catalog_url or f'https://www.themoviedb.org/{media_type}/{tmdb_id}'}

def schedule_id(media_uid:str)->str:
    return 'media.monitor.'+hashlib.sha256(media_uid.encode()).hexdigest()[:24]

def _release_tuple(key:str|None)->tuple[int,int]:
    m=re.fullmatch(r'S(\d{1,3})E(\d{1,4})',str(key or ''),re.I); return (int(m.group(1)),int(m.group(2))) if m else (0,0)

def _source_max(job:dict[str,Any])->tuple[int,int]:
    best=(0,0)
    for s in job.get('sources') or []:
        if media_discovery.is_trailer_source(s): continue
        sm=re.search(r'\d+',str(s.get('season') or '')); em=re.search(r'\d+',str(s.get('episode') or ''))
        cur=(int(sm.group()) if sm else 0,int(em.group()) if em else 0)
        if cur>best: best=cur
    return best

def _has_full_source(job:dict[str,Any])->bool:
    return any(isinstance(s,dict) and not media_discovery.is_trailer_source(s) for s in (job.get('sources') or []))

def tmdb_release_snapshot(tmdb_id:int)->dict[str,Any]:
    response=media_discovery._get(f'https://www.themoviedb.org/tv/{int(tmdb_id)}?language=en-US',timeout=30)
    soup=BeautifulSoup(response.text,'lxml'); text=' '.join(soup.get_text(' ',strip=True).split())
    status=''; m=re.search(r'\bStatus\s+(Returning Series|Ended|Canceled|In Production|Planned|Pilot)\b',text,re.I)
    if m: status=m.group(1)
    eps=[]
    for m in re.finditer(r'\((\d{1,3})x(\d{1,4}),\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})\)',text):
        try: air=dt.datetime.strptime(m.group(3),'%B %d, %Y').date()
        except Exception: continue
        prefix=text[max(0,m.start()-140):m.start()].strip(); title=re.split(r'(?<=[.!?])\s+',prefix)[-1][-100:].strip(' -–—')
        eps.append({'season':int(m.group(1)),'episode':int(m.group(2)),'air_date':air.isoformat(),'episode_title':title})
    if not eps: return {'status':status,'release_key':'','season':0,'episode':0,'air_date':'','episode_title':''}
    latest=max(eps,key=lambda x:(x['season'],x['episode']))
    return {'status':status,'release_key':f"S{latest['season']:02d}E{latest['episode']:02d}",**latest}

def state_for_job(job:dict[str,Any])->dict[str,Any]:
    meta=_identity(job); row=_get(meta['media_uid'])
    return {'schema':SCHEMA,'media_uid':meta['media_uid'],'tmdb_id':meta['tmdb_id'],'media_type':meta['media_type'],'title':meta['title'],'enabled':bool(row.get('enabled',0)),'state':row.get('state') or 'OFF','title_status':row.get('title_status') or '', 'pending_release_key':row.get('pending_release_key') or '', 'notified_release_key':row.get('notified_release_key') or '', 'schedule_id':schedule_id(meta['media_uid'])}

def set_for_job(job:dict[str,Any],enabled:bool)->dict[str,Any]:
    ensure_schema(); meta=_identity(job); now=int(time.time()); old=_get(meta['media_uid'])
    # Operator contract: release-follow is series-only. A film sequel is a distinct work, not an episode.
    if enabled and meta['media_type'] != 'tv':
        enabled = False
    bkey=str(old.get('baseline_release_key') or ''); bs=int(old.get('baseline_season') or 0); be=int(old.get('baseline_episode') or 0); ba=str(old.get('baseline_air_date') or ''); status=str(old.get('title_status') or '')
    if enabled and not bkey and meta['media_type']=='tv':
        try:
            snap=tmdb_release_snapshot(meta['tmdb_id']); status=str(snap.get('status') or ''); bkey=str(snap.get('release_key') or ''); bs=int(snap.get('season') or 0); be=int(snap.get('episode') or 0); ba=str(snap.get('air_date') or '')
        except Exception:
            bs,be=_source_max(job); bkey=f'S{bs:02d}E{be:02d}' if bs and be else ''
    with _con() as con:
        con.execute('''INSERT INTO media_release_monitors(media_uid,tmdb_id,media_type,title,catalog_url,enabled,explicit_override,title_status,baseline_release_key,baseline_season,baseline_episode,baseline_air_date,state,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(media_uid) DO UPDATE SET tmdb_id=excluded.tmdb_id,media_type=excluded.media_type,title=excluded.title,catalog_url=excluded.catalog_url,enabled=excluded.enabled,explicit_override=excluded.explicit_override,title_status=CASE WHEN excluded.title_status<>'' THEN excluded.title_status ELSE media_release_monitors.title_status END,baseline_release_key=CASE WHEN COALESCE(media_release_monitors.baseline_release_key,'')='' THEN excluded.baseline_release_key ELSE media_release_monitors.baseline_release_key END,baseline_season=CASE WHEN COALESCE(media_release_monitors.baseline_season,0)=0 THEN excluded.baseline_season ELSE media_release_monitors.baseline_season END,baseline_episode=CASE WHEN COALESCE(media_release_monitors.baseline_episode,0)=0 THEN excluded.baseline_episode ELSE media_release_monitors.baseline_episode END,baseline_air_date=CASE WHEN COALESCE(media_release_monitors.baseline_air_date,'')='' THEN excluded.baseline_air_date ELSE media_release_monitors.baseline_air_date END,state=CASE WHEN excluded.enabled=0 THEN 'OFF' WHEN COALESCE(media_release_monitors.pending_release_key,'')<>'' THEN media_release_monitors.state ELSE 'MONITORED' END,updated_at=excluded.updated_at''',(meta['media_uid'],meta['tmdb_id'],meta['media_type'],meta['title'],meta['catalog_url'],1 if enabled else 0,'ON' if enabled else 'OFF',status,bkey,bs,be,ba,'MONITORED' if enabled else 'OFF',now,now))
    missing = not _has_full_source(job)
    cur=_get(meta['media_uid'])
    if enabled and missing and not str(cur.get('pending_release_key') or ''):
        if meta['media_type']=='tv' and bkey:
            with _con() as con: con.execute("UPDATE media_release_monitors SET pending_release_key=?,pending_season=?,pending_episode=?,pending_air_date=?,state='WAITING_FOR_TRANSLATION',updated_at=? WHERE media_uid=?",(bkey,bs,be,ba,now,meta['media_uid']))
        elif meta['media_type']=='movie':
            with _con() as con: con.execute("UPDATE media_release_monitors SET pending_release_key='MOVIE',state='WAITING_FOR_TRANSLATION',updated_at=? WHERE media_uid=?",(now,meta['media_uid']))
    return state_for_job(job)


def ensure_auto_for_job(job:dict[str,Any],reason:str='watched')->dict[str,Any]:
    # Cost safety: automatic discovery must never opt a title into paid web-search monitoring.
    # Monitoring remains available only after an explicit operator/user ON action.
    meta=_identity(job); old=_get(meta['media_uid'])
    if str(old.get('explicit_override') or '').upper()=='ON':
        if old.get('enabled') and not _has_full_source(job) and not str(old.get('pending_release_key') or ''):
            return set_for_job(job,True)
        return state_for_job(job)
    if old.get('enabled'):
        with _con() as con: con.execute("UPDATE media_release_monitors SET enabled=0,state='OFF',updated_at=? WHERE media_uid=?",(int(time.time()),meta['media_uid']))
    return state_for_job(job)

def schedules()->list[dict[str,Any]]:
    ensure_schema(); out=[]
    with _con() as con: rows=con.execute('SELECT * FROM media_release_monitors ORDER BY media_uid').fetchall()
    for r in rows:
        d=_row(r); enabled=bool(d.get('enabled')) and str(d.get('explicit_override') or '').upper() == 'ON' and str(d.get('media_type') or '').lower() == 'tv'; pending=enabled and bool(d.get('pending_release_key')) and d.get('state') not in {'NOTIFIED','OFF'}; terminal=str(d.get('title_status') or '').lower() in {'ended','canceled'} and not pending
        cron='17 */6 * * *' if pending else ('31 5 * * 1' if terminal else '23 5 * * *')
        out.append({'schedule_id':schedule_id(str(d['media_uid'])),'media_uid':str(d['media_uid']),'enabled':enabled,'cron_expression':cron,'state':str(d.get('state') or ''),'media_type':str(d.get('media_type') or '')})
    return out

def tick(media_uid:str,occurrence_id:str,localization_probe:Callable[[dict[str,Any],dict[str,Any]],dict[str,Any]])->dict[str,Any]:
    ensure_schema(); now=int(time.time()); row=_get(media_uid)
    if not row or not row.get('enabled') or str(row.get('explicit_override') or '').upper() != 'ON' or str(row.get('media_type') or '').lower() != 'tv': return {'status':'DONE','accepted':True,'reason':'MONITOR_DISABLED','media_uid':media_uid,'notification':None}
    changed={}; today=dt.datetime.now(dt.timezone.utc).date()
    if row.get('media_type')=='tv' and (not row.get('last_release_check') or now-int(row.get('last_release_check') or 0)>=20*3600):
        try:
            snap=tmdb_release_snapshot(int(row['tmdb_id'])); changed['last_release_check']=now; changed['title_status']=snap.get('status') or row.get('title_status') or ''; latest=str(snap.get('release_key') or ''); baseline=str(row.get('baseline_release_key') or '')
            if latest and not baseline: changed.update({'baseline_release_key':latest,'baseline_season':snap.get('season'),'baseline_episode':snap.get('episode'),'baseline_air_date':snap.get('air_date'),'state':'MONITORED'})
            elif latest and _release_tuple(latest)>_release_tuple(baseline) and latest!=str(row.get('notified_release_key') or ''):
                air=str(snap.get('air_date') or ''); due=True
                if air:
                    try: due=dt.date.fromisoformat(air)<=today
                    except Exception: pass
                changed.update({'pending_release_key':latest,'pending_season':snap.get('season'),'pending_episode':snap.get('episode'),'pending_air_date':air,'pending_episode_title':snap.get('episode_title') or '', 'state':'WAITING_FOR_TRANSLATION' if due else 'RELEASE_KNOWN'})
        except Exception: changed['last_release_check']=now
    if changed:
        assigns=','.join(f'{k}=?' for k in changed)+',updated_at=?'; vals=list(changed.values())+[now,media_uid]
        with _con() as con: con.execute(f'UPDATE media_release_monitors SET {assigns} WHERE media_uid=?',vals)
        row=_get(media_uid)
    pending=str(row.get('pending_release_key') or '')
    if pending:
        air=str(row.get('pending_air_date') or ''); due=True
        if air:
            try: due=dt.date.fromisoformat(air)<=today
            except Exception: pass
        if due:
            release={'release_key':pending,'season':int(row.get('pending_season') or 0),'episode':int(row.get('pending_episode') or 0),'air_date':air,'episode_title':str(row.get('pending_episode_title') or '')}; probe=localization_probe(row,release) or {}
            with _con() as con: con.execute('UPDATE media_release_monitors SET last_localization_check=?,updated_at=? WHERE media_uid=?',(now,now,media_uid))
            if probe.get('available'):
                row=_get(media_uid)
                if str(row.get('notified_release_key') or '')==pending: return {'status':'DONE','accepted':True,'reason':'ALREADY_NOTIFIED','media_uid':media_uid,'notification':None}
                if str(row.get('alert_claim_occurrence') or ''): return {'status':'DONE','accepted':True,'reason':'ALERT_ALREADY_CLAIMED','media_uid':media_uid,'notification':None}
                with _con() as con: con.execute('UPDATE media_release_monitors SET state=?,localization_type=?,localization_source=?,alert_claim_occurrence=?,alert_claimed_at=?,updated_at=? WHERE media_uid=?',('LOCALIZATION_AVAILABLE',str(probe.get('capability') or 'uk_audio'),str(probe.get('provider') or 'home_media'),occurrence_id,now,now,media_uid))
                return {'status':'DONE','accepted':True,'reason':'LOCALIZATION_AVAILABLE','media_uid':media_uid,'notification':{'media_uid':media_uid,'release_key':pending,'title':str(row.get('title') or ''),'season':release['season'],'episode':release['episode'],'episode_title':release['episode_title'],'air_date':release['air_date'],'capability':str(probe.get('capability') or 'uk_audio'),'provider':str(probe.get('provider') or 'Home'),'occurrence_id':occurrence_id}}
    return {'status':'DONE','accepted':True,'reason':str(_get(media_uid).get('state') or 'MONITORED'),'media_uid':media_uid,'notification':None}

def ack(media_uid:str,release_key:str,occurrence_id:str,message_id:int|None)->dict[str,Any]:
    now=int(time.time()); row=_get(media_uid)
    if not row or str(row.get('pending_release_key') or '')!=release_key or str(row.get('alert_claim_occurrence') or '')!=occurrence_id: raise ValueError('ALERT_ACK_MISMATCH')
    with _con() as con: con.execute("UPDATE media_release_monitors SET notified_release_key=?,telegram_message_id=?,baseline_release_key=?,baseline_season=pending_season,baseline_episode=pending_episode,baseline_air_date=pending_air_date,pending_release_key=NULL,pending_season=NULL,pending_episode=NULL,pending_air_date=NULL,pending_episode_title=NULL,state='NOTIFIED',alert_claim_occurrence=NULL,alert_claimed_at=NULL,updated_at=? WHERE media_uid=?",(release_key,message_id,release_key,now,media_uid))
    return {'status':'ok','media_uid':media_uid,'release_key':release_key,'message_id':message_id}

def release_claim(media_uid:str,occurrence_id:str)->dict[str,Any]:
    now=int(time.time())
    with _con() as con: con.execute("UPDATE media_release_monitors SET alert_claim_occurrence=NULL,alert_claimed_at=NULL,state=CASE WHEN pending_release_key IS NOT NULL THEN 'WAITING_FOR_TRANSLATION' ELSE state END,updated_at=? WHERE media_uid=? AND alert_claim_occurrence=?",(now,media_uid,occurrence_id))
    return {'status':'ok'}
