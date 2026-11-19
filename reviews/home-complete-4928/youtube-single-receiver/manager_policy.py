import json,time
from pathlib import Path

def manual_receiver_policy(ws,seq,root=None):
 # Optional, explicitly approved policy. No extra process or transport.
 import hashlib
 root=Path(root) if root is not None else Path.home()/'.local/lib/skeleton-youtube'
 config=root/'manual-receiver-policy.json'
 try:
  if not config.exists():return {'status':'not_configured','applied':False}
  policy=json.loads(config.read_text())
  if policy.get('schema')!='skeleton.youtube.manual_receiver.v1':return {'status':'invalid_policy','applied':False}
  if policy.get('enabled') is not True:return {'status':'disabled','applied':False}
  expected='322c430ea1d976857338b2fae36cac0d3f030f1fc9606c500d6750ac56dc1776'
  if policy.get('source_sha256')!=expected:return {'status':'unapproved_source','applied':False}
  source=(root/'manual-receiver-apply.js').read_bytes()
  if len(source)>12000 or hashlib.sha256(source).hexdigest()!=expected:return {'status':'source_integrity_failed','applied':False}
  ident=2000000+seq
  ws.send(json.dumps({'id':ident,'method':'Runtime.evaluate','params':{'expression':source.decode(),'returnByValue':True,'awaitPromise':True}}))
  deadline=time.monotonic()+6
  while time.monotonic()<deadline:
   message=json.loads(ws.recv())
   if message.get('id')!=ident:continue
   result=message.get('result') or {}
   if message.get('error') or result.get('exceptionDetails'):return {'status':'policy_expression_failed','applied':False}
   value=((result.get('result') or {}).get('value')) or {}
   allowed=('status','applied','autoconnect_registered','manual_screen_preserved','paired_count','guard_retained','rollback_required')
   return {key:value[key] for key in allowed if key in value}
  return {'status':'policy_response_deadline','applied':False}
 except Exception:
  # Never copy arbitrary provider exceptions, URLs or identity values to state.
  return {'status':'policy_unavailable','applied':False}

