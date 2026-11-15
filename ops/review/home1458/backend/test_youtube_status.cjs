const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const js=fs.readFileSync(process.argv[2]||__dirname+'/youtube_status.js','utf8');
let count=0;
function probe({video={},data={},response={},state=1}={}){
  const v={duration:120,currentTime:12,ended:false,paused:false,seeking:false,readyState:4,
    videoWidth:1280,videoHeight:720,playbackRate:1,seekable:{length:1,start:()=>0,end:()=>120},
    getVideoPlaybackQuality:()=>({totalVideoFrames:100,droppedVideoFrames:0}),...video};
  const p={getVideoData:()=>({video_id:'AAAAAAAAAAA',...data}),
    getPlayerResponse:()=>({videoDetails:{videoId:'AAAAAAAAAAA',title:'Current',author:'Author'},...response})};
  if(state!==null)p.getPlayerState=()=>state;
  return vm.runInNewContext(js,{document:{querySelector:()=>v,getElementById:()=>p},
    location:{href:'https://youtube.com/tv'},navigator:{mediaSession:{metadata:{title:'Previous'}}},window:{}});
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
console.log(JSON.stringify({tests:count,failures:0,scope:'synthetic DOM; no browser or network'}));
