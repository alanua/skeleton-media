const vm=require('node:vm');const assert=require('node:assert/strict');const fs=require('node:fs');
const APPLY=fs.readFileSync(process.argv[2],'utf8'),ROLLBACK=fs.readFileSync(process.argv[3],'utf8');
function hash(fn){return require('node:crypto').createHash('sha256').update(fn.toString()).digest('hex')}
function fixture(){
 const store=new Map([
  ['yt.leanback.default::yt_mdx_screen',JSON.stringify({data:{name:'YouTube TV',screenId:'synthetic-screen',loungeToken:'synthetic-token'}})],
  ['yt.leanback.default::migrated-mdx-paired-devices',JSON.stringify({data:[{type:'REMOTE_CONTROL',name:'Redmi',id:'one'},{type:'REMOTE_CONTROL',name:'Pixel',id:'two'}]})]
 ]);
 let cancels=0,registrations=0;
 const proto={P(){registrations++;return{unsubscribe(){}}}};
 const service=Object.assign(Object.create(proto),{actionType:'HANDOFF_FEATURE_TYPE_LR_AUTOCONNECT',isInitialized:true,actionId:'synthetic-action',j:{synthetic:true},ek(){this.P();this.actionId='restored-action'}});
 const native={DGc:{},I:{get(){return service}},async US(s){assert.equal(s,service);cancels++;if(window.testCancel==='reject')throw Error('synthetic rejection');s.actionId=null;s.j=null;if(window.testCancel==='pending')return new Promise(()=>{})}};
 const window={_yttv:native,tectonicConfig:{featureSwitches:{castConfig:{supportsCastV2:false},mdxConfig:{enableCastToNative:false}}}};
 window.crypto=require('node:crypto').webcrypto;
 const expected={cancel:hash(native.US),registrar:hash(service.P),initialize:hash(service.ek)};
 const source=APPLY.replace(/const EXPECTED=.*?;/,'const EXPECTED='+JSON.stringify(expected)+';');
 const context=vm.createContext({window,TextEncoder,setTimeout:(fn)=>setTimeout(fn,20),clearTimeout,localStorage:{getItem:k=>store.get(k)||null,setItem(){throw Error('storage write forbidden')},removeItem(){throw Error('pairing removal forbidden')}},console});
 return{context,window,store,service,proto,native,run:()=>vm.runInContext(source,context),rollback:()=>vm.runInContext(ROLLBACK,context),counts:()=>({cancels,registrations})};
}
(async()=>{
 let count=0;
 {const f=fixture(),before=JSON.stringify([...f.store]);const r=await f.run();assert.equal(r.applied,true);assert.equal(r.paired_count,2);assert.equal(f.service.actionId,null);assert.equal(JSON.stringify([...f.store]),before);assert.equal(r.playback_command_sent,false);count++}
 {const f=fixture();await f.run();await f.run();assert.equal(f.counts().cancels,1);count++}
 {const f=fixture();await f.run();f.service.P({handoffParams:{callType:'HANDOFF_CALL_TYPE_INITIATE_ACTION'}});assert.equal(f.counts().registrations,0);count++}
 {const f=fixture(),original=f.service.P;await f.run();const r=await f.rollback();assert.equal(r.restored,true);assert.equal(f.service.P,original);assert.equal(Object.hasOwn(f.service,'P'),false);assert.equal(f.counts().registrations,1);count++}
 {const f=fixture();Object.defineProperty(f.service,'P',{value:f.service.P,configurable:true,writable:true,enumerable:false});const original=Object.getOwnPropertyDescriptor(f.service,'P');await f.run();await f.rollback();assert.deepEqual(Object.getOwnPropertyDescriptor(f.service,'P'),original);count++}
 {const f=fixture();f.service.actionType='HANDOFF_FEATURE_TYPE_LR_COMMENTS';assert.equal((await f.run()).applied,false);assert.equal(f.counts().cancels,0);count++}
 {const f=fixture();f.window.tectonicConfig.featureSwitches.castConfig.supportsCastV2=true;assert.equal((await f.run()).applied,false);assert.equal(f.counts().cancels,0);count++}
 {const f=fixture();f.store.set('yt.leanback.default::yt_mdx_screen',JSON.stringify({data:{name:'Other',screenId:'synthetic-screen'}}));assert.equal((await f.run()).applied,false);assert.equal(f.counts().cancels,0);count++}
 {const f=fixture();f.store.set('yt.leanback.default::migrated-mdx-paired-devices',JSON.stringify({data:[]}));assert.equal((await f.run()).applied,false);count++}
 {const f=fixture();delete f.window._yttv.US;assert.equal((await f.run()).applied,false);count++}
 {const f=fixture();f.native.US=async()=>{throw Error('synthetic cancel failure')};assert.equal((await f.run()).status,'vendor_contract_changed');assert.equal(f.counts().cancels,0);count++}
 {const f=fixture();await f.run();f.service.P=function newer(){};assert.equal((await f.run()).status,'policy_contract_changed');assert.equal((await f.rollback()).restored,false);count++}
 {const f=fixture();f.window.testCancel='reject';const r=await f.run();assert.equal(r.status,'native_cancel_failed');assert.equal(r.guard_retained,true);assert.notEqual(f.service.P,f.proto.P);await f.rollback();assert.equal(f.service.P,f.proto.P);count++}
 {const f=fixture();f.window.testCancel='pending';const r=await f.run();assert.equal(r.status,'native_cancel_unconfirmed');assert.equal(r.guard_retained,true);await f.run();assert.equal(f.counts().cancels,1);await f.rollback();count++}
 {const f=fixture();await f.run();f.service.actionId='unexpected';assert.equal((await f.run()).status,'unexpected_registration');assert.equal(f.counts().cancels,1);count++}
 {const f=fixture();f.service.actionId=null;await f.run();assert.equal(f.counts().cancels,0);await f.rollback();assert.equal(f.counts().registrations,0);count++}
 {const f=fixture();f.service.isInitialized=false;assert.equal((await f.run()).applied,false);count++}
 {const f=fixture();Object.preventExtensions(f.service);assert.equal((await f.run()).status,'service_not_writable');assert.equal(f.counts().cancels,0);count++}
 console.log(JSON.stringify({tests:count,failures:0,synthetic_only:true,network_calls:0}));
})().catch(e=>{console.error(e);process.exitCode=1});
