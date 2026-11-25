import json
from flask import Flask
from movie_atlas_live_adapter import register

def run():
    app=Flask(__name__);app.testing=True
    control={'device':'phone','ready':True,'target':True,'clear':True,'owner':None,'fail':False,'close_fail':False}
    calls=[]
    def launch(code):
        calls.append(('start',code))
        if control['fail']:return False
        control['owner']=code;control['clear']=False
        return True
    def close(code):
        calls.append(('stop',code))
        if control['close_fail']:return False
        if control['owner']==code:
            control['owner']=None;control['clear']=True
        return True
    store=register(app,require=lambda:None,device_id=lambda:control['device'],history_rows=lambda:[{'history_id':'a'*16,'title':'Test','year':2026}],search_rows=lambda q:[],confirm_intake=lambda s,d:{},display_clear=lambda:control['clear'],static='.',poster_safe=lambda p:False,presentation_ready=lambda:control['ready'],atlas_owned=lambda code:control['owner']==code,launch_display=launch,close_display=close,target_ready=lambda:control['target'])
    c=app.test_client();origin={'Origin':'http://localhost'};passed=[]
    def create():return c.post('/api/movie-atlas/sessions',headers=origin)
    def cap():return c.get('/api/movie-atlas/capabilities').get_json()['presentation_ready']
    def check(condition,name):
        assert condition,name
        passed.append(name)
    check(cap() is True,'capability_available')
    control['target']=False
    check(not cap() and create().status_code==409 and not calls,'wrong_endpoint_refused')
    control['target']=True;control['ready']=False
    check(not cap() and create().status_code==409 and not calls,'unregistered_controller_refused')
    control['ready']=True;control['clear']=False
    check(not cap() and create().status_code==409 and not calls,'busy_refused')
    control['clear']=True;control['fail']=True
    check(create().status_code==409 and not store._sessions and calls[-1][0]=='stop','failed_launch_rolls_back')
    control['fail']=False;calls.clear()
    resp=create();assert resp.status_code==201,resp.get_json();s=resp.get_json();code=s['code'];token=s['controller_token'];path='/api/movie-atlas/sessions/'+code;headers={**origin,'X-Atlas-Token':token}
    check(calls==[('start',code)] and control['owner']==code,'exact_session_launched')
    check(create().status_code==409 and calls==[('start',code)],'second_launch_refused')
    control['device']='display'
    join=c.post('/api/movie-atlas/join',headers=origin,json={'code':code})
    assert join.status_code==200,join.get_json()
    display=join.get_json()['display_token'];dh={**origin,'X-Atlas-Token':display}
    check(c.get(path+'?role=display',headers=dh).status_code==200,'owned_display_polls_while_busy')
    check(c.delete(path,headers=dh).status_code==403 and control['owner']==code,'display_cannot_stop')
    check(c.delete(path,headers=headers).status_code==403 and control['owner']==code,'other_controller_cannot_stop')
    control['device']='phone';control['close_fail']=True
    check(c.delete(path,headers=headers).status_code==409 and code in store._sessions,'close_failure_retains_session')
    control['close_fail']=False
    check(c.delete(path,headers=headers).status_code==200 and control['owner'] is None and code not in store._sessions,'exact_session_closed')
    print(json.dumps({'passed':len(passed),'checks':passed,'source_only':True,'runtime_changed':False},ensure_ascii=False))
if __name__=='__main__':run()
