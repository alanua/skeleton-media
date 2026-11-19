const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const source=path.join(__dirname,'youtube_lounge_xhr_observer.py');
const extract=spawnSync('python3',['-c',"import ast,pathlib,sys; t=ast.parse(pathlib.Path(sys.argv[1]).read_text()); print(next(ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='READ_RECEIVER_FN' for x in n.targets)))",source],{encoding:'utf8'});
assert.equal(extract.status,0,extract.stderr);
const state=new WeakMap();let reads=0;
class XHR {constructor(url,text='12\n[[1,["noop"]]]',ready=3){state.set(this,{url,text,ready});}}
Object.defineProperties(XHR.prototype,{
  responseURL:{get(){return state.get(this).url}},
  readyState:{get(){return state.get(this).ready}},
  responseText:{get(){reads++;const value=state.get(this).text;if(value===null)throw new Error('unavailable');return value}}
});
const read=vm.runInNewContext('('+extract.stdout+')',{XMLHttpRequest:XHR,Reflect,Object});
const url='https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&RID=rpc&AID=0&SID=private-session';
let tests=0;function check(fn){reads=0;fn();tests++}
check(()=>{const x=new XHR(url);const before=Reflect.ownKeys(x);const out=read.call([x]);assert.equal(out.length,1);assert.equal(reads,1);assert.deepEqual(Reflect.ownKeys(x),before)});
check(()=>{const x=new XHR(url);Object.defineProperty(x,'responseURL',{get(){throw new Error('own accessor must never run')}});assert.equal(read.call([x]).length,1)});
check(()=>{const out=read.call([new XHR('https://other.org/api/lounge/bc/bind?device=LOUNGE_SCREEN&RID=rpc'),new XHR(url.replace('LOUNGE_SCREEN','REMOTE_CONTROL'))]);assert.equal(out.length,0);assert.equal(reads,0)});
check(()=>{assert.equal(read.call([new XHR(url.replace('RID=rpc','RID=1'))]).length,0);assert.equal(reads,0)});
check(()=>{assert.equal(read.call([new XHR(url,'x'.repeat(262145))])[0].too_large,true);assert.equal(read.call([new XHR(url,null)])[0].text_unavailable,true)});
check(()=>{assert.equal(read.call(Array.from({length:20},()=>new XHR(url))).length,16);assert.equal(reads,16)});
console.log(JSON.stringify({tests,failures:0,scope:'synthetic native getter access; no browser/network'}));
