import ast,unittest,urllib.parse
from pathlib import Path
s=(Path(__file__).resolve().parents[1]/'integrations/legacy_dam/platform_sync.py').read_text();tree=ast.parse(s);node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='walk');ns={'urllib':__import__('urllib')};exec(compile(ast.Module(body=[node],type_ignores=[]),'platform_sync.py','exec'),ns);walk=ns['walk']
class CursorTests(unittest.TestCase):
 def test_discovery_and_execution_cursor(self):
  for body in (None,{'workflowId':'w'}):
   calls=[]
   def call(p,b=None):
    calls.append((p,b));return {'items':[len(calls)],'pageInfo':{'hasMore':len(calls)==1,'nextPageToken':'next'}}
   self.assertEqual(list(walk(call,'/api',body)),[[1],[2]])
   self.assertTrue('pageToken=next' in calls[1][0] if body is None else calls[1][1]['pageToken']=='next')
 def test_missing_and_repeated_cursor_fail(self):
  for token in (None,'same'):
   def call(*args):return {'items':[],'pageInfo':{'hasMore':True,'nextPageToken':token}}
   with self.assertRaises(RuntimeError):list(walk(call,'/api'))
 def test_bad_response_fails(self):
  with self.assertRaises(RuntimeError):list(walk(lambda *a:{'data':[]},'/api'))
if __name__=='__main__':unittest.main()
