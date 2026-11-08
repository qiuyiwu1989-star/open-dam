#!/usr/bin/env python3
"""造物云 DAM (Digital Asset Management) API。端口 8091,服务 dam.example.com。

统一向量底座架构(2026-06):
  - 一张 dam_asset(COS 每张可检索的图一行,按 lib 分面)+ 一张 dam_embedding(单一 HNSW 向量基座)。
  - 业务元数据不搬家:dam_asset.(ext_table,ext_id) 软外键回指 gen_run / zaowuyun_media / works。
  - 所有检索端点 lib 化;老的 gen 路由与 /media/* 路由保留为薄 shim(向后兼容,前端零回归)。
  - 加一个新库 = LIBS 注册一行 + 一个 COS 扫描 adapter(scan_cos_lib.py)。
"""
import os, json, time, io, zipfile, urllib.request, ssl, socket, re, hashlib
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import psycopg2
import psycopg2.extras

app = FastAPI(title="造物云 DAM", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── 数据库 ──
def _cfg(p): return json.load(open(os.path.expanduser(p)))

def pg():
    d = _cfg("~/.opendam/.aha_pg.json")
    return psycopg2.connect(host=d["host"], port=d["port"], dbname=d["dbname"], user=d["user"], password=d["password"], options="-c search_path=dam_internal,public")

def q(sql, args=(), one=False):
    cn = pg()
    cur = cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, args)
    r = cur.fetchone() if one else cur.fetchall()
    cur.close(); cn.close()
    return r

def ex(sql, args=()):
    cn = pg()
    cur = cn.cursor()
    cur.execute(sql, args)
    cn.commit()
    cur.close(); cn.close()

AHA_BUCKET = "example-assets-1234567890"
def img_url(cos_key, thumb=None, bucket=None):
    if not cos_key: return None
    # aha 桶走 /img 内网代理;其它桶走 /cimg/<桶>/ 通用代理
    from urllib.parse import quote
    cos_key = quote(cos_key, safe="/")
    url = f"/cimg/{bucket}/{cos_key}" if (bucket and bucket != AHA_BUCKET) else f"/img/{cos_key}"
    if thumb: url += f"?imageMogr2/thumbnail/{thumb}x"
    return url

# ════════════════════════════════════════════════════════════════════
#  库(lib)注册表:meta 决定元数据从哪张业务表取。加新库只改这里 + 跑扫描脚本。
#    gen        生成区(造物云团队)       meta=gen   元数据 gen_run
#    infmonkeys 艺术家平台出图           meta=gen   元数据 gen_run
#    media      素材库(抓取)             meta=media 元数据 zaowuyun_media
#    nianhua    年画(COS NewYearPaintings) meta=cos  纯 COS,无业务表
# ════════════════════════════════════════════════════════════════════
# group = 库家族(前端竖向分组导航),order 控制家族排序
LIBS = {
    "gen":        {"meta": "gen",   "label": "生成区",     "group": "AI 生成"},
    "infmonkeys": {"meta": "gen",   "label": "Artist1.0 生成", "group": "AI 生成"},
    "media":      {"meta": "media", "label": "素材库",     "group": "素材参考"},
    "nianhua":    {"meta": "cos",   "label": "年画",       "group": "艺术藏品"},
    "artwork":    {"meta": "cos",   "label": "油画名作",   "group": "艺术藏品"},
    "guohua":     {"meta": "cos",   "label": "国画",       "group": "艺术藏品"},
    "beitie":     {"meta": "cos",   "label": "碑帖拓片",   "group": "艺术藏品"},
    "yishujia":   {"meta": "cos",   "label": "艺术数据集", "group": "艺术藏品"},
    "webdesign":  {"meta": "cos",   "label": "网页设计",   "group": "素材参考"},
}
LIB_GROUP_ORDER = ["AI 生成", "素材参考", "艺术藏品", "其它来源"]
for _src in json.load(open(os.environ.get("DAM_SOURCE_CONFIG", "./cos_sources.json"))):
    LIBS[_src["lib"]] = {"meta":"cos", "label":_src["label"], "group":"其它来源"}

def _meta(lib): return LIBS.get(lib, {}).get("meta", "cos")
def _norm_lib(lib): return lib if (lib in LIBS or lib == "all") else "gen"   # 'all'=跨库

# 统一资产查询:一条 SELECT 覆盖所有库(LEFT JOIN 由 ext_table 门控,只有对的那张业务表生效)
_ASSET_SELECT = """SELECT a.asset_id, a.ext_id, a.ext_table, a.lib, a.bucket, a.cos_key, a.category, a.ave_rgb, a.hue,
                          a.width, a.height, a.bytes, a.fmt, a.created_ts,
                          gr.workflow_name, gr.input_prompt, gr.params,
                          m.display_name, m.description"""
_ASSET_FROM = """FROM dam_asset a
                 LEFT JOIN gen_run gr ON a.ext_table='gen_run' AND gr.instance_id=a.ext_id
                 LEFT JOIN zaowuyun_media m ON a.ext_table='zaowuyun_media' AND m.src_id=a.ext_id"""
# 对外 id:业务 id 优先(gen=instance_id, media=src_id),纯 COS 库用 'a'+asset_id
_EXT_ID = "COALESCE(a.ext_id, 'a'||a.asset_id::text)"

def _ext_of(r):
    return r["ext_id"] or ("a" + str(r["asset_id"]))

def _asset_item(r):
    """统一卡片结构(与历史 gen/media 卡片同形,前端无需改渲染)。"""
    meta = _meta(r["lib"])
    it = {"id": _ext_of(r), "lib": r["lib"], "category": r.get("category"),
          "w": r.get("width"), "h": r.get("height"), "size": r.get("bytes"), "fmt": r.get("fmt"),
          "ave_rgb": r.get("ave_rgb"), "hue": r.get("hue"),
          "time": int(r["created_ts"].timestamp()*1000) if r.get("created_ts") else None,
          "image": img_url(r.get("cos_key"), bucket=r.get("bucket")), "thumb": img_url(r.get("cos_key"), 500, r.get("bucket"))}
    if meta == "gen":
        it["workflow"] = r.get("workflow_name") or ""
        it["prompt"] = r.get("input_prompt")
        it["model"] = (r.get("params") or {}).get("model")
    elif meta == "media":
        nm = r.get("display_name") or ""; desc = r.get("description") or ""
        it["name"] = nm; it["workflow"] = ""; it["model"] = None
        it["prompt"] = (nm + (" · " + desc if desc and desc != nm else "")).strip(" ·") or nm
    else:  # cos-only 库(如年画):没有业务元数据,用文件名兜底
        base = (r.get("cos_key") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        it["name"] = base; it["workflow"] = ""; it["model"] = None
        it["prompt"] = r.get("category") or base
    return it

def _assets_by_tokens(tokens):
    """按对外 id(业务 id 或 'a'+asset_id)批量取资产行。"""
    tokens = [t for t in tokens if t]
    if not tokens: return []
    return q(f"{_ASSET_SELECT} {_ASSET_FROM} WHERE {_EXT_ID} = ANY(%s)", (tokens,))

def _one_asset(token):
    rows = _assets_by_tokens([token])
    return rows[0] if rows else None

# ════════════════════════════════════════════════════════════════════
#  统一检索内核(按 lib)。老路由 = 调用它的薄 shim。
# ════════════════════════════════════════════════════════════════════

def _u_filters(lib, category, kw, color, date_from, date_to, near, tag, fold, quality=""):
    """构造 WHERE 子句(基于 dam_asset,跨库通用)。lib='all' 时不限库。返回 (where_list, args, use_near, near_hex)。"""
    where = ["a.status='active'"]; args = []
    if lib != "all":
        where.insert(0, "a.lib=%s"); args.append(lib)
    if quality and _meta(lib) == "gen":   # 质检达标筛选(仅生成区有 gen_file.q_status)
        gfx = "EXISTS(SELECT 1 FROM gen_file gf WHERE gf.instance_id=a.ext_id AND gf.role='output' AND gf.q_status"
        if quality == "kept":       where.append(gfx + "='kept')")
        elif quality == "flag":     where.append(gfx + " IN ('archived','rejected'))")
        elif quality in ("archived", "rejected", "pending"):
            where.append(gfx + "=%s)"); args.append(quality)
    if category:
        if "/" in category:
            where.append("a.category=%s"); args.append(category)          # 子类:精确
        else:
            # 大类:匹配自身(单层category)或其子类(两层 大类/子)
            where.append("(a.category=%s OR a.category LIKE %s)"); args += [category, category + "/%"]
    if kw:
        # 跨库统一:命中 prompt / 名称 / 描述 / cos_key / 聚合文本(含VLM画意+标签) 任一
        where.append("(gr.input_prompt ILIKE %s OR m.display_name ILIKE %s OR m.description ILIKE %s OR a.cos_key ILIKE %s OR EXISTS(SELECT 1 FROM dam_text dt WHERE dt.asset_id=a.asset_id AND dt.txt ILIKE %s))")
        like = "%" + kw + "%"; args += [like, like, like, like, like]
    if color:
        where.append("a.hue=%s"); args.append(color)
    if tag:
        where.append(f"EXISTS(SELECT 1 FROM dam_tag_item t WHERE t.lib=a.lib AND t.item_id={_EXT_ID} AND t.tag=%s)")
        args.append(tag)
    if date_from:
        where.append("a.created_ts >= %s"); args.append(date_from)
    if date_to:
        where.append("a.created_ts < (%s::date + 1)"); args.append(date_to)
    if fold and _meta(lib) == "gen":   # 折叠近重复(仅生成区有 dupe_rep)
        where.append("EXISTS(SELECT 1 FROM gen_file gf WHERE gf.instance_id=a.ext_id AND gf.role='output' AND gf.dupe_rep)")
    near_hex = near.lstrip("#") if near else ""
    use_near = bool(re.fullmatch(r"[0-9a-fA-F]{6}", near_hex))
    if use_near:
        where.append("a.ave_rgb ~ '^#[0-9a-fA-F]{6}$'")
    return where, args, use_near, near_hex

def _u_stats(lib):
    lib = _norm_lib(lib)
    lw = "" if lib == "all" else "AND lib=%s"; la = [] if lib == "all" else [lib]
    lwa = "" if lib == "all" else "AND a.lib=%s"
    f = q(f"SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM dam_asset WHERE status='active' {lw}", la, one=True)
    cats = q(f"SELECT category, COUNT(*) n FROM dam_asset WHERE status='active' AND category IS NOT NULL {lw} GROUP BY 1 ORDER BY 2 DESC", la)
    vec = q(f"SELECT COUNT(*) n FROM dam_asset a JOIN dam_embedding e USING(asset_id) WHERE a.status='active' {lwa}", la, one=True)
    return {"total_runs": f["n"], "total_images": f["n"], "total_bytes": f["b"],
            "gb": round(f["b"]/1073741824, 2), "vectorized": vec["n"],
            "categories": [{"k": c["category"], "n": c["n"]} for c in cats]}

def _u_categories(lib):
    lib = _norm_lib(lib)
    rows = q("SELECT category, COUNT(*) n FROM dam_asset WHERE lib=%s AND status='active' AND category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC", (lib,))
    major = {}; sub = []
    for r in rows:
        cat = r["category"]; n = r["n"]; sub.append({"k": cat, "n": n})
        m = cat.split("/")[0] if "/" in cat else cat
        major[m] = major.get(m, 0) + n
    return {"major": [{"k": k, "n": n} for k, n in sorted(major.items(), key=lambda x: -x[1])], "sub": sub}

def _u_colors(lib):
    lib = _norm_lib(lib)
    lw = "" if lib == "all" else "AND lib=%s"; la = [] if lib == "all" else [lib]
    rows = q(f"SELECT hue k, COUNT(*) n FROM dam_asset WHERE status='active' AND hue IS NOT NULL {lw} GROUP BY 1 ORDER BY 2 DESC", la)
    return [{"k": r["k"], "n": r["n"]} for r in rows]

def _u_timeline(lib):
    lib = _norm_lib(lib)
    rows = q("SELECT to_char(created_ts::date,'YYYY-MM-DD') d, COUNT(*) n FROM dam_asset WHERE lib=%s AND status='active' AND created_ts IS NOT NULL GROUP BY 1 ORDER BY 1", (lib,))
    return {"days": [{"d": r["d"], "n": r["n"]} for r in rows]}

def _u_browse(lib, category, kw, sort, model, workflow, color, date_from, date_to, near, tag, fold, limit, offset, quality=""):
    lib = _norm_lib(lib)
    where, args, use_near, near_hex = _u_filters(lib, category, kw, color, date_from, date_to, near, tag, fold, quality)
    if model:    where.append("gr.params->>'model'=%s"); args.append(model)
    if workflow: where.append("gr.workflow_name=%s"); args.append(workflow)
    wsql = " AND ".join(where)
    order = _color_dist_order(near_hex, "a.ave_rgb") if use_near else \
            {"newest": "a.created_ts DESC NULLS LAST, a.asset_id DESC", "oldest": "a.created_ts ASC, a.asset_id ASC",
             "large": "a.bytes DESC", "hd": "a.width*a.height DESC"}.get(sort, "a.created_ts DESC NULLS LAST")
    rows = q(f"{_ASSET_SELECT} {_ASSET_FROM} WHERE {wsql} ORDER BY {order} OFFSET %s LIMIT %s", args + [offset, limit])
    total = q(f"SELECT COUNT(*) n {_ASSET_FROM} WHERE {wsql}", args, one=True)["n"]
    return {"items": [_asset_item(r) for r in rows], "total": total}

def _u_search(lib, query, limit, offset, category, color, date_from, date_to, tag, workflow):
    lib = _norm_lib(lib)
    if not query: return {"items": [], "total": 0}
    enc_q, translated = _maybe_translate(query)
    try:
        vec_str = _encode_text(enc_q)
    except Exception as e:
        raise HTTPException(500, f"CLIP error: {e}")
    where, args, _, _ = _u_filters(lib, category, "", color, date_from, date_to, "", tag, 0)
    if workflow: where.append("gr.workflow_name=%s"); args.append(workflow)
    wsql = " AND ".join(where)
    N = offset + limit + 60
    # 1) 向量候选(CLIP,英文译文,HNSW 快)
    vfrm = f"{_ASSET_FROM} JOIN dam_embedding e ON e.asset_id=a.asset_id"
    total = q(f"SELECT COUNT(*) n {vfrm} WHERE {wsql} AND e.embedding IS NOT NULL", args, one=True)["n"]
    vrows = q(f"""{_ASSET_SELECT}, 1 - (e.embedding <=> %s::vector) AS score
                  {vfrm} WHERE {wsql} AND e.embedding IS NOT NULL
                  ORDER BY e.embedding <=> %s::vector LIMIT %s""",
              [vec_str] + args + [vec_str, N])
    # 2) 文本候选(原始 query 子串命中聚合文本 dam_text:含VLM画意+标签+prompt,trgm 加速)
    tfrm = f"{_ASSET_FROM} JOIN dam_text dt ON dt.asset_id=a.asset_id"
    trows = q(f"""{_ASSET_SELECT}, 0.0 AS score
                  {tfrm} WHERE {wsql} AND dt.txt ILIKE %s LIMIT %s""",
              args + ['%' + query + '%', N])
    # 3) RRF 融合(文本精确命中权重更高)
    fused = {}
    for i, r in enumerate(vrows):
        fused.setdefault(r["asset_id"], {"r": r, "s": 0.0})["s"] += 1.0 / (60 + i)
    for i, r in enumerate(trows):   # 纯重排:只给向量候选里也命中文本的项加分,不引入纯文本噪声
        if r["asset_id"] in fused: fused[r["asset_id"]]["s"] += 0.7 / (60 + i)
    ranked = sorted(fused.values(), key=lambda x: -x["s"])[offset: offset + limit]
    items = []
    for x in ranked:
        it = _asset_item(x["r"]); it["score"] = round(float(x["r"].get("score") or 0), 4); items.append(it)
    return {"items": items, "total": total, "translated": translated}

def _u_similar(token, limit):
    a = _one_asset(token)
    if not a: return []
    row = q("SELECT embedding::text v FROM dam_embedding WHERE asset_id=%s", (a["asset_id"],), one=True)
    if not row: return []
    rows = q(f"""{_ASSET_SELECT}, 1 - (e.embedding <=> %s::vector) AS score
                 {_ASSET_FROM} JOIN dam_embedding e ON e.asset_id=a.asset_id
                 WHERE a.lib=%s AND a.status='active' AND a.asset_id != %s
                 ORDER BY e.embedding <=> %s::vector LIMIT %s""",
             (row["v"], a["lib"], a["asset_id"], row["v"], limit))
    out = []
    for r in rows:
        it = _asset_item(r); it["score"] = round(float(r["score"]), 4); out.append(it)
    return out

def _u_detail(token):
    a = _one_asset(token)
    if not a: raise HTTPException(404)
    meta = _meta(a["lib"])
    if meta == "gen" and a.get("ext_table") == "gen_run" and a.get("ext_id"):
        r = q("""SELECT gr.*, gf.cos_key, gf.width, gf.height, gf.bytes, gf.fmt,
                        gf.category, gf.ave_rgb, gf.hue, gf.q_status, gf.quality, gf.vlm
                 FROM gen_run gr LEFT JOIN gen_file gf ON gr.instance_id=gf.instance_id AND gf.role='output' AND gf.cos_key=%s
                 WHERE gr.instance_id=%s""", (a["cos_key"], a["ext_id"]), one=True)
        if not r: raise HTTPException(404)
        return {"id": r["instance_id"], "lib": a["lib"], "workflow": r["workflow_name"], "status": r["status"],
                "prompt": r["input_prompt"], "final_prompt": r["final_prompt"],
                "system_prompt": r["system_prompt"], "model": (r["params"] or {}).get("model"),
                "params": r["params"], "seed": r["seed"], "category": r["category"],
                "w": r["width"], "h": r["height"], "size": r["bytes"], "fmt": r["fmt"],
                "ave_rgb": r["ave_rgb"], "hue": r["hue"], "q_status": r["q_status"], "quality": r["quality"], "vlm": r.get("vlm"),
                "time": int(r["start_time"].timestamp()*1000) if r["start_time"] else None,
                "image": img_url(a["cos_key"], bucket=a["bucket"]), "thumb": img_url(a["cos_key"], 500, a["bucket"])}
    if meta == "media":
        r = q("""SELECT src_id, display_name, description, category, cos_key, width, height,
                        size, ave_rgb, hue, created_ts, type, src_url, meta
                 FROM zaowuyun_media WHERE src_id=%s""", (a["ext_id"],), one=True)
        if not r: raise HTTPException(404)
        it = _asset_item(a); it["type"] = r.get("type"); it["src_url"] = r.get("src_url")
        it["size"] = r.get("size"); it["category"] = r.get("category")
        return it
    it = _asset_item(a)
    it["provenance"] = q("SELECT bucket,region,source_id,ownership,modified_at FROM public.dam_cos_object WHERE bucket=%s AND cos_key=%s", (a["bucket"],a["cos_key"]), one=True)
    # cos-only 库(如国画/年画):附 VLM 鉴赏标注
    art = q("SELECT data FROM dam_vlm WHERE lib=%s AND item_id=%s", (a["lib"], _ext_of(a)), one=True)
    if art: it["art"] = art["data"]
    return it

def _color_dist_order(hexv, col="ave_rgb"):
    """按主色(col,#rrggbb)与目标色的 RGB 欧氏距离升序;hexv 已校验为 6 位 hex。"""
    tr, tg, tb = int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
    return (f"(power(('x'||substr({col},2,2))::bit(8)::int - {tr},2)"
            f" + power(('x'||substr({col},4,2))::bit(8)::int - {tg},2)"
            f" + power(('x'||substr({col},6,2))::bit(8)::int - {tb},2)) ASC")

# ── 中文查询翻译(CLIP是英文模型,中文query先翻成英文再编码) ──
_translate_cache = {}
def _has_cjk(s):
    return any('一' <= ch <= '鿿' for ch in (s or ""))

def _maybe_translate(query):
    if not _has_cjk(query):
        return query, None
    if query in _translate_cache:
        t = _translate_cache[query]; return t, t
    try:
        cfg = _cfg("~/.opendam/.aha_llm.json")
        body = json.dumps({"model": cfg.get("model", "gpt-4o-mini"), "temperature": 0, "max_tokens": 60,
            "messages": [
                {"role": "system", "content": "Translate the user's image search query to a concise English phrase for CLIP image retrieval. Output ONLY the English phrase, no quotes, no explanation."},
                {"role": "user", "content": query}]}).encode()
        req = urllib.request.Request(cfg["base"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        t = r["choices"][0]["message"]["content"].strip().strip('"').strip()
        if t:
            _translate_cache[query] = t; return t, t
    except Exception:
        pass
    return query, None

def _encode_text(query):
    import numpy as np, torch, clip as clip_module
    model, device = get_clip_model()
    tok = clip_module.tokenize([query]).to(device)
    with torch.no_grad():
        emb = model.encode_text(tok)
        emb = emb / emb.norm(dim=-1, keepdim=True)
    vec = emb.cpu().numpy().astype(np.float32).flatten()
    return '[' + ','.join(f'{v:.6f}' for v in vec) + ']'

# ── CLIP 模型缓存 ──
_clip_model = None; _clip_device = None
def get_clip_model():
    global _clip_model, _clip_device
    if _clip_model is None:
        import torch, clip
        _clip_device = "cuda" if torch.cuda.is_available() else "cpu"
        _clip_model, _ = clip.load("ViT-B/32", device=_clip_device)
    return _clip_model, _clip_device

# ════════════════════════════════════════════════════════════════════
#  路由层。规范路由(?lib=)+ 老路由 shim(gen 无前缀 / media 加 /media)。
# ════════════════════════════════════════════════════════════════════
@app.get("/healthz")
def healthz(): return {"ok": True, "ts": int(time.time())}

@app.get("/libs")
def libs():
    """库清单 + 计数(供前端竖向分组导航);按家族(group)排序。"""
    rows = q("SELECT lib, COUNT(*) n FROM dam_asset WHERE status='active' GROUP BY 1")
    cnt = {r["lib"]: r["n"] for r in rows}
    out = [{"lib": k, "label": v["label"], "meta": v["meta"], "group": v.get("group", "其它"), "n": cnt.get(k, 0)}
           for k, v in LIBS.items() if cnt.get(k, 0) > 0]
    order = {g: i for i, g in enumerate(LIB_GROUP_ORDER)}
    out.sort(key=lambda x: (order.get(x["group"], 99), -x["n"]))
    return out

@app.get("/overview")
def overview():
    """全库合计统计(跨所有 lib)。供侧栏「全部资产」总计条。"""
    r = q("""SELECT count(*) n, COALESCE(SUM(bytes),0) b,
                    count(DISTINCT lib) libs, count(DISTINCT category) cats
             FROM dam_asset WHERE status='active'""", one=True)
    v = q("SELECT count(*) n FROM dam_embedding e JOIN dam_asset a USING(asset_id) WHERE a.status='active'", one=True)
    groups = q("""SELECT lib, count(*) n FROM dam_asset WHERE status='active' GROUP BY 1""")
    by_group = {}
    for lib_row in groups:
        g = LIBS.get(lib_row["lib"], {}).get("group", "其它")
        by_group[g] = by_group.get(g, 0) + lib_row["n"]
    return {"images": r["n"], "bytes": r["b"], "gb": round(r["b"]/1073741824, 2),
            "libs": r["libs"], "categories": r["cats"], "vectorized": v["n"],
            "groups": [{"g": g, "n": by_group.get(g, 0)} for g in LIB_GROUP_ORDER if by_group.get(g)]}

# —— 规范路由(lib 参数,默认 gen;新库走这套) ——
@app.get("/stats")
def stats(lib: str = "gen"): return _u_stats(lib)

@app.get("/categories")
def categories(lib: str = "gen"): return _u_categories(lib)

@app.get("/colors")
def colors(lib: str = "gen"): return _u_colors(lib)

@app.get("/timeline")
def timeline(lib: str = "gen"): return _u_timeline(lib)

@app.get("/browse")
def browse(lib: str = "gen", category: str = "", kw: str = Query("", alias="q"), sort: str = "newest",
           model: str = "", workflow: str = "", color: str = "", date_from: str = "", date_to: str = "",
           near: str = "", tag: str = "", fold: int = 0, quality: str = "", limit: int = 60, offset: int = 0):
    return _u_browse(lib, category, kw, sort, model, workflow, color, date_from, date_to, near, tag, fold, limit, offset, quality)

@app.get("/search")
def semantic_search(lib: str = "gen", query: str = Query("", alias="q"), limit: int = 30, offset: int = 0,
                    category: str = "", color: str = "", date_from: str = "", date_to: str = "", tag: str = "", workflow: str = ""):
    return _u_search(lib, query, limit, offset, category, color, date_from, date_to, tag, workflow)

@app.get("/similar/{token}")
def similar(token: str, limit: int = 12): return _u_similar(token, limit)

@app.get("/detail/{token}")
def detail(token: str): return _u_detail(token)

@app.get("/workflows")
def workflows(lib: str = "gen"):
    lib = _norm_lib(lib)
    if _meta(lib) != "gen": return []
    rows = q("""SELECT gr.workflow_name k, COUNT(*) n FROM dam_asset a JOIN gen_run gr ON gr.instance_id=a.ext_id
                WHERE a.lib=%s AND a.status='active' AND gr.workflow_name IS NOT NULL AND gr.workflow_name!='' GROUP BY 1 ORDER BY 2 DESC""", (lib,))
    return [{"k": r["k"], "n": r["n"]} for r in rows]

@app.get("/models")
def models(lib: str = "gen"):
    lib = _norm_lib(lib)
    if _meta(lib) != "gen": return []
    rows = q("""SELECT gr.params->>'model' k, COUNT(*) n FROM dam_asset a JOIN gen_run gr ON gr.instance_id=a.ext_id
                WHERE a.lib=%s AND a.status='active' AND gr.params->>'model' IS NOT NULL AND gr.params->>'model'!='' GROUP BY 1 ORDER BY 2 DESC""", (lib,))
    return [{"k": r["k"], "n": r["n"]} for r in rows]

@app.get("/variations/{token}")
def variations(token: str, limit: int = 24):
    a = _one_asset(token)
    if not a or _meta(a["lib"]) != "gen": return {"prompt": None, "total": 0, "items": []}
    r = q("SELECT input_prompt FROM gen_run WHERE instance_id=%s", (a["ext_id"],), one=True)
    if not r or not r["input_prompt"]: return {"prompt": None, "total": 0, "items": []}
    prompt = r["input_prompt"]
    total = q("""SELECT COUNT(*) n FROM gen_run gr JOIN gen_file gf ON gf.instance_id=gr.instance_id AND gf.role='output'
                 WHERE md5(gr.input_prompt)=md5(%s) AND gr.instance_id != %s""", (prompt, a["ext_id"]), one=True)["n"]
    rows = q("""SELECT gr.instance_id, gr.workflow_name, gr.start_time, gf.cos_key, gf.width, gf.height, gf.category
                FROM gen_run gr JOIN gen_file gf ON gf.instance_id=gr.instance_id AND gf.role='output'
                WHERE md5(gr.input_prompt)=md5(%s) AND gr.instance_id != %s
                ORDER BY gr.start_time DESC LIMIT %s""", (prompt, a["ext_id"], limit))
    items = [{"id": r["instance_id"], "lib": "gen", "workflow": r["workflow_name"],
              "w": r["width"], "h": r["height"], "category": r["category"],
              "image": img_url(a["cos_key"], bucket=a["bucket"]), "thumb": img_url(a["cos_key"], 500, a["bucket"]),
              "time": int(r["start_time"].timestamp()*1000) if r["start_time"] else None} for r in rows]
    return {"prompt": prompt, "total": total, "items": items}

# —— 老 /media/* 路由 shim(向后兼容,前端 media 库零回归) ——
@app.get("/media/stats")
def media_stats(): return _u_stats("media")
@app.get("/media/categories")
def media_categories(): return _u_categories("media")
@app.get("/media/colors")
def media_colors(): return _u_colors("media")
@app.get("/media/browse")
def media_browse(category: str = "", kw: str = Query("", alias="q"), sort: str = "newest",
                 color: str = "", date_from: str = "", date_to: str = "", near: str = "", tag: str = "",
                 limit: int = 60, offset: int = 0):
    return _u_browse("media", category, kw, sort, "", "", color, date_from, date_to, near, tag, 0, limit, offset)
@app.get("/media/detail/{token}")
def media_detail(token: str): return _u_detail(token)
@app.get("/media/search")
def media_search(query: str = Query("", alias="q"), limit: int = 30, offset: int = 0,
                 category: str = "", color: str = "", date_from: str = "", date_to: str = "", tag: str = ""):
    return _u_search("media", query, limit, offset, category, color, date_from, date_to, tag, "")
@app.get("/media/similar/{token}")
def media_similar(token: str, limit: int = 12): return _u_similar(token, limit)

# ════════════════════════════════════════════════════════════════════
#  投票(gen_vote 表跨库共用:id 不冲突 → instance_id 当通用对外 id)。
# ════════════════════════════════════════════════════════════════════
@app.post("/vote")
def vote(instance_id: str, score: int, voter: str = "anonymous"):
    if score == 0:
        ex("DELETE FROM gen_vote WHERE instance_id=%s AND voter=%s", (instance_id, voter[:40]))
        return {"ok": True}
    if score not in (1, -1): raise HTTPException(400)
    ex("INSERT INTO gen_vote (instance_id,voter,score) VALUES(%s,%s,%s) ON CONFLICT(instance_id,voter) DO UPDATE SET score=%s,created_at=now()",
       (instance_id, voter[:40], score, score))
    return {"ok": True}

@app.get("/votes")
def get_votes(ids: str = ""):
    if not ids: return {}
    rows = q("""SELECT instance_id, SUM(CASE WHEN score=1 THEN 1 ELSE 0 END) likes,
                       SUM(CASE WHEN score=-1 THEN 1 ELSE 0 END) dislikes
                FROM gen_vote WHERE instance_id = ANY(%s) GROUP BY 1""", (ids.split(","),))
    return {r["instance_id"]: {"likes": r["likes"], "dislikes": r["dislikes"]} for r in rows}

@app.get("/voted")
def get_voted(voter: str = "anonymous"):
    rows = q("SELECT instance_id, score FROM gen_vote WHERE voter=%s", (voter,))
    return {r["instance_id"]: r["score"] for r in rows}

# ════════════════════════════════════════════════════════════════════
#  命名集合(collections):跨库挑图攒成命名集合。item_id = 对外 id。
# ════════════════════════════════════════════════════════════════════
def _resolve_items(pairs):
    """pairs=[(lib,item_id,added_at)] → 卡片结构,保持传入顺序。lib 经统一表反查,支持任意库。"""
    amap = {_ext_of(r): _asset_item(r) for r in _assets_by_tokens([p[1] for p in pairs])}
    return [amap[iid] for _, iid, _ in pairs if iid in amap]

@app.get("/collections")
def collections_list(owner: str = ""):
    if not owner: return []
    rows = q("""SELECT c.id, c.name, c.created_at,
                       (SELECT count(*) FROM dam_collection_item i WHERE i.coll_id=c.id) n
                FROM dam_collection c WHERE c.owner=%s ORDER BY c.created_at DESC""", (owner,))
    out = []
    for r in rows:
        cover = q("SELECT lib,item_id FROM dam_collection_item WHERE coll_id=%s ORDER BY added_at DESC LIMIT 1", (r["id"],), one=True)
        thumb = None
        if cover:
            it = _resolve_items([(cover["lib"], cover["item_id"], None)])
            thumb = it[0]["thumb"] if it else None
        out.append({"id": r["id"], "name": r["name"], "n": r["n"],
                    "created": int(r["created_at"].timestamp()*1000) if r["created_at"] else None, "cover": thumb})
    return out

@app.post("/collections")
def collection_create(name: str, owner: str):
    name = (name or "").strip()[:60]
    if not name or not owner: raise HTTPException(400, "name/owner required")
    cn = pg(); cur = cn.cursor()
    cur.execute("INSERT INTO dam_collection(name,owner) VALUES(%s,%s) RETURNING id", (name, owner))
    cid = cur.fetchone()[0]; cn.commit(); cur.close(); cn.close()
    return {"id": cid, "name": name, "n": 0}

@app.post("/collections/rename")
def collection_rename(id: int, name: str, owner: str):
    ex("UPDATE dam_collection SET name=%s WHERE id=%s AND owner=%s", ((name or '').strip()[:60], id, owner))
    return {"ok": True}

@app.delete("/collections")
def collection_delete(id: int, owner: str):
    ex("DELETE FROM dam_collection WHERE id=%s AND owner=%s", (id, owner))
    return {"ok": True}

@app.post("/collections/add")
def collection_add(id: int, owner: str, lib: str = "gen", ids: str = ""):
    if not q("SELECT 1 FROM dam_collection WHERE id=%s AND owner=%s", (id, owner), one=True):
        raise HTTPException(403, "not your collection")
    lib = _norm_lib(lib)
    n = 0; cn = pg(); cur = cn.cursor()
    for iid in [s for s in ids.split(",") if s][:200]:
        cur.execute("INSERT INTO dam_collection_item(coll_id,lib,item_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (id, lib, iid)); n += cur.rowcount
    cn.commit(); cur.close(); cn.close()
    return {"added": n}

@app.post("/collections/remove")
def collection_remove(id: int, owner: str, lib: str = "gen", ids: str = ""):
    if not q("SELECT 1 FROM dam_collection WHERE id=%s AND owner=%s", (id, owner), one=True):
        raise HTTPException(403, "not your collection")
    ex("DELETE FROM dam_collection_item WHERE coll_id=%s AND lib=%s AND item_id = ANY(%s)",
       (id, _norm_lib(lib), [s for s in ids.split(",") if s]))
    return {"ok": True}

@app.get("/collections/items")
def collection_items(id: int, limit: int = 120, offset: int = 0):
    total = q("SELECT count(*) n FROM dam_collection_item WHERE coll_id=%s", (id,), one=True)["n"]
    rows = q("SELECT lib,item_id,added_at FROM dam_collection_item WHERE coll_id=%s ORDER BY added_at DESC OFFSET %s LIMIT %s", (id, offset, limit))
    return {"items": _resolve_items([(r["lib"], r["item_id"], r["added_at"]) for r in rows]), "total": total}

# ── 只读分享门户 ──
def _share_token(): return hashlib.sha256(os.urandom(16)).hexdigest()[:18]

@app.post("/collections/share")
def collection_share(id: int, owner: str, allow_download: int = 1):
    own = q("SELECT share_token FROM dam_collection WHERE id=%s AND owner=%s", (id, owner), one=True)
    if not own: raise HTTPException(403, "not your collection")
    token = own["share_token"] or _share_token()
    ex("UPDATE dam_collection SET share_token=%s, allow_download=%s WHERE id=%s", (token, bool(allow_download), id))
    return {"token": token, "allow_download": bool(allow_download)}

@app.post("/collections/unshare")
def collection_unshare(id: int, owner: str):
    ex("UPDATE dam_collection SET share_token=NULL WHERE id=%s AND owner=%s", (id, owner))
    return {"ok": True}

@app.get("/share/{token}")
def share_view(token: str, limit: int = 500):
    c = q("SELECT id,name,allow_download FROM dam_collection WHERE share_token=%s", (token,), one=True)
    if not c: raise HTTPException(404, "分享不存在或已取消")
    rows = q("SELECT lib,item_id,added_at FROM dam_collection_item WHERE coll_id=%s ORDER BY added_at DESC LIMIT %s", (c["id"], limit))
    items = _resolve_items([(r["lib"], r["item_id"], r["added_at"]) for r in rows])
    return {"name": c["name"], "allow_download": c["allow_download"], "count": len(items), "items": items}

# ════════════════════════════════════════════════════════════════════
#  轻量账号(dam_user):用户名 + 4-8位PIN。owner='u:<username>'。
# ════════════════════════════════════════════════════════════════════
def _pin_hash(username, pin):
    return hashlib.sha256(f"dam:{username}:{pin}".encode()).hexdigest()

@app.post("/login")
def login(username: str, pin: str):
    username = (username or "").strip()[:24]; pin = (pin or "").strip()
    if not username or not re.fullmatch(r'\d{4,8}', pin):
        raise HTTPException(400, "用户名必填,PIN 为 4-8 位数字")
    h = _pin_hash(username, pin)
    row = q("SELECT pin_hash FROM dam_user WHERE username=%s", (username,), one=True)
    if row is None:
        ex("INSERT INTO dam_user(username,pin_hash,last_login) VALUES(%s,%s,now())", (username, h)); created = True
    else:
        if row["pin_hash"] != h: raise HTTPException(403, "PIN 不正确")
        ex("UPDATE dam_user SET last_login=now() WHERE username=%s", (username,)); created = False
    return {"ok": True, "username": username, "owner": f"u:{username}", "created": created}

@app.post("/claim")
def claim(from_owner: str = "", to_owner: str = ""):
    if not from_owner or not to_owner or from_owner == to_owner: return {"moved": 0}
    moved = 0
    for sql in ["UPDATE gen_vote SET voter=%s WHERE voter=%s",
                "UPDATE dam_collection SET owner=%s WHERE owner=%s",
                "UPDATE dam_smart_folder SET owner=%s WHERE owner=%s"]:
        cn = pg(); cur = cn.cursor()
        try:
            cur.execute(sql, (to_owner, from_owner)); moved += cur.rowcount; cn.commit()
        except Exception:
            cn.rollback()
        finally:
            cur.close(); cn.close()
    return {"moved": moved}

# ════════════════════════════════════════════════════════════════════
#  智能文件夹/动态集合(dam_smart_folder)。
# ════════════════════════════════════════════════════════════════════
@app.get("/smartfolders")
def smartfolders_list(owner: str = ""):
    if not owner: return []
    rows = q("SELECT id, name, lib, rule, created_at FROM dam_smart_folder WHERE owner=%s ORDER BY created_at DESC", (owner,))
    return [{"id": r["id"], "name": r["name"], "lib": r["lib"], "rule": r["rule"],
             "created": int(r["created_at"].timestamp()*1000) if r["created_at"] else None} for r in rows]

@app.post("/smartfolders")
def smartfolder_create(name: str, owner: str, lib: str = "gen", rule: str = "{}"):
    name = (name or "").strip()[:60]
    if not name or not owner: raise HTTPException(400, "name/owner required")
    try:
        rule_obj = json.loads(rule or "{}")
    except Exception:
        rule_obj = {}
    cn = pg(); cur = cn.cursor()
    cur.execute("INSERT INTO dam_smart_folder(name,owner,lib,rule) VALUES(%s,%s,%s,%s) RETURNING id",
                (name, owner, _norm_lib(lib), json.dumps(rule_obj)))
    sid = cur.fetchone()[0]; cn.commit(); cur.close(); cn.close()
    return {"id": sid, "name": name}

@app.post("/smartfolders/rename")
def smartfolder_rename(id: int, name: str, owner: str):
    ex("UPDATE dam_smart_folder SET name=%s WHERE id=%s AND owner=%s", ((name or '').strip()[:60], id, owner))
    return {"ok": True}

@app.delete("/smartfolders")
def smartfolder_delete(id: int, owner: str):
    ex("DELETE FROM dam_smart_folder WHERE id=%s AND owner=%s", (id, owner))
    return {"ok": True}

# ════════════════════════════════════════════════════════════════════
#  标签系统(dam_tag_item):AI 自动标 + 人工标。lib 现支持任意库。
# ════════════════════════════════════════════════════════════════════
@app.get("/tags")
def tags_cloud(lib: str = "gen", facet: str = ""):
    lib = _norm_lib(lib)
    where = []; args = []
    if lib != "all": where.append("lib=%s"); args.append(lib)
    if facet: where.append("facet=%s"); args.append(facet)
    else: where.append("facet <> '元素'")   # 元素=3万种长尾碎词,只进搜索文本,不进标签云导航
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = q(f"SELECT facet, tag, COUNT(*) n FROM dam_tag_item{wsql} GROUP BY 1,2 ORDER BY 1, 3 DESC", args)
    return [{"facet": r["facet"], "tag": r["tag"], "n": r["n"]} for r in rows]

@app.get("/tags/of")
def tags_of(lib: str = "gen", id: str = ""):
    lib = _norm_lib(lib)
    rows = q("SELECT tag, facet, source, conf FROM dam_tag_item WHERE lib=%s AND item_id=%s ORDER BY source, conf DESC NULLS LAST", (lib, id))
    return [{"tag": r["tag"], "facet": r["facet"], "source": r["source"], "conf": r["conf"]} for r in rows]

@app.post("/tags/add")
def tags_add(lib: str = "gen", ids: str = "", tag: str = ""):
    lib = _norm_lib(lib); tag = (tag or "").strip()[:40]
    id_list = [s for s in ids.split(",") if s][:500]
    if not tag or not id_list: raise HTTPException(400, "tag/ids required")
    cn = pg(); cur = cn.cursor(); n = 0
    for iid in id_list:
        cur.execute("""INSERT INTO dam_tag_item(lib,item_id,tag,facet,source,conf)
                       VALUES(%s,%s,%s,'人工','manual',NULL) ON CONFLICT DO NOTHING""", (lib, iid, tag)); n += cur.rowcount
    cn.commit(); cur.close(); cn.close()
    return {"added": n}

@app.post("/tags/remove")
def tags_remove(lib: str = "gen", id: str = "", tag: str = ""):
    ex("DELETE FROM dam_tag_item WHERE lib=%s AND item_id=%s AND tag=%s", (_norm_lib(lib), id, (tag or "").strip()))
    return {"ok": True}

# ════════════════════════════════════════════════════════════════════
#  使用统计:热门资产 + 我最近用过。
# ════════════════════════════════════════════════════════════════════
@app.get("/hot")
def hot(lib: str = "gen", limit: int = 80):
    lib = _norm_lib(lib)
    rank = q("""
      WITH coll AS (SELECT item_id, COUNT(*) c FROM dam_collection_item WHERE lib=%s GROUP BY 1),
           fav  AS (SELECT instance_id, COUNT(*) c FROM gen_vote WHERE score=1 GROUP BY 1)
      SELECT item, COALESCE(c.c,0)*2+COALESCE(f.c,0) score, COALESCE(c.c,0) colls, COALESCE(f.c,0) favs
      FROM (SELECT item_id item FROM coll UNION SELECT instance_id FROM fav) u
      LEFT JOIN coll c ON c.item_id=u.item
      LEFT JOIN fav f ON f.instance_id=u.item
      ORDER BY score DESC LIMIT %s
    """, (lib, limit))
    stat = {r["item"]: r for r in rank}
    items = _resolve_items([(lib, r["item"], None) for r in rank])
    for it in items:
        s = stat.get(it["id"])
        if s: it["hot"] = s["score"]; it["colls"] = s["colls"]; it["favs"] = s["favs"]
    items.sort(key=lambda it: -(it.get("hot") or 0))
    return {"items": items, "total": len(items)}

@app.get("/recent")
def recent(owner: str = "", limit: int = 80):
    if not owner: return {"items": [], "total": 0}
    rows = q("SELECT instance_id, created_at FROM gen_vote WHERE voter=%s AND score=1 ORDER BY created_at DESC LIMIT %s", (owner, limit))
    pairs = [(None, r["instance_id"], r["created_at"]) for r in rows]   # lib 由统一表反查,无需猜
    return {"items": _resolve_items(pairs), "total": len(pairs)}

# ── 批量打包下载 ──
_ZIP_SSL = ssl.create_default_context(); _ZIP_SSL.check_hostname = False; _ZIP_SSL.verify_mode = ssl.CERT_NONE
IMG_BASE = "https://dam.example.com"

def _cos_keys_for(ids):
    """对外 id → (id, cos_key, 名字提示, bucket),经统一表,跨库通用。"""
    if not ids: return []
    rows = _assets_by_tokens(ids)
    out = []
    for r in rows:
        if not r.get("cos_key"): continue
        nm = r.get("workflow_name") or r.get("display_name") or (r.get("cos_key").rsplit("/", 1)[-1])
        out.append((_ext_of(r), r["cos_key"], nm or "", r.get("bucket")))
    return out

def _fetch_original(cos_key, bucket=None):
    import urllib.parse
    safe_key = urllib.parse.quote(cos_key, safe="/")
    if bucket and bucket != AHA_BUCKET:   # 其它桶直接走 COS 公网
        url = f"https://{bucket}.cos.ap-shanghai.myqcloud.com/{safe_key}"
    else:
        url = f"{IMG_BASE}/img/{safe_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30, context=_ZIP_SSL).read()

@app.get("/zip")
def zip_download(lib: str = "gen", ids: str = "", limit: int = 80):
    id_list = [s for s in ids.split(",") if s][:limit]
    if not id_list: raise HTTPException(400, "no ids")
    entries = _cos_keys_for(id_list)
    if not entries: raise HTTPException(404, "nothing to pack")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        seen = set()
        for i, (iid, cos_key, nm, bucket) in enumerate(entries, 1):
            try:
                raw = _fetch_original(cos_key, bucket)
            except Exception:
                continue
            ext = cos_key.rsplit(".", 1)[-1] if "." in cos_key else "png"
            safe = "".join(ch for ch in (nm or "")[:40] if ch not in '\\/:*?"<>|').strip()
            for e in (".png", ".jpg", ".jpeg", ".webp"):
                if safe.lower().endswith(e): safe = safe[:-len(e)]
            safe = safe or str(iid)[:12]
            fname = f"{i:03d}_{safe}.{ext}"
            while fname in seen: fname = f"{i:03d}x_{safe}.{ext}"
            seen.add(fname)
            zf.writestr(fname, raw)
    buf.seek(0)
    fn = f"dam_{_norm_lib(lib)}_{len(entries)}p.zip"
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})
