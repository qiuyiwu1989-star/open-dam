"""Resumable COS inventory to DAM; dry-run by default, immutable originals."""
import argparse,json,os,hashlib
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_values
from qcloud_cos import CosConfig,CosS3Client
EXT={'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tif','.tiff','.avif','.svg'}
def eligible(key,size):
    return size>0 and Path(key).suffix.lower() in EXT and not any(p.startswith('._') or p in {'.git','node_modules','__MACOSX'} for p in key.split('/'))
def ownership(key):
    p=key.split('/');tenant=None;owner=None
    if 'tenants' in p:
        i=p.index('tenants');tenant=p[i+1] if i+1<len(p) else None
    for s in p:
        if s.startswith('local_'):owner=s[6:]
    return {'tenant_hint':tenant,'owner_hint':owner,'prefix': '/'.join(p[:-1]),'verified':False}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);ap.add_argument('--apply',action='store_true');ap.add_argument('--restart',action='store_true');a=ap.parse_args()
    cfg=json.load(open(a.config));db=json.load(open(os.path.expanduser('~/.opendam/.aha_pg.json')));cn=psycopg2.connect(**{k:db[k] for k in ('host','port','dbname','user','password')});cn.autocommit=False
    with cn.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_lock(718240117)')
        if not cur.fetchone()[0]:raise RuntimeError('another ingestion is running')
        if a.apply:
            cur.execute('''CREATE SCHEMA IF NOT EXISTS dam_internal;
            CREATE TABLE IF NOT EXISTS dam_internal.assets (LIKE public.dam_asset INCLUDING ALL);
            CREATE OR REPLACE VIEW dam_internal.dam_asset AS SELECT * FROM public.dam_asset UNION ALL SELECT i.* FROM dam_internal.assets i WHERE NOT EXISTS (SELECT 1 FROM public.dam_asset p WHERE p.bucket=i.bucket AND p.cos_key=i.cos_key);
            CREATE TABLE IF NOT EXISTS dam_cos_object (
            bucket text NOT NULL,cos_key text NOT NULL,region text NOT NULL,etag text,bytes bigint,modified_at timestamptz,
            source_id text NOT NULL,ownership jsonb NOT NULL DEFAULT '{}',last_seen_at timestamptz DEFAULT now(),
            PRIMARY KEY(bucket,cos_key));
            CREATE TABLE IF NOT EXISTS dam_cos_cursor (bucket text PRIMARY KEY,marker text NOT NULL DEFAULT '',completed_at timestamptz);''');cn.commit()
    for s in cfg:
        cred=json.load(open(os.path.expanduser(s['credential_file'])));client=CosS3Client(CosConfig(Region=s['region'],SecretId=cred['secret_id'],SecretKey=cred['secret_key'],Timeout=30));marker='';seen=added=0
        if a.apply and not a.restart:
            with cn.cursor() as cur:
                cur.execute('SELECT marker,completed_at FROM dam_cos_cursor WHERE bucket=%s',(s['bucket'],));row=cur.fetchone()
                if row and row[1] is None:marker=row[0]
        while True:
            r=client.list_objects(Bucket=s['bucket'],Marker=marker,MaxKeys=1000);rows=[];assets=[]
            for o in r.get('Contents',[]):
                k=o['Key'];size=int(o['Size'])
                if not eligible(k,size):continue
                seen+=1;rows.append((s['bucket'],k,s['region'],o.get('ETag','').strip('"'),size,o['LastModified'],s['lib'],json.dumps(ownership(k))))
                assets.append((s['bucket'],k,s['lib'],'cos:'+s['bucket'],size,Path(k).suffix.lower()[1:],o['LastModified']))
            more=str(r.get('IsTruncated')).lower()=='true';next_marker=(r.get('NextMarker') or r['Contents'][-1]['Key']) if more else ''
            if more and next_marker==marker:raise RuntimeError('pagination did not advance')
            if a.apply:
                with cn.cursor() as cur:
                    if rows:
                        changed = execute_values(cur, """SELECT a.asset_id FROM dam_internal.assets a
                        JOIN public.dam_cos_object old ON old.bucket=a.bucket AND old.cos_key=a.cos_key
                        JOIN (VALUES %s) AS v(bucket,cos_key,region,etag,bytes,modified_at,source_id,ownership)
                        ON v.bucket=old.bucket AND v.cos_key=old.cos_key
                        WHERE old.etag IS DISTINCT FROM v.etag""", rows, fetch=True)
                        cur.execute("SELECT to_regclass('dam_internal.embeddings')")
                        if cur.fetchone()[0] and changed:
                            cur.execute("DELETE FROM dam_internal.embeddings WHERE asset_id=ANY(%s)",([x[0] for x in changed],))
                            cur.execute("DELETE FROM dam_internal.encode_failures WHERE asset_id=ANY(%s)",([x[0] for x in changed],))
                    if rows:execute_values(cur,'''INSERT INTO dam_cos_object(bucket,cos_key,region,etag,bytes,modified_at,source_id,ownership) VALUES %s
                     ON CONFLICT(bucket,cos_key) DO UPDATE SET etag=excluded.etag,bytes=excluded.bytes,modified_at=excluded.modified_at,last_seen_at=now(),ownership=excluded.ownership''',rows)
                    if assets:
                        result=execute_values(cur,'''INSERT INTO dam_internal.assets(bucket,cos_key,lib,source,bytes,fmt,created_ts,status)
                        SELECT v.* FROM (VALUES %s) AS v(bucket,cos_key,lib,source,bytes,fmt,created_ts,status) WHERE NOT EXISTS (SELECT 1 FROM public.dam_asset a WHERE a.bucket=v.bucket AND a.cos_key=v.cos_key) ON CONFLICT(bucket,cos_key) DO NOTHING RETURNING asset_id''',assets,template="(%s,%s,%s,%s,%s,%s,%s::timestamptz,'active')",fetch=True);added+=len(result)
                    if rows:
                        cur.execute("""UPDATE dam_internal.assets a SET bytes=o.bytes,created_ts=o.modified_at
                        FROM public.dam_cos_object o WHERE a.bucket=o.bucket AND a.cos_key=o.cos_key
                        AND a.bucket=%s AND a.cos_key=ANY(%s)
                        AND (a.bytes IS DISTINCT FROM o.bytes OR a.created_ts IS DISTINCT FROM o.modified_at)""",(s['bucket'],[r[1] for r in rows]))
                    cur.execute('''INSERT INTO dam_cos_cursor(bucket,marker,completed_at) VALUES(%s,%s,CASE WHEN %s THEN now() ELSE NULL END)
                    ON CONFLICT(bucket) DO UPDATE SET marker=excluded.marker,completed_at=excluded.completed_at''',(s['bucket'],next_marker,not more))
                cn.commit()
            print(json.dumps({'bucket':s['bucket'],'page_images':len(rows),'seen':seen,'added':added}),flush=True)
            marker=next_marker
            if not more:break
        print(json.dumps({'bucket':s['bucket'],'scanned_images':seen,'added':added,'apply':a.apply}),flush=True)
    cn.close()
if __name__=='__main__':main()
