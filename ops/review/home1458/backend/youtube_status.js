(() => {
  const video=document.querySelector('video');
  const player=document.getElementById('ytlr-player__player-container-player');
  const response=player?.getPlayerResponse?.()||{};
  const data=player?.getVideoData?.()||{};
  const rawId=String(data.video_id||response?.videoDetails?.videoId||'');
  const validId=/^[A-Za-z0-9_-]{11}$/.test(rawId);
  const state=typeof player?.getPlayerState==='function'?Number(player.getPlayerState()):null;
  const active=!!video&&!video.ended&&validId&&(state===null?video.readyState>=1:[1,2,3].includes(state));
  const id=active?rawId:'';
  const matches=!!id&&response?.videoDetails?.videoId===id;
  const details=matches?(response.videoDetails||{}):{};
  const thumbs=(details.thumbnail?.thumbnails||[]).filter(x=>x&&x.url);
  const live=active&&(video.duration===Infinity||(matches&&response?.microformat?.playerMicroformatRenderer?.liveBroadcastDetails?.isLiveNow===true)||data.is_live===true);
  let duration=active&&Number.isFinite(video.duration)?Number(video.duration):0;
  let canSeek=false;
  try{
    const ranges=video?.seekable;
    if(active&&ranges?.length){const end=ranges.end(ranges.length-1);canSeek=Number.isFinite(end)&&end>ranges.start(0);if(!duration&&canSeek)duration=end;}
  }catch(e){}
  let frames=null,dropped=null;
  try{const q=video?.getVideoPlaybackQuality?.();frames=q?.totalVideoFrames??video?.webkitDecodedFrameCount??null;dropped=q?.droppedVideoFrames??video?.webkitDroppedFrameCount??null;}catch(e){}
  return {
    running:active,backend:'youtube-web',source:'YouTube Web',url:location.href,video_id:id,
    'media-title':active?String(details.title||'').trim():'',
    'display-title':active?String(details.title||'').trim():'',
    channel:active?String(details.author||'').trim()||null:null,
    poster:active?(thumbs.length?thumbs[thumbs.length-1].url:'https://i.ytimg.com/vi/'+id+'/hqdefault.jpg'):null,
    pause:video?video.paused:null,ended:video?video.ended:null,seeking:video?video.seeking:null,
    player_state:state,buffering:active&&!video.paused&&video.readyState<3,
    'time-pos':active?Number(video.currentTime||0):0,duration,is_live:!!live,is_seekable:canSeek,
    fullscreen:true,quality:active&&video.videoWidth?String(video.videoWidth)+'×'+String(video.videoHeight):null,
    video_width:active?video.videoWidth:0,video_height:active?video.videoHeight:0,
    ready_state:active?video.readyState:0,playback_rate:active?video.playbackRate:0,
    decoded_frames:frames,dropped_frames:dropped,
    logged_in:window.ytcfg?.get?.('LOGGED_IN')||false
  };
})()
