"""Hide a paused MPV surface after the browser has been made ready."""
import json
import socket
import time
from pathlib import Path


def park_paused_mpv(command,wait=time.sleep):
    def get(name):
        value=command(['get_property',name])
        if value.get('error')!='success':raise RuntimeError('MPV property unavailable: '+name)
        return value.get('data')
    if get('pause') is not True:raise RuntimeError('MPV must be paused before parking')
    original={name:get(name) for name in ['fullscreen','ontop','window-minimized']}
    if original=={'fullscreen':False,'ontop':False,'window-minimized':True}:
        return {'status':'already_parked','surface_hidden':True,'playback_started':False}
    for name,value in [('fullscreen',False),('ontop',False),('window-minimized',True)]:
        result=command(['set_property',name,value])
        if result.get('error')!='success':raise RuntimeError('MPV did not accept parking property: '+name)
    for _ in range(6):
        if get('pause') is not True:raise RuntimeError('Playback changed concurrently during parking')
        if get('fullscreen') is False and get('ontop') is False and get('window-minimized') is True:
            return {'status':'parked','surface_hidden':True,'playback_started':False}
        wait(.1)
    raise RuntimeError('MPV surface parking postcondition not confirmed')


class MpvIPC:
    def __init__(self,path):self.path=str(path);self.sequence=0
    def __call__(self,argv):
        self.sequence+=1
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(2);connection.connect(self.path)
            connection.sendall((json.dumps({'command':argv,'request_id':self.sequence})+'\n').encode())
            buffered=b''
            while len(buffered)<1048576:
                chunk=connection.recv(65536)
                if not chunk:raise RuntimeError('MPV IPC closed without confirmation')
                buffered+=chunk
                while b'\n' in buffered:
                    line,buffered=buffered.split(b'\n',1)
                    value=json.loads(line)
                    if value.get('request_id')==self.sequence:return value
            raise RuntimeError('MPV IPC response exceeded bound')


def main():
    mode_file=Path.home()/'.local/state/tv-mode/current'
    requested_file=mode_file.with_name('requested')
    requested=requested_file.read_text().strip() if requested_file.exists() else None
    current=mode_file.read_text().strip()
    if (requested is not None and requested not in {'kiosk','chrome'}) or (current not in {'kiosk','chrome'} and requested not in {'kiosk','chrome'}):
        raise RuntimeError('No supported browser handoff context')
    # Called by the existing mode controller after pausing MPV and readying Chrome.
    result=park_paused_mpv(MpvIPC('/run/user/'+str(__import__('os').getuid())+'/skeleton-cast-mpv.sock'))
    print(json.dumps(result))


if __name__=='__main__':main()
