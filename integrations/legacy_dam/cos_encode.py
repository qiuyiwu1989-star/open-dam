"""Bounded local CLIP worker for internal assets; retryable failures, no external AI calls."""
import os,json,time,io,urllib.request,urllib.error
import psycopg2
from qcloud_cos import CosConfig,CosS3Client
from image_processing import thumbnail_params
from failure_diagnostics import describe_failure,DiagnosticError
from PIL import Image
Image.MAX_IMAGE_PIXELS=60_000_000
import torch,clip
cfg={s['bucket']:s for s in json.load(open(os.environ.get('DAM_SOURCE_CONFIG', './cos_sources.json')))}
p=json.load(open(os.path.expanduser('~/.opendam/.aha_pg.json')))
cn=psycopg2.connect(**{k:p[k] for k in ('host','port','dbname','user','password')})
with cn.cursor() as c:
 c.execute('SELECT pg_try_advisory_lock(718240118)')
 if not c.fetchone()[0]:raise RuntimeError('encoder already running')
 c.execute('''CREATE TABLE IF NOT EXISTS dam_internal.embeddings (LIKE public.dam_embedding INCLUDING ALL);
 CREATE OR REPLACE VIEW dam_internal.dam_embedding AS SELECT * FROM public.dam_embedding UNION ALL SELECT i.* FROM dam_internal.embeddings i WHERE NOT EXISTS (SELECT 1 FROM public.dam_embedding p WHERE p.asset_id=i.asset_id);
 CREATE TABLE IF NOT EXISTS dam_internal.encode_failures(asset_id bigint PRIMARY KEY,attempts int NOT NULL DEFAULT 1,error_code text,next_retry timestamptz NOT NULL DEFAULT now()+interval '1 hour');''');cn.commit()
torch.set_num_threads(2);model,pre=clip.load('ViT-B/32',device='cpu');model.eval();clients={}
def get_image(bucket,key):
 s=cfg[bucket]
 if bucket not in clients:
  d=json.load(open(os.path.expanduser(s['credential_file'])));clients[bucket]=CosS3Client(CosConfig(Region=s['region'],SecretId=d['secret_id'],SecretKey=d['secret_key']))
 client=clients[bucket]
 for params in (thumbnail_params(384),{}):
  try:
   url=client.get_presigned_url(Method='GET',Bucket=bucket,Key=key,Params=params,Expired=120)
   with urllib.request.urlopen(url,timeout=20) as r:
    raw=r.read(12*1024*1024+1)
    if len(raw)>12*1024*1024:raise DiagnosticError('image_byte_limit',byte_limit=12*1024*1024)
   im=Image.open(io.BytesIO(raw));im.load();return pre(im.convert('RGB'))
  except urllib.error.HTTPError as e:
   if params and e.code in (400,404):continue
   raise
 raise ValueError('image_unavailable')
while True:
 with cn.cursor() as c:
  c.execute('''SELECT a.asset_id,a.bucket,a.cos_key FROM dam_internal.dam_asset a
  LEFT JOIN dam_internal.dam_embedding e USING(asset_id) LEFT JOIN dam_internal.encode_failures f USING(asset_id)
  WHERE a.status='active' AND e.asset_id IS NULL AND (f.asset_id IS NULL OR (f.next_retry<now() AND f.attempts<3))
  ORDER BY a.asset_id LIMIT 16''');rows=c.fetchall()
 if not rows:break
 tensors=[];ids=[]
 for aid,b,k in rows:
  try:tensors.append(get_image(b,k));ids.append(aid)
  except Exception as e:
   code=json.dumps(describe_failure(e),ensure_ascii=False)
   with cn.cursor() as c:c.execute('''INSERT INTO dam_internal.encode_failures(asset_id,error_code) VALUES(%s,%s)
   ON CONFLICT(asset_id) DO UPDATE SET attempts=encode_failures.attempts+1,error_code=excluded.error_code,next_retry=now()+interval '6 hours' ''',(aid,code))
 if ids:
  with torch.no_grad():v=model.encode_image(torch.stack(tensors));v=v/v.norm(dim=-1,keepdim=True)
  with cn.cursor() as c:
   for aid,vec in zip(ids,v.tolist()):
    c.execute("INSERT INTO dam_internal.embeddings(asset_id,model,embedding) VALUES(%s,'clip-vit-b32',%s::vector) ON CONFLICT DO NOTHING",(aid,str(vec)))
    c.execute('DELETE FROM dam_internal.encode_failures WHERE asset_id=%s',(aid,))
 cn.commit();print(json.dumps({'encoded':len(ids),'failed':len(rows)-len(ids),'last_id':rows[-1][0]}),flush=True)
cn.close()
