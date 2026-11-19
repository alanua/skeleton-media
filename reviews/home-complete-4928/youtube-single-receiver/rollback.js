(()=>{
const KEY='__skeletonYouTubeManualReceiverV1',state=window[KEY];
if(!state)return{status:'not_applied',restored:true};
if(state.version!==1||state.service?.actionType!=='HANDOFF_FEATURE_TYPE_LR_AUTOCONNECT'||state.service.P!==state.block)return{status:'rollback_contract_changed',restored:false};
const service=state.service;
if(state.descriptor)Object.defineProperty(service,'P',state.descriptor);else delete service.P;
delete window[KEY];
let requestSent=false;
if(state.previouslyRegistered&&typeof service.ek==='function'){service.ek();requestSent=true}
return{status:'restored_native_registrar',restored:true,registration_request_sent:requestSent,phone_visibility_verified:false,playback_command_sent:false,pairing_changed:false};
})()
