(async()=>{
const KEY='__skeletonYouTubeManualReceiverV1',version=1;
const EXPECTED={"cancel":"f400eff4dae51849426313cfcd377dc299d588c9a524cecdfebb0f2606e54a44","registrar":"5cab2e433c6844fb69c12f70ecf48bd96fc54e5e5a3e47aa205b70a7d2e6e805","initialize":"b5670a449a00acc93582c18cf28c6df7e5eb229f78f12eb104e1db6725bf38e4"};
const n=window._yttv;
if(!n||!n.I||!n.DGc||typeof n.US!=='function')return{status:'unsupported_native_contract',applied:false};
const config=window.tectonicConfig?.featureSwitches||{};
if(config.castConfig?.supportsCastV2!==false||config.mdxConfig?.enableCastToNative!==false)return{status:'native_cast_endpoint_refused',applied:false};
let screen,paired;
try{
 screen=JSON.parse(localStorage.getItem('yt.leanback.default::yt_mdx_screen')||'null')?.data;
 paired=JSON.parse(localStorage.getItem('yt.leanback.default::migrated-mdx-paired-devices')||'null')?.data;
}catch(e){return{status:'invalid_saved_receiver',applied:false}}
if(!screen?.screenId||screen.name!=='YouTube TV'||!Array.isArray(paired)||!paired.some(x=>x?.type==='REMOTE_CONTROL'))return{status:'manual_receiver_not_verified',applied:false};
let service;
try{service=n.I.get(n.DGc)}catch(e){return{status:'native_service_unavailable',applied:false}}
if(service?.actionType!=='HANDOFF_FEATURE_TYPE_LR_AUTOCONNECT'||service.isInitialized!==true||typeof service.P!=='function'||typeof service.ek!=='function')return{status:'different_native_service',applied:false};
const old=window[KEY];
if(old){
 if(old.version!==version||old.service!==service||old.block!==service.P)return{status:'policy_contract_changed',applied:false};
 if(service.actionId)return{status:'unexpected_registration',applied:false,guard_retained:true};
 return{status:old.stage,applied:old.stage==='manual_only',autoconnect_registered:false,manual_screen_preserved:true,paired_count:paired.filter(x=>x?.type==='REMOTE_CONTROL').length};
}
if(!window.crypto?.subtle)return{status:'hash_unavailable',applied:false};
const digest=async fn=>Array.from(new Uint8Array(await window.crypto.subtle.digest('SHA-256',new TextEncoder().encode(fn.toString()))),x=>x.toString(16).padStart(2,'0')).join('');
if(await digest(n.US)!==EXPECTED.cancel||await digest(service.P)!==EXPECTED.registrar||await digest(service.ek)!==EXPECTED.initialize)return{status:'vendor_contract_changed',applied:false};
const state={version,service,previousP:service.P,descriptor:Object.getOwnPropertyDescriptor(service,'P'),previouslyRegistered:!!service.actionId,stage:'applying'};
state.block=function(){return{unsubscribe(){}}};
try{Object.defineProperty(service,'P',{value:state.block,writable:true,configurable:true,enumerable:true})}catch(e){return{status:'service_not_writable',applied:false}}
window[KEY]=state;
let timer;
try{
 if(service.actionId)await Promise.race([n.US(service),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('cancel_deadline')),4000)})]);
 if(service.actionId)throw new Error('native_action_remains');
 state.stage='manual_only';
 return{status:'manual_only',applied:true,autoconnect_registered:false,manual_screen_preserved:true,paired_count:paired.filter(x=>x?.type==='REMOTE_CONTROL').length,playback_command_sent:false,pairing_changed:false};
}catch(e){
 state.stage=e?.message==='cancel_deadline'?'native_cancel_unconfirmed':'native_cancel_failed';
 return{status:state.stage,applied:false,guard_retained:true,rollback_required:true};
}finally{if(timer)clearTimeout(timer)}
})()
