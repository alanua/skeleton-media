const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const js=fs.readFileSync(process.argv[2]||__dirname+'/youtube_status.js','utf8');
let count=0;
function probe({video={},data={},response={},state=1,metadata={title:'Previous'}}={}){
  const v={duration:120,currentTime:12,ended:false,paused:false,seeking:false,readyState:4,
    videoWidth:1280,videoHeight:720,playbackRate:1,seekable:{length:1,start:()=>0,end:()=>120},
    getVideoPlaybackQuality:()=>({totalVideoFrames:100,droppedVideoFrames:0}),...video};
  const p={getVideoData:()=>({video_id:'AAAAAAAAAAA',...data}),
    getPlayerResponse:()=>({videoDetails:{videoId:'AAAAAAAAAAA',title:'Current',author:'Author'},...response})};
  if(state!==null)p.getPlayerState=()=>state;
  return vm.runInNewContext(js,{document:{querySelector:()=>v,getElementById:()=>p},
    location:{href:'https://youtube.com/tv'},navigator:{mediaSession:{metadata}},window:{},URL});
}
function check(name,fn){fn();count++}
check('current title and frame evidence',()=>{const x=probe();assert.equal(x['display-title'],'Current');assert.equal(x.decoded_frames,100);assert.equal(x.running,true)});
check('new identity rejects old title',()=>{const x=probe({data:{video_id:'BBBBBBBBBBB'}});assert.equal(x.video_id,'BBBBBBBBBBB');assert.equal(x['display-title'],'');assert.ok(x.poster.includes('BBBBBBBBBBB'));assert.ok(!JSON.stringify(x).includes('Previous'))});
check('paused is active media',()=>{const x=probe({state:2,video:{paused:true}});assert.equal(x.running,true);assert.equal(x.pause,true)});
for(const state of [-1,0,5])check('stopped state '+state,()=>{const x=probe({state});assert.equal(x.running,false);assert.equal(x.poster,null);assert.equal(x.video_id,'');assert.equal(x['display-title'],'')});
check('live Infinity duration remains active',()=>{const x=probe({video:{duration:Infinity}});assert.equal(x.running,true);assert.equal(x.is_live,true);assert.equal(x.is_seekable,true);assert.ok(Number.isFinite(x.duration))});
check('live without DVR has no seek',()=>{const x=probe({video:{duration:Infinity,seekable:{length:0}}});assert.equal(x.running,true);assert.equal(x.duration,0);assert.equal(x.is_seekable,false)});
check('ended clears stale poster',()=>{const x=probe({video:{ended:true}});assert.equal(x.running,false);assert.equal(x.poster,null)});
check('mismatched live response is ignored',()=>{const x=probe({data:{video_id:'BBBBBBBBBBB'},response:{videoDetails:{videoId:'AAAAAAAAAAA'},microformat:{playerMicroformatRenderer:{liveBroadcastDetails:{isLiveNow:true}}}}});assert.equal(x.is_live,false)});
check('missing frame API remains unverified',()=>{const x=probe({video:{getVideoPlaybackQuality:undefined}});assert.equal(x.decoded_frames,null)});

const artwork=id=>[{src:'https://i.ytimg.com/vi/'+id+'/hqdefault.jpg'}];
check('video-data title is bound to its own id',()=>{const x=probe({data:{title:'Current from data'},response:{videoDetails:undefined}});assert.equal(x['display-title'],'Current from data')});
check('current media-session title survives missing player response',()=>{const x=probe({response:{videoDetails:undefined},metadata:{title:'Current from session',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'Current from session');assert.equal(x['media-title'],'Current from session')});
check('old media-session artwork cannot lend a stale title',()=>{const x=probe({data:{video_id:'BBBBBBBBBBB'},response:{videoDetails:undefined},metadata:{title:'Previous',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'')});
check('session title without identity evidence is ignored',()=>{const x=probe({response:{videoDetails:undefined},metadata:{title:'Unbound'}});assert.equal(x['display-title'],'')});
check('mixed old and current artwork is ambiguous',()=>{const x=probe({response:{videoDetails:undefined},metadata:{title:'Ambiguous',artwork:[...artwork('AAAAAAAAAAA'),...artwork('BBBBBBBBBBB')]}});assert.equal(x['display-title'],'')});
check('unrelated image host cannot establish identity',()=>{const x=probe({response:{videoDetails:undefined},metadata:{title:'Unbound',artwork:[{src:'https://example.invalid/vi/AAAAAAAAAAA/hqdefault.jpg'}]}});assert.equal(x['display-title'],'')});
check('paused clip retains its verified session title',()=>{const x=probe({state:2,video:{paused:true},response:{videoDetails:undefined},metadata:{title:'Paused current',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'Paused current');assert.equal(x.pause,true)});
check('stopped clip clears all fallback titles and artwork',()=>{const x=probe({state:5,data:{title:'Previous'},metadata:{title:'Previous',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'');assert.equal(x.poster,null)});
check('response title wins over secondary metadata',()=>{const x=probe({data:{title:'Data'},metadata:{title:'Session',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'Current')});
check('new data title wins over stale mismatched response',()=>{const x=probe({data:{video_id:'BBBBBBBBBBB',title:'New clip'},metadata:{title:'Previous',artwork:artwork('AAAAAAAAAAA')}});assert.equal(x['display-title'],'New clip')});
check('webp artwork identifies current session title',()=>{const x=probe({response:{videoDetails:undefined},metadata:{title:'Current webp',artwork:[{src:'https://i.ytimg.com/vi_webp/AAAAAAAAAAA/mqdefault.webp'}]}});assert.equal(x['display-title'],'Current webp')});

console.log(JSON.stringify({tests:count,failures:0,scope:'synthetic DOM; no browser or network'}));
