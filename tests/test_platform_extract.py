import ast,json,re,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'integrations/legacy_dam'
def extract_env(file):
 s=(ROOT/file).read_text();t=ast.parse(s);nodes=[]
 for n in t.body:
  if isinstance(n,ast.FunctionDef):nodes.append(n)
  if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id in ('IMG_RE','_PARAM_KEYS') for x in n.targets):nodes.append(n)
 env={'json':json,'re':re};exec(compile(ast.Module(body=nodes,type_ignores=[]),file,'exec'),env);return env
class Extract(unittest.TestCase):
 def test_artist_nested_output(self):
  e=extract_env('ingest_infmonkeys.py');r=e['extract']({'input':{},'output':{'data':{'file_list':['https://example.com/output.png']}},'tasks':[]});self.assertIn('https://example.com/output.png',r['output_images'])
 def test_app_flat_output(self):
  e=extract_env('sync_zwy_daily.py');r=e['extract_meta']({'input':{},'output':{'file_list':['https://example.com/output.png']}},'example');self.assertEqual(r['output_images'],['https://example.com/output.png'])
if __name__=='__main__':unittest.main()
