"""Bounded real packet-PTS probe for an exact selected HLS source.

Private signed URL stays in a sealed anonymous input descriptor, not command
arguments or logs. No playback command, cache dump, filter or display mutation.
Only an explicitly enabled policy with a healthy buffered session may invoke it.
"""
import json
import fcntl
import math
import os
from pathlib import Path
import signal
import subprocess
from urllib.parse import urlsplit


def valid_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=16384 or any(c in value for c in '\r\n'):
        return False
    try:
        u=urlsplit(value)
        return (u.scheme in ('http','https') and bool(u.hostname) and u.username is None
                and u.password is None and not u.fragment)
    except ValueError:return False


class BoundedPacketPTSProbe:
    def __init__(self,*,popen=subprocess.Popen,ffprobe='/usr/bin/ffprobe',
                 prlimit='/usr/bin/prlimit'):
        self.popen=popen;self.ffprobe=ffprobe;self.prlimit=prlimit

    def __call__(self,url,position,*,headers=None,timeout=3):
        if (not valid_url(url) or type(position) not in (int,float)
                or not math.isfinite(position) or not 0<=position<=604800
                or type(timeout) not in (int,float) or not .5<=timeout<=3):
            return None
        options=[]
        for key,value in (headers or {}).items():
            if (not isinstance(key,str) or not isinstance(value,str) or len(value)>4096
                    or any(c in value for c in '\r\n')):return None
            key=key.lower()
            if key=='user-agent':options+=['-user_agent',value]
            elif key=='referer':options+=['-referer',value]
            elif key=='origin':options+=['-headers','Origin: '+value+'\r\n']
            else:
                # Credential-bearing/custom headers require their own canonical
                # protected transport adapter; never improvise an auth fallback.
                return None
        input_fd=output_fd=None;process=None
        try:
            if not Path(self.ffprobe).is_file() or not Path(self.prlimit).is_file():return None
            input_fd=os.memfd_create('skeleton-cadence-hls',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
            master=('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\n'+url+'\n').encode()
            os.write(input_fd,master);os.lseek(input_fd,0,os.SEEK_SET)
            fcntl.fcntl(input_fd,fcntl.F_ADD_SEALS,fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL)
            output_fd=os.memfd_create('skeleton-cadence-json',os.MFD_CLOEXEC)
            argv=[self.prlimit,'--fsize=262144','--cpu=3','--as=536870912','--',
                  self.ffprobe,'-v','error','-rw_timeout','1500000',
                  '-protocol_whitelist','file,http,https,tcp,tls,crypto',
                  *options,'-read_intervals',format(position,'.3f')+'%+1.25',
                  '-select_streams','v:0','-show_entries','stream=start_time:packet=pts_time',
                  '-of','json','-f','hls','/proc/self/fd/'+str(input_fd)]
            process=self.popen(argv,stdin=subprocess.DEVNULL,stdout=output_fd,
                stderr=subprocess.DEVNULL,pass_fds=(input_fd,),start_new_session=True)
            try:code=process.wait(timeout=timeout)
            except BaseException:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=1)
                return None
            if code!=0 or os.fstat(output_fd).st_size>262144:return None
            os.lseek(output_fd,0,os.SEEK_SET)
            data=json.loads(os.read(output_fd,262145))
            streams=data.get('streams');packets=data.get('packets')
            if not isinstance(streams,list) or len(streams)!=1 or not isinstance(packets,list):
                return None
            origin=float(streams[0]['start_time'])
            if not math.isfinite(origin):return None
            values=[]
            for packet in packets:
                raw=packet.get('pts_time') if isinstance(packet,dict) else None
                if not isinstance(raw,(str,int,float)) or isinstance(raw,bool):return None
                value=float(raw)-origin
                if not math.isfinite(value):return None
                values.append(value)
            # Packet decode order can differ from presentation order for B frames.
            # Sorting retains real PTS values; duplicate/missing timestamps are not invented.
            values.sort()
            if (not 20<=len(values)<=240 or len(set(values))!=len(values)
                    or values[0]<position-3 or values[-1]>position+4):
                return None
            return values
        except Exception:return None
        finally:
            if process is not None and process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                try:process.wait(timeout=1)
                except Exception:pass
            for fd in (input_fd,output_fd):
                if fd is not None:os.close(fd)
