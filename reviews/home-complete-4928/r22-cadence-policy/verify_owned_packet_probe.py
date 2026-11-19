"""Real packet timestamp probe on generated loopback HLS; no TV/MPV mutation."""
from functools import partial
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from bounded_packet_pts_probe import BoundedPacketPTSProbe
from cadence_policy import constant_pts_rate


def run():
    with tempfile.TemporaryDirectory(prefix='r22-packet-fixture-') as directory:
        root=Path(directory)
        for name,variable in [('fixed',False),('variable',True)]:
            folder=root/name;folder.mkdir()
            argv=['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','testsrc2=size=96x64:rate='+('60' if variable else '25'),
                '-t','6','-an']
            if variable:argv+=['-vf','select=not(mod(n\\,2))+not(mod(n\\,3))','-fps_mode','vfr']
            argv+=['-c:v','libx264','-preset','ultrafast','-g','25','-sc_threshold','0',
                '-f','hls','-hls_time','1','-hls_list_size','0',
                '-hls_segment_filename',str(folder/'part%d.ts'),str(folder/'fixture.m3u8')]
            subprocess.run(argv,check=True,capture_output=True,timeout=12)
        class Handler(SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
        thread=threading.Thread(target=server.serve_forever,name='owned-cadence-fixture')
        thread.start()
        try:
            results={}
            for name in ['fixed','variable']:
                log=root/(name+'.stderr')
                with log.open('w') as error:
                    def owned_popen(argv,**kwargs):
                        kwargs['stderr']=error
                        return subprocess.Popen(argv,**kwargs)
                    probe=BoundedPacketPTSProbe(popen=owned_popen)
                    url='http://127.0.0.1:'+str(server.server_address[1])+'/'+name+'/fixture.m3u8'
                    values=probe(url,2.,timeout=3)
                results[name]=dict(packet_pts_count=len(values) if values else 0,
                    classified_fps=constant_pts_rate(values),
                    safe_fixture_error=log.read_text()[-1200:])
            return dict(status='PASS' if results['fixed']['classified_fps']==25
                        and results['variable']['packet_pts_count']>=20
                        and results['variable']['classified_fps'] is None else 'UNVERIFIED',
                results=results,actual_ffprobe_packets=True,
                fixture_generated=True,loopback_only=True,external_network_requests=0,
                physical_tv_output=False,display_refresh_changed=False,
                actual_tv_cadence_acceptance=False)
        finally:
            server.shutdown();server.server_close();thread.join(2)

if __name__=='__main__':
    try:print(json.dumps(run()))
    except Exception as error:
        print(json.dumps(dict(status='UNVERIFIED',error_type=type(error).__name__,
            safe_detail=str(error) if isinstance(error,RuntimeError) else 'owned fixture failure',
            physical_tv_output=False,display_refresh_changed=False)))
        raise SystemExit(1)
