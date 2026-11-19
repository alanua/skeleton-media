import unittest,tempfile,json,hashlib
from pathlib import Path
from manager_policy import manual_receiver_policy
SOURCE=Path(__file__).with_name('apply.js').read_bytes()
class Socket:
 def __init__(self,response=None):self.sent=[];self.response=response or {'id':2000007,'result':{'result':{'value':{'status':'manual_only','applied':True,'private_token':'must_not_escape'}}}}
 def send(self,s):self.sent.append(json.loads(s))
 def recv(self):return json.dumps(self.response)
class PolicyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.ws=Socket()
  self.cfg={'schema':'skeleton.youtube.manual_receiver.v1','enabled':True,'source_sha256':hashlib.sha256(SOURCE).hexdigest()}
  (self.root/'manual-receiver-apply.js').write_bytes(SOURCE);self.save()
 def save(self):(self.root/'manual-receiver-policy.json').write_text(json.dumps(self.cfg))
 def run_policy(self):return manual_receiver_policy(self.ws,7,self.root)
 def test_only_opt_in(self):
  self.cfg['enabled']=False;self.save();self.assertEqual(self.run_policy()['status'],'disabled');self.assertEqual(self.ws.sent,[])
 def test_absent_config_does_not_evaluate(self):
  (self.root/'manual-receiver-policy.json').unlink();self.assertEqual(self.run_policy()['status'],'not_configured');self.assertEqual(self.ws.sent,[])
 def test_changed_source_never_executes(self):
  (self.root/'manual-receiver-apply.js').write_text('unsafe');self.assertEqual(self.run_policy()['status'],'source_integrity_failed');self.assertEqual(self.ws.sent,[])
 def test_changed_approval_never_executes(self):
  self.cfg['source_sha256']='0'*64;self.save();self.assertEqual(self.run_policy()['status'],'unapproved_source');self.assertEqual(self.ws.sent,[])
 def test_invalid_config_does_not_evaluate(self):
  self.cfg['schema']='other';self.save();self.assertEqual(self.run_policy()['status'],'invalid_policy');self.assertEqual(self.ws.sent,[])
 def test_exact_source_and_async_acknowledgement(self):
  self.assertTrue(self.run_policy()['applied']);self.assertEqual(self.ws.sent[0]['params']['expression'],SOURCE.decode());self.assertTrue(self.ws.sent[0]['params']['awaitPromise']);self.assertNotIn('private_token',self.run_policy())
 def test_expression_failure_not_success(self):
  self.ws=Socket({'id':2000007,'result':{'exceptionDetails':{'text':'sensitive'}}});self.assertEqual(self.run_policy()['status'],'policy_expression_failed')
 def test_bad_json_does_not_log_exception(self):
  (self.root/'manual-receiver-policy.json').write_text('sensitive');r=self.run_policy();self.assertEqual(r,{'status':'policy_unavailable','applied':False})
if __name__=='__main__':unittest.main()
