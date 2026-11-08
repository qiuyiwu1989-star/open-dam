import unittest,ast,sys
from pathlib import Path
r=Path(__file__).resolve().parents[1];sys.path.insert(0,str(r/'integrations'/'legacy_dam'))
from image_processing import thumbnail_params
class HTTPError(Exception):
 def __init__(self,status,*args):self.status=status
class RetrievalSafety(unittest.TestCase):
 def test_processing_query_is_command_key(self):self.assertEqual(thumbnail_params(384),{'imageMogr2/thumbnail/384x/':''})
 def test_reject_bad_dimensions(self):
  for width in (0,-1,4097,True,'384'):
   with self.assertRaises(ValueError):thumbnail_params(width)
if __name__=='__main__':unittest.main()
