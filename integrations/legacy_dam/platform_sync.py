"""Cursor-based platform ingestion. Read-only probe unless --apply supplied."""
import argparse,runpy,socket,json,time,os,hashlib,urllib.request,urllib.parse,io
from failure_diagnostics import describe_failure,DiagnosticError
from PIL import Image
Image.MAX_IMAGE_PIXELS=60_000_000
ROOT=os.environ.get('DAM_LEGACY_SCRIPTS', os.path.dirname(os.path.abspath(__file__))) + '/'
def walk(call,path,body=None):
 token='';seen=set()
 while True:
  if body is None:d=call(path+('?pageToken='+urllib.parse.quote(token) if token else ''))
  else:d=call(path,{**body,**({'pageToken':token} if token else {}),'pageSize':50})
  if not isinstance(d,dict) or not isinstance(d.get('items'),list):raise RuntimeError('unexpected_response_shape')
  yield d['items']
  info=d.get('pageInfo',{})
  if not info.get('hasMore'):break
  token=info.get('nextPageToken')
  if not token or token in seen:raise RuntimeError('missing_or_repeated_page_cursor')
  seen.add(token)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--source',choices=['app','artist'],required=True);ap.add_argument('--apply',action='store_true');ap.add_argument('--days',type=int,default=3);ap.add_argument('--limit',type=int,default=0);ap.add_argument('--workflow',default='');ap.add_argument('--drain',action='store_true');a=ap.parse_args()
 legacy=runpy.run_path(ROOT+'sync_zwy_daily.py',run_name='adapter')
 if a.source=='artist':
  artist=runpy.run_path(ROOT+'ingest_infmonkeys.py',run_name='adapter');socket.getaddrinfo=artist['_orig'];session=artist['S'];session.verify=True
  def call(path,body=None):
   r=session.request('POST' if body is not None else 'GET',artist['BASE']+path,json=body,timeout=30);r.raise_for_status();return r.json()
 else:
  def call(path,body=None):
   req=urllib.request.Request(legacy['API_BASE']+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer '+legacy['TOKEN'],'Content-Type':'application/json'})
   for attempt in range(3):
    try:
     with urllib.request.urlopen(req,timeout=40) as r:return json.load(r)
    except Exception:
     if attempt==2:raise
     time.sleep(2**attempt)
 workflows={}
 for page in ([] if a.drain else walk(call,'/api/workflow/metadata')):
  for x in page:
   record=x.get('record',x);meta=x.get('definition',{}).get('metadata',{});wid=record.get('workflowId') or record.get('id')
   name=meta.get('name') or record.get('displayName') or wid
   if isinstance(name,dict):name=name.get('zh-CN') or name.get('en-US') or next(iter(name.values()),wid)
   if wid:workflows[wid]=str(name)
 if not workflows and not a.drain:raise RuntimeError('empty_workflow_discovery')
 print(json.dumps({'source':a.source,'workflows':len(workflows),'apply':a.apply}),flush=True)
 cn=legacy['pg']();cur=cn.cursor();source='zaowuyun_team' if a.source=='app' else 'infmonkeys'
 cur.execute('SELECT pg_try_advisory_lock(hashtext(%s))',('dam-platform-'+a.source+('-drain' if a.drain else ''),))
 if not cur.fetchone()[0]:
  print(json.dumps({'source':a.source,'skipped':'sync_already_running'}),flush=True);cn.close();return
 # Keep previously observed workflows even if no longer in the discovery list.
 cur.execute('SELECT distinct workflow_id,workflow_name FROM gen_run WHERE source=%s OR (%s=\'infmonkeys\' AND source IS NULL)',(source,source))
 for wid,name in cur.fetchall():
  if wid and not a.drain:workflows.setdefault(wid,name or wid)
 if a.apply:
  cur.execute('''CREATE TABLE IF NOT EXISTS dam_platform_file(source text,instance_id text,url_hash text,url text,state text NOT NULL DEFAULT 'pending',attempts int NOT NULL DEFAULT 0,last_error text,updated_at timestamptz DEFAULT now(),PRIMARY KEY(source,instance_id,url_hash));
  CREATE TABLE IF NOT EXISTS dam_platform_status(source text PRIMARY KEY,last_started timestamptz,last_success timestamptz,error text,runs_seen bigint DEFAULT 0,files_stored bigint DEFAULT 0);''')
  cur.execute('INSERT INTO dam_platform_status(source,last_started,error) VALUES(%s,now(),NULL) ON CONFLICT(source) DO UPDATE SET last_started=now(),error=NULL',(source,));cn.commit()
 count=stored=errors=0;cutoff=time.time()*1000-a.days*86400000
 try:
  for wid,name in workflows.items():
   if a.workflow and wid!=a.workflow:continue
   done=False
   for page in walk(call,'/api/workflow/executions/search',{'workflowId':wid,'summaryOnly':False,'orderBy':{'field':'startTime','order':'DESC'}}):
    if not page:break
    for it in page:
     if a.days and it.get('startTime',0)<cutoff:done=True;continue
     instance=it.get('workflowId') or it.get('id')
     if not instance:raise RuntimeError('missing_execution_id')
     count+=1;meta=artist['extract'](it) if a.source=='artist' else legacy['extract_meta'](it,name)
     output=it.get('output') or {}
     while isinstance(output,dict) and isinstance(output.get('data'),dict):output=output['data']
     if isinstance(output,dict):
      for key in ('file_list','data'):
       if isinstance(output.get(key),list):meta['output_images']+= [u for u in output[key] if isinstance(u,str) and u.startswith(('https://','http://'))]
     meta['output_images']=list(dict.fromkeys(meta['output_images']))
     if a.apply:
      cur.execute('''INSERT INTO gen_run(instance_id,workflow_id,workflow_name,status,input_prompt,input_images,params,start_time,end_time,raw,source)
      VALUES(%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s/1000.0),to_timestamp(%s/1000.0),%s,%s)
      ON CONFLICT(instance_id) DO UPDATE SET status=excluded.status,end_time=excluded.end_time''',(instance,wid,name,it.get('status'),meta['input_prompt'],json.dumps(meta['input_images']),json.dumps(meta['params']),it.get('startTime'),it.get('endTime'),json.dumps({'adapter':'cursor-v1'}),source))
      for url in meta['output_images']:
       identity=hashlib.sha256(urllib.parse.urlsplit(url).path.encode()).hexdigest()
       cur.execute("INSERT INTO dam_platform_file(source,instance_id,url_hash,url) VALUES(%s,%s,%s,%s) ON CONFLICT(source,instance_id,url_hash) DO UPDATE SET url=excluded.url",(source,instance,identity,url))
      cn.commit()
     if a.limit and count>=a.limit:done=True;break
    if done:break
   if a.limit and count>=a.limit:break
   print(json.dumps({'source':a.source,'runs_seen':count}),flush=True)
  if a.apply:
   cur.execute("SELECT instance_id,url_hash,url FROM dam_platform_file WHERE source=%s AND state!='stored' AND attempts<5 ORDER BY attempts,updated_at LIMIT 2000",(source,));pending=cur.fetchall()
   for inst,identity,url in pending:
    try:
     cur.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s))',(source+inst+identity,))
     if not cur.fetchone()[0]:cn.rollback();continue
     # Existing output links are valid completion evidence; failed outputs remain retryable.
     cur.execute("SELECT 1 FROM gen_file WHERE instance_id=%s AND src_url=%s AND role='output'",(inst,url));exists=cur.fetchone()
     if not exists:
      req=urllib.request.Request(url,headers={'User-Agent':'DAM-Sync/1.0'})
      with urllib.request.urlopen(req,timeout=30) as response:
       if 'text/html' in response.headers.get('Content-Type',''):raise DiagnosticError('upstream_returned_html',http_status=response.status,content_type=response.headers.get('Content-Type','').split(';')[0])
       raw=response.read(128*1024*1024+1)
      sha=hashlib.sha256(raw).hexdigest();ext=urllib.parse.urlsplit(url).path.rsplit('.',1)[-1].lower()
      if ext not in ('png','jpg','jpeg','webp','gif','tif','tiff','avif','bmp'):ext='png'
      if len(raw)>128*1024*1024:raise DiagnosticError('image_byte_limit',byte_limit=128*1024*1024)
      im=Image.open(io.BytesIO(raw));w,h=im.size
      key='artworks/_generated/'+source+'/'+inst+'/'+identity+'.'+ext
      legacy['cos']().put_object(Bucket=legacy['BUCKET'],Key=key,Body=raw)
      cur.execute("""INSERT INTO gen_file(instance_id,cos_key,src_url,sha256,width,height,bytes,fmt,role,source,q_status)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'output',%s,'pending') ON CONFLICT(cos_key) DO NOTHING""",(inst,key,url,sha,w,h,len(raw),ext,source))
     cur.execute("UPDATE dam_platform_file SET state='stored',updated_at=now(),last_error=NULL WHERE source=%s AND instance_id=%s AND url_hash=%s",(source,inst,identity));cn.commit();stored+=1
     if stored%100==0:
      cur.execute(open(ROOT+'sync_dam_asset.sql').read());cur.execute("UPDATE dam_asset SET lib='infmonkeys' WHERE source='infmonkeys' AND lib='gen'");cn.commit()
      print(json.dumps({'source':a.source,'stored_checkpoint':stored}),flush=True)
    except Exception as e:
     cn.rollback();errors+=1
     cur.execute("UPDATE dam_platform_file SET attempts=attempts+1,state='failed',last_error=%s,updated_at=now() WHERE source=%s AND instance_id=%s AND url_hash=%s",(json.dumps(describe_failure(e),ensure_ascii=False),source,inst,identity));cn.commit()
   cur.execute(open(ROOT+'sync_dam_asset.sql').read());cur.execute("UPDATE dam_asset SET lib='infmonkeys' WHERE source='infmonkeys' AND lib='gen'");cn.commit()
   cur.execute('UPDATE dam_platform_status SET last_success=now(),runs_seen=%s,files_stored=%s,error=%s WHERE source=%s',(count,stored,'file_failures:'+str(errors) if errors else None,source));cn.commit()
  print(json.dumps({'source':a.source,'runs_seen':count,'stored':stored,'failed':errors}),flush=True)
 except Exception as e:
  cn.rollback()
  if a.apply:
   cur.execute('UPDATE dam_platform_status SET error=%s WHERE source=%s',(type(e).__name__,source));cn.commit()
  raise
 finally:cn.close()
if __name__=='__main__':
 try:main()
 except Exception as error:
  print(json.dumps({'event':'source_sync_failed','error':describe_failure(error)},ensure_ascii=False),flush=True)
  raise SystemExit(1)
