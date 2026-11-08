import unittest,sys,io,json,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integrations'/'legacy_dam'))
from failure_diagnostics import describe_failure,DiagnosticError
class FailureTests(unittest.TestCase):
 def http(self,status,body=b'',headers=None):return urllib.error.HTTPError('https://host.invalid/private?token=DO_NOT_LOG',status,'secret message',headers or {},io.BytesIO(body))
 def test_transient_and_rate_limit(self):
  d=describe_failure(self.http(429,headers={'Retry-After':'120'}));self.assertTrue(d['retryable']);self.assertEqual(d['retry_after_seconds'],120)
 def test_secret_safe(self):
  d=describe_failure(self.http(403,b'<Error><Code>AccessDenied</Code><Message>DO_NOT_LOG</Message></Error>'))
  self.assertEqual(d['provider_code'],'AccessDenied');self.assertNotIn('DO_NOT_LOG',json.dumps(d));self.assertFalse(d['retryable'])
 def test_oversize_is_not_blind_retry(self):
  d=describe_failure(self.http(400,b'<Error><Code>ImageTooLarge</Code></Error>'));self.assertEqual(d['action'],'resize_or_original_fallback');self.assertFalse(d['retryable'])
 def test_nonimage_and_size(self):
  self.assertEqual(describe_failure(DiagnosticError('upstream_returned_html',http_status=200))['category'],'non_image_response')
  self.assertEqual(describe_failure(DiagnosticError('image_byte_limit',byte_limit=12))['category'],'resource_limit')
 def test_disabled_source_is_not_transient(self):
  d=describe_failure(self.http(403,b'<Error><Code>UserDisable</Code></Error>'));self.assertEqual(d['category'],'source_storage_disabled');self.assertFalse(d['retryable'])
 def test_network(self):self.assertTrue(describe_failure(TimeoutError())['retryable'])
if __name__=='__main__':unittest.main()
