#!/usr/bin/env python3
from pathlib import Path
import json,re,hashlib,sys
N=Path(__file__).resolve().parent; C=json.loads((N/'WEB_DESIGN_CANON.json').read_text()); S=(N/'src/com/skeleton/home/MainActivity.java').read_text()
checks={}
rgb={'BG':'Color.rgb(10,13,16)','CARD':'Color.rgb(21,26,31)','CARD2':'Color.rgb(26,32,38)','TILE':'Color.rgb(18,23,28)','ACTION':'Color.rgb(23,29,35)','INPUT':'Color.rgb(17,22,26)','LINE':'Color.rgb(35,42,49)','TEXT':'Color.rgb(245,246,247)','MUTED':'Color.rgb(141,150,159)','ACCENT':'Color.rgb(255,106,0)','ACCENT_SOFT':'Color.rgb(42,28,19)','GREEN':'Color.rgb(88,213,123)'}
for name,val in rgb.items(): checks['color_'+name]=val in S
checks['adaptive_actual_card_width']='width-2*sidePx-2*gapPx' in S
checks['youtube_percent_sectors']=all(x in S for x in ('d*.50f','d*.32f','d*.38f'))
checks['youtube_breakpoints']=all(x in S for x in ('heightDp<=780','screenDp<=360','capDp=292','capDp=250','capDp=244'))
checks['web_mdi_names']=all(name in S for name in ['youtube','cast-connected','television','gamepad-variant','home','movie-open','monitor-dashboard','devices','keyboard-return','chevron-up','chevron-left','chevron-right','chevron-down','rewind-15','fast-forward-15','volume-off','volume-high'])
checks['no_unicode_icons']=not any(x in S for x in ['⌂','▦','▶❚❚','🔇','📡'])
checks['sk_dashboard_pilot']=all(x in S for x in ['/api/native/skeleton-dashboard','void pollSk()','skList','Skeleton · Підключено'])
checks['no_media_dashboard_in_sk']=not any(x in S for x in ['Медіанода','Сервіси Home Edge','Chromecast receiver'])
checks['custom_controls']=all(x in S for x in ['class SkeletonSlider','class SkeletonSelect','class SkeletonToggle']) and 'new SeekBar' not in S and 'new Spinner' not in S
# Source assets must still be byte-identical with the recorded web canon.
for name,meta in C['icons'].items():
 p=Path(meta['path']); checks['icon_'+name]=p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==meta['sha256']
ok=all(checks.values()); print(json.dumps({'status':'PASS' if ok else 'FAIL','checks':checks},ensure_ascii=False,indent=2)); sys.exit(0 if ok else 1)
