"""Internal DAM access gateway. Reuses app-market sessions; no password storage."""
import os,json,time,hashlib,urllib.request,urllib.error,urllib.parse
from http.cookies import SimpleCookie
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse,Response,JSONResponse
from qcloud_cos import CosConfig,CosS3Client
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
CONFIG=os.environ.get('DAM_SOURCE_CONFIG','./cos_sources.json')
AUTH=os.environ.get('DAM_AUTH_URL', 'http://127.0.0.1:13100/api/auth')
def allowed(user):
    names=set(os.environ.get('DAM_ALLOWED_USERS','example-user').split(','))
    return user.get('role')=='admin' or user.get('username') in names
async def check(request):
    cookie=request.headers.get('cookie','')
    if not cookie:raise HTTPException(401,'请登录内部账号')
    req=urllib.request.Request(AUTH+'/me',headers={'Cookie':cookie})
    try:
        with urllib.request.urlopen(req,timeout=8) as r:data=json.load(r)
    except Exception:raise HTTPException(503,'登录服务暂不可用')
    if not data.get('authenticated'):raise HTTPException(401,'请登录内部账号')
    if not allowed(data.get('user',{})):raise HTTPException(403,'账号尚未获准访问内部 DAM')
    return data['user']
@app.get('/auth/check')
async def auth_check(request:Request):
    u=await check(request);return Response(status_code=204,headers={'X-DAM-User':u['id']})
@app.get('/login')
def login_page():
    return HTMLResponse('''<!doctype html><meta charset="utf-8"><title>DAM 内部登录</title><style>body{font:16px system-ui;background:#f8f7f4;max-width:380px;margin:15vh auto}input,button{box-sizing:border-box;width:100%;padding:14px;margin:8px 0}p{color:#666}</style><h1>DAM 数字资产中心</h1><p>使用已获授权的应用中心账号登录。</p><form id="f"><input name="login" autocomplete="username" placeholder="用户名或邮箱" required><input name="password" type="password" autocomplete="current-password" placeholder="密码" required><button>登录</button></form><p id="msg"></p><script>f.onsubmit=async e=>{e.preventDefault();let r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(f)))});if(r.ok)location.href='/';else msg.textContent=r.status===403?'账号尚未获准访问内部 DAM':'登录失败，请检查账号或稍后重试';}</script>''',headers={'Cache-Control':'no-store'})
@app.post('/auth/login')
async def login(request:Request):
    if request.headers.get('origin')!='https://dam.example.com':raise HTTPException(403)
    raw=await request.body()
    if len(raw)>8192:raise HTTPException(413)
    try:
        with urllib.request.urlopen(urllib.request.Request(AUTH+'/login',data=raw,headers={'Content-Type':'application/json'}),timeout=15) as r:
            data=json.load(r);cookie=r.headers.get('Set-Cookie','')
    except urllib.error.HTTPError as e:raise HTTPException(e.code)
    except Exception:raise HTTPException(503)
    if not allowed(data.get('user',{})):raise HTTPException(403)
    return JSONResponse({'ok':True},headers={'Set-Cookie':cookie+'; Secure','Cache-Control':'no-store'})
@app.get('/object/{bucket}/{key:path}')
async def image(bucket:str,key:str,request:Request):
    sources={s['bucket']:s for s in json.load(open(CONFIG))}
    if bucket not in sources:raise HTTPException(404)
    from pathlib import Path
    if Path(key).suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.tif','.tiff','.avif','.svg'}:raise HTTPException(404)
    s=sources[bucket];d=json.load(open(os.path.expanduser(s['credential_file'])))
    c=CosS3Client(CosConfig(Region=s['region'],SecretId=d['secret_id'],SecretKey=d['secret_key']))
    params={}
    if request.url.query.startswith('imageMogr2/thumbnail/'):
        val=request.url.query[len('imageMogr2/'):]
        import re
        if re.fullmatch(r'thumbnail/[1-9][0-9]{0,3}x',val):params['imageMogr2']=val
    url=c.get_presigned_url(Method='GET',Bucket=bucket,Key=key,Expired=90,Params=params)
    return RedirectResponse(url,status_code=302,headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'})
