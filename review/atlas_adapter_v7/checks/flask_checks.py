import pytest
from flask import Flask,abort
from review.home_atlas_live_adapter_20261125 import register

@pytest.fixture
def rig(tmp_path):
    app=Flask(__name__);app.testing=True
    device=['phone'];clear=[True];calls=[]
    rows=[dict(history_id=f'{i:016x}',title=f'Title {i}',media_type='movie',tmdb_id=i+1,year=2020) for i in range(301)]
    rows.append(dict(history_id='f'*16,title='Hidden',media_type='movie',tmdb_id=777,hidden=True))
    def intake(s,d):calls.append((s['state']['selected_id'],d));return dict(status='saved_discovery_queued',job_id='fixture')
    register(app,require=lambda:None,device_id=lambda:device[0],history_rows=lambda:rows,
      search_rows=lambda q:[dict(title='New',media_type='movie',tmdb_id=999),dict(title='Hidden',media_type='movie',tmdb_id=777)],
      confirm_intake=intake,display_clear=lambda:clear[0],static=tmp_path,poster_safe=lambda p:False)
    c=app.test_client();origin={'Origin':'http://localhost'}
    s=c.post('/api/movie-atlas/sessions',headers=origin).get_json()
    base='/api/movie-atlas/sessions/'+s['code']
    h={**origin,'X-Atlas-Token':s['controller_token']}
    return c,s,base,h,device,clear,calls,rows

def test_all_saved_search_and_paging(rig):
    c,s,b,h,*_=rig
    assert s['catalog_window']['total']==301 and len(s['catalog'])==24
    r=c.post(b+'/catalog',headers=h,json=dict(revision=s['revision'],scope='saved',query='',page=13)).get_json()
    assert r['catalog_window']['total']==301 and len(r['catalog'])==13
    r=c.post(b+'/catalog',headers=h,json=dict(revision=r['revision'],scope='saved',query='Title 300',page=1)).get_json()
    assert r['catalog_window']['total']==1 and r['catalog'][0]['title']=='Title 300'

def test_confirm_only_and_display_cannot_write(rig):
    c,s,b,h,device,clear,calls,rows=rig
    r=c.post(b+'/catalog',headers=h,json=dict(revision=s['revision'],scope='tmdb',query='new',page=1)).get_json()
    assert r['catalog_window']['provider_limited'] and len(r['catalog'])==1
    r=c.put(b,headers=h,json=dict(revision=r['revision'],state={'selected_id':'tmdb-movie-999'})).get_json()
    assert not calls
    display=c.post('/api/movie-atlas/join',headers={'Origin':'http://localhost'},json={'code':s['code']}).get_json()
    assert c.post(b+'/handoff',headers={**h,'X-Atlas-Token':display['display_token']},json={'revision':r['revision']}).status_code==403
    assert c.post(b+'/handoff',headers=h,json={'revision':r['revision']-1}).status_code==409
    device[0]='other'
    assert c.post(b+'/handoff',headers=h,json={'revision':r['revision']}).status_code==403
    device[0]='phone'
    assert c.post(b+'/handoff',headers=h,json={'revision':r['revision']}).get_json()['status']=='saved_discovery_queued'
    assert calls==[('tmdb-movie-999','phone')]

def test_saved_confirm_never_calls_discovery_and_rechecks_hidden(rig):
    c,s,b,h,device,clear,calls,rows=rig
    r=c.put(b,headers=h,json=dict(revision=s['revision'],state={'selected_id':s['catalog'][0]['id']})).get_json()
    assert c.post(b+'/handoff',headers=h,json={'revision':r['revision']}).get_json()['status']=='already_saved'
    rows[0]['hidden']=True
    assert c.post(b+'/handoff',headers=h,json={'revision':r['revision']}).status_code==409
    assert not calls

def test_origin_owner_and_capability_fail_closed(rig):
    c,s,b,h,device,clear,calls,rows=rig
    assert c.post(b+'/handoff',headers={'X-Atlas-Token':h['X-Atlas-Token']},json={'revision':s['revision']}).status_code==403
    assert c.get('/api/movie-atlas/capabilities').get_json()['presentation_ready'] is False
    clear[0]=False
    assert c.post('/api/movie-atlas/sessions',headers=h).status_code==409
    device[0]='unknown'
    assert c.get('/api/movie-atlas/capabilities').status_code==403
