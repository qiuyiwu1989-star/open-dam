"""Secret-safe failure details and proposed retry class; no retry-state mutation."""
import urllib.error,urllib.parse,xml.etree.ElementTree as ET
class DiagnosticError(ValueError):
 def __init__(self,reason,**details):super().__init__(reason);self.reason=reason;self.details=details

def describe_failure(error):
 r={'type':type(error).__name__,'category':'unknown','action':'inspect','retryable':False}
 if isinstance(error,urllib.error.HTTPError):
  status=error.code;r.update(http_status=status,host=urllib.parse.urlsplit(error.url).hostname,content_type=error.headers.get('Content-Type','').split(';')[0])
  try:r['provider_code']=ET.fromstring(error.read(8192)).findtext('Code')
  except Exception:pass
  if r.get('provider_code')=='ImageTooLarge':r.update(category='oversize_processing',action='resize_or_original_fallback')
  elif r.get('provider_code')=='UserDisable':r.update(category='source_storage_disabled',action='restore_source_storage_or_recover_copy')
  elif status in (408,425,429) or status>=500:r.update(category='transient_http',action='backoff',retryable=True)
  elif status in (401,403):r.update(category='authorization_or_expired_url',action='refresh_source_url')
  elif status in (404,410):r.update(category='missing_source_object',action='reconcile_source')
  else:r.update(category='http_rejected',action='inspect_request')
  retry_after=error.headers.get('Retry-After','')
  if retry_after.isdigit():r['retry_after_seconds']=min(int(retry_after),86400)
 elif isinstance(error,(TimeoutError,urllib.error.URLError,ConnectionError)) or type(error).__name__ in ('ConnectTimeout','ReadTimeout','ConnectionError'):
  r.update(category='network',action='backoff',retryable=True)
 elif isinstance(error,DiagnosticError):
  r.update(reason=error.reason,**{k:v for k,v in error.details.items() if k in ('http_status','content_type','byte_limit')})
  if error.reason=='upstream_returned_html':r.update(category='non_image_response',action='refresh_source_url')
  elif error.reason=='image_byte_limit':r.update(category='resource_limit',action='bounded_thumbnail')
 elif type(error).__name__=='UnidentifiedImageError':r.update(category='decode_unidentified',action='inspect_response_or_format')
 elif isinstance(error,OSError):r.update(category='decode_or_io',action='verify_original')
 return r
