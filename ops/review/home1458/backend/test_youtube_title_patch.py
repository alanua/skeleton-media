import ast,hashlib,unittest
from patch_youtube_title import patch_status_js,remaining_ast
class TitlePatchTests(unittest.TestCase):
    def test_preserves_every_unrelated_ast_node(self):
        source="import json\nSTATUS_JS = '''old\nexpression'''\nSECRET_SETTING = object()\ndef recover():\n    return 'untouched'\n"
        result=patch_status_js(source,"new expression",hashlib.sha256(b'old\nexpression').hexdigest())
        self.assertEqual(remaining_ast(source),remaining_ast(result))
        self.assertIn("new expression",result)
    def test_refuses_baseline_drift(self):
        with self.assertRaises(ValueError):patch_status_js("STATUS_JS='different'\n","new")
    def test_refuses_duplicate_assignments(self):
        with self.assertRaises(ValueError):patch_status_js("STATUS_JS='a'\nSTATUS_JS='b'\n","new")
    def test_refuses_nonliteral(self):
        with self.assertRaises(ValueError):patch_status_js("STATUS_JS=load()\n","new")
    def test_refuses_empty_new_expression(self):
        with self.assertRaises(ValueError):patch_status_js("STATUS_JS='old'\n","",hashlib.sha256(b'old').hexdigest())
if __name__=='__main__':unittest.main()
