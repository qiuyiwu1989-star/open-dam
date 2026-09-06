#!/usr/bin/env python3
"""open-dam · API 服务 (FastAPI)

端点:上传(内容寻址秒判重) / 浏览 / 语义搜索 / 色彩检索 / 以图搜图 / 集合 / 统计
存储:本地文件系统 data/uploads/<sha256[:2]>/<sha256>.<ext>(内容寻址,天然去重)
"""
import os, hashlib
import psycopg2, psycopg2.extras
from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:postgres@db:5432/opendam")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
AI_ENABLED = os.environ.get("AI_ENABLED", "true").lower() == "true"
CLIP_MODEL = os.environ.get("CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED = os.environ.get("CLIP_PRETRAINED", "laion2b_s34b_b79k")

app = FastAPI(title="open-dam", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def pg():
    return psycopg2.connect(DATABASE_URL)

def q(sql, args=(), one=False):
    cn = pg()
    cur = cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    r = cur.fetchone() if one else cur.fetchall()
    cn.commit()   # INSERT ... RETURNING 也走这里,必须提交
    cur.close(); cn.close()
    return r

def ex(sql, args=()):
    cn = pg()
    cur = cn.cursor()
    cur.execute(sql, args)
    cn.commit(); cur.close(); cn.close()

def public_url(storage_key):
    return f"/img/{storage_key}"

def asset_json(a):
    a = dict(a)
    a["image"] = public_url(a["storage_key"])
    a["created_at"] = a["created_at"].isoformat() if a.get("created_at") else None
    return a

# ── 健康 ──
@app.get("/healthz")
def healthz():
    q("SELECT 1")
    return {"ok": True, "ai": AI_ENABLED}

# ── 上传(内容寻址,sha256 秒判重) ──
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    os.makedirs(DATA_DIR, exist_ok=True)
    results = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            results.append({"filename": f.filename, "status": "too_large"})
            continue
        if len(raw) < 100:
            results.append({"filename": f.filename, "status": "invalid"})
            continue
        sha = hashlib.sha256(raw).hexdigest()
        ext = (f.filename or "x.png").rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"):
            ext = "png"
        dup = q("SELECT asset_id, status FROM asset WHERE sha256=%s", (sha,), one=True)
        if dup:
            results.append({"filename": f.filename, "status": "duplicate", "asset_id": dup["asset_id"]})
            continue
        storage_key = f"uploads/{sha[:2]}/{sha}.{ext}"
        path = os.path.join(DATA_DIR, storage_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as out:
            out.write(raw)
        row = q("""INSERT INTO asset (sha256, filename, bytes, fmt, storage_key)
                   VALUES (%s,%s,%s,%s,%s) RETURNING asset_id""",
                (sha, f.filename, len(raw), ext, storage_key), one=True)
        ex("INSERT INTO task (kind, asset_id) VALUES ('ingest', %s) ON CONFLICT DO NOTHING",
           (row["asset_id"],))
        results.append({"filename": f.filename, "status": "accepted", "asset_id": row["asset_id"]})
    return {"results": results}

# ── 浏览 ──
@app.get("/api/assets")
def browse(hue: str = "", category: str = "", q_: str = Query("", alias="q"),
           status: str = "active", limit: int = 60, offset: int = 0):
    where, args = ["status=%s"], [status]
    if hue:      where.append("hue=%s");      args.append(hue)
    if category: where.append("category=%s"); args.append(category)
    if q_:
        where.append("(filename ILIKE %s OR category ILIKE %s OR %s = ANY(tags))")
        args += [f"%{q_}%", f"%{q_}%", q_]
    args += [min(limit, 200), offset]
    rows = q(f"""SELECT asset_id, filename, bytes, fmt, width, height, ave_rgb, hue,
                        category, tags, storage_key, q_score, created_at
                 FROM asset WHERE {' AND '.join(where)}
                 ORDER BY created_at DESC LIMIT %s OFFSET %s""", args)
    return {"items": [asset_json(a) for a in rows]}

@app.get("/api/assets/{asset_id}")
def detail(asset_id: int):
    a = q("SELECT * FROM asset WHERE asset_id=%s", (asset_id,), one=True)
    if not a: raise HTTPException(404)
    return asset_json(a)

# ── CLIP 文本编码(惰性加载;AI_ENABLED=false 时 503) ──
_clip = {}
def text_vec(s: str):
    if not AI_ENABLED:
        raise HTTPException(503, "AI 未启用(AI_ENABLED=false),语义搜索不可用")
    if "model" not in _clip:
        import open_clip, torch
        model, _, _ = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
        tok = open_clip.get_tokenizer(CLIP_MODEL)
        model.eval()
        _clip.update(model=model, tok=tok, torch=torch)
    with _clip["torch"].no_grad():
        t = _clip["tok"]([s])
        v = _clip["model"].encode_text(t)
        v = v / v.norm(dim=-1, keepdim=True)
    return "[" + ",".join(f"{x:.6f}" for x in v[0].tolist()) + "]"

# ── 语义搜索 ──
@app.get("/api/search/text")
def search_text(q_: str = Query(..., alias="q"), limit: int = 30):
    vec = text_vec(q_)
    rows = q("""SELECT a.asset_id, a.filename, a.bytes, a.fmt, a.width, a.height,
                       a.ave_rgb, a.hue, a.category, a.tags, a.storage_key, a.created_at,
                       1 - (e.embedding <=> %s::vector) AS score
                FROM embedding e JOIN asset a USING (asset_id)
                WHERE a.status='active'
                ORDER BY e.embedding <=> %s::vector LIMIT %s""",
             (vec, vec, min(limit, 100)))
    return {"items": [asset_json(a) for a in rows]}

# ── 以图搜图 ──
@app.get("/api/similar/{asset_id}")
def similar(asset_id: int, limit: int = 30):
    base = q("SELECT embedding::text v FROM embedding WHERE asset_id=%s", (asset_id,), one=True)
    if not base: raise HTTPException(404, "该图暂无向量(可能还在处理中)")
    rows = q("""SELECT a.asset_id, a.filename, a.bytes, a.fmt, a.width, a.height,
                       a.ave_rgb, a.hue, a.category, a.tags, a.storage_key, a.created_at,
                       1 - (e.embedding <=> %s::vector) AS score
                FROM embedding e JOIN asset a USING (asset_id)
                WHERE a.status='active' AND a.asset_id <> %s
                ORDER BY e.embedding <=> %s::vector LIMIT %s""",
             (base["v"], asset_id, base["v"], min(limit, 100)))
    return {"items": [asset_json(a) for a in rows]}

# ── 色彩检索 ──
def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

@app.get("/api/search/color")
def search_color(hex: str = Query("#b0673a"), limit: int = 60):
    try:
        tr, tg, tb = hex_to_rgb(hex)
    except Exception:
        raise HTTPException(400, "hex 格式应为 #rrggbb")
    rows = q("""SELECT asset_id, filename, bytes, fmt, width, height, ave_rgb, hue,
                       category, tags, storage_key, created_at
                FROM asset WHERE status='active' AND ave_rgb IS NOT NULL""",
             ())
    # RGB 距离在 Python 侧算(库量级 <百万时足够快,且免去 SQL 十六进制解析)
    def dist(a):
        try:
            r, g, b = hex_to_rgb(a["ave_rgb"])
            return (r-tr)**2 + (g-tg)**2 + (b-tb)**2
        except Exception:
            return 1e9
    rows = sorted(rows, key=dist)[:min(limit, 200)]
    return {"items": [asset_json(a) for a in rows]}

# ── 色相/分类清单(筛选器) ──
@app.get("/api/facets")
def facets():
    return {
        "hues": q("SELECT hue k, count(*) n FROM asset WHERE status='active' AND hue IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"),
        "categories": q("SELECT category k, count(*) n FROM asset WHERE status='active' AND category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"),
    }

# ── 集合 ──
@app.get("/api/collections")
def collections():
    return {"items": q("""SELECT c.id, c.name, count(i.asset_id) n
                          FROM collection c LEFT JOIN collection_item i ON i.coll_id=c.id
                          GROUP BY c.id ORDER BY c.created_at DESC""")}

@app.post("/api/collections")
def new_collection(name: str):
    return q("INSERT INTO collection (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id, name",
             (name,), one=True)

@app.post("/api/collections/{coll_id}/add")
def coll_add(coll_id: int, asset_id: int):
    ex("INSERT INTO collection_item (coll_id, asset_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
       (coll_id, asset_id))
    return {"ok": True}

@app.get("/api/collections/{coll_id}/items")
def coll_items(coll_id: int):
    rows = q("""SELECT a.asset_id, a.filename, a.bytes, a.fmt, a.width, a.height,
                       a.ave_rgb, a.hue, a.category, a.tags, a.storage_key, a.created_at
                FROM collection_item i JOIN asset a ON a.asset_id=i.asset_id
                WHERE i.coll_id=%s ORDER BY i.added_at DESC""", (coll_id,))
    return {"items": [asset_json(a) for a in rows]}

# ── 统计 ──
@app.get("/api/stats")
def stats():
    s = q("""SELECT count(*) total,
                    count(*) FILTER (WHERE status='active') active,
                    count(*) FILTER (WHERE status='processing') processing,
                    COALESCE(sum(bytes),0) bytes
             FROM asset""", one=True)
    v = q("SELECT count(*) n FROM embedding", one=True)
    s["bytes_gb"] = round(s["bytes"] / 1e9, 2)
    s["vectorized"] = v["n"]
    return s

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
