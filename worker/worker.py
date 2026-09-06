#!/usr/bin/env python3
"""open-dam · AI worker

从 task 队列领任务(FOR UPDATE SKIP LOCKED),对每张新图跑幂等管线:
  1) 尺寸/格式        PIL
  2) phash            imagehash(完全相同的 phash → dupe_of 折叠)
  3) 主色+色相         PIL 1x1 平均色 → HSV 分桶
  4) CLIP 向量         OpenCLIP ViT-B/32 → embedding 表(语义搜/以图搜图)
  5) 零样本打标        文本标签集相似度 → tags + category
  6) 质检             边缘方差测糊 + 近纯色测废 → status=active|rejected

AI_ENABLED=false 时只做 1-3 和 6(无语义能力,其余功能不受影响)。
"""
import os, sys, time, json, math, colorsys
import psycopg2, psycopg2.extras
from PIL import Image, ImageFile, ImageFilter
import numpy as np

ImageFile.LOAD_TRUNCATED = True
Image.MAX_IMAGE_PIXELS = None

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://postgres:postgres@db:5432/opendam")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
AI_ENABLED = os.environ.get("AI_ENABLED", "true").lower() == "true"
CLIP_MODEL = os.environ.get("CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED = os.environ.get("CLIP_PRETRAINED", "laion2b_s34b_b79k")
POLL_IDLE_S = 2.0

# 零样本标签集(英文提示 → 中文标签;CLIP 对英文更稳)
ZSL_LABELS = [
    ("a product photo on clean background", "产品图"),
    ("a scene or lifestyle photo with products", "场景图"),
    ("a fashion or apparel image", "服装"),
    ("an illustration or drawing", "插画"),
    ("a poster or graphic design", "平面设计"),
    ("a UI screenshot or web design", "界面设计"),
    ("a portrait of a person", "人像"),
    ("a landscape or nature photo", "风景"),
    ("food or beverage photo", "美食"),
    ("architecture or interior photo", "建筑空间"),
    ("a 3D render", "三维渲染"),
    ("a texture or material close-up", "材质纹理"),
    ("a logo or icon", "标志图标"),
    ("a painting or artwork", "艺术作品"),
]

def pg():
    return psycopg2.connect(DATABASE_URL)

# ── CLIP 惰性加载 ──
_clip = {}
def clip():
    if "model" not in _clip:
        import open_clip, torch
        model, _, preprocess = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
        tok = open_clip.get_tokenizer(CLIP_MODEL)
        model.eval()
        _clip.update(model=model, preprocess=preprocess, tok=tok, torch=torch)
    return _clip

def vec_str(v):
    return "[" + ",".join(f"{x:.6f}" for x in v.tolist()) + "]"

def clip_image_vec(im):
    c = clip()
    with c["torch"].no_grad():
        t = c["preprocess"](im).unsqueeze(0)
        v = c["model"].encode_image(t)
        v = v / v.norm(dim=-1, keepdim=True)
    return vec_str(v[0])

def zero_shot(im):
    """返回 (category, tags)"""
    c = clip()
    with c["torch"].no_grad():
        ti = c["preprocess"](im).unsqueeze(0)
        vi = c["model"].encode_image(ti)
        vi = vi / vi.norm(dim=-1, keepdim=True)
        tt = c["tok"]([en for en, _ in ZSL_LABELS])
        vt = c["model"].encode_text(tt)
        vt = vt / vt.norm(dim=-1, keepdim=True)
        sims = (vi @ vt.T)[0]
    top = sims.argsort(descending=True)[:3]
    tags = [ZSL_LABELS[i][1] for i in top if sims[i] > 0.20]
    category = ZSL_LABELS[int(sims.argmax())][1]
    return category, tags or [category]

# ── 图像特征 ──
def avg_color_and_hue(im):
    px = im.convert("RGB").resize((1, 1), Image.BOX).getpixel((0, 0))
    r, g, b = px
    ave = f"#{r:02x}{g:02x}{b:02x}"
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.15: hue = "黑"
    elif s < 0.12 and v > 0.85: hue = "白"
    elif s < 0.12: hue = "中性"
    else:
        deg = h * 360
        buckets = [(15, "红"), (45, "橙"), (75, "黄"), (165, "绿"), (195, "青"),
                   (255, "蓝"), (285, "紫"), (330, "粉"), (361, "红")]
        hue = next(name for limit, name in buckets if deg < limit)
    return ave, hue

def phash_of(im):
    import imagehash
    return str(imagehash.phash(im.convert("RGB")))

def hamming(h1, h2):
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")

def sharpness(im):
    """边缘灰度方差,越大越清晰;近纯色/全黑趋近 0"""
    g = im.convert("L").resize((256, 256), Image.BOX).filter(ImageFilter.FIND_EDGES)
    a = np.asarray(g, dtype=np.float32)
    return float(a.var())

# ── 单图管线 ──
def ingest(cur, asset_id):
    cur.execute("SELECT * FROM asset WHERE asset_id=%s", (asset_id,))
    a = cur.fetchone()
    if not a or a["status"] != "processing":
        return "skip"
    path = os.path.join(DATA_DIR, a["storage_key"])
    im = Image.open(path); im.load()
    w, h = im.size
    ave, hue = avg_color_and_hue(im)
    ph = phash_of(im)
    score = sharpness(im)

    # phash 完全相同的先入资产 → 折叠
    cur.execute("""SELECT asset_id, phash FROM asset
                   WHERE status='active' AND phash IS NOT NULL AND asset_id<>%s""", (asset_id,))
    dupe_of = None
    for r in cur.fetchall():
        if hamming(ph, r["phash"]) <= 4:
            dupe_of = r["asset_id"]; break

    # 质检:过糊或近纯色 → rejected(永不物理删)
    status = "active"
    if score < 5.0:
        status = "rejected"

    category, tags = (None, [])
    emb = None
    if AI_ENABLED and status == "active":
        category, tags = zero_shot(im)
        emb = clip_image_vec(im)

    cur.execute("""UPDATE asset SET width=%s, height=%s, ave_rgb=%s, hue=%s, phash=%s,
                   q_score=%s, dupe_of=%s, status=%s, category=%s, tags=%s
                   WHERE asset_id=%s""",
                (w, h, ave, hue, ph, score, dupe_of, status, category, tags, asset_id))
    if emb:
        cur.execute("""INSERT INTO embedding (asset_id, embedding) VALUES (%s, %s::vector)
                       ON CONFLICT (asset_id) DO NOTHING""", (asset_id, emb))
    return status

# ── 队列循环 ──
def claim(cn):
    cur = cn.cursor()
    cur.execute("""WITH c AS (
                     SELECT task_id FROM task
                     WHERE status='pending' AND run_after<=now()
                     ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED
                   )
                   UPDATE task t SET status='running', attempts=attempts+1
                   FROM c WHERE t.task_id=c.task_id
                   RETURNING t.task_id, t.asset_id""")
    row = cur.fetchone()
    cn.commit()
    return row

def main():
    print(f"[worker] start · AI_ENABLED={AI_ENABLED} · model={CLIP_MODEL}/{CLIP_PRETRAINED}", flush=True)
    while True:
        cn = pg()
        task = claim(cn)
        cn.close()
        if not task:
            time.sleep(POLL_IDLE_S)
            continue
        task_id, asset_id = task
        try:
            cn = pg()
            cur = cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            result = ingest(cur, asset_id)
            cur.execute("UPDATE task SET status='done' WHERE task_id=%s", (task_id,))
            cn.commit(); cn.close()
            print(f"[worker] asset {asset_id} → {result}", flush=True)
        except Exception as e:
            try:
                cn = pg()
                cur = cn.cursor()
                cur.execute("""UPDATE task SET
                                 status = CASE WHEN attempts>=5 THEN 'dead' ELSE 'pending' END,
                                 run_after = now() + (interval '1 min' * power(2, attempts)),
                                 last_error = %s
                               WHERE task_id=%s""", (f"{type(e).__name__}: {e}", task_id))
                cn.commit(); cn.close()
            except Exception:
                pass
            print(f"[worker] asset {asset_id} 失败: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

if __name__ == "__main__":
    main()
