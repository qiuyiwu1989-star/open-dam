-- DAM 统一向量底座 · 阶段1a 迁移/回填(幂等,可反复跑;接 cron 自动并入新数据)
-- 不重算向量:直接拷贝 gen_clip / zaowuyun_media 里现成的 embedding_vec

-- 1) 生成区 → dam_asset(lib='gen';source 含 zaowuyun_team + infmonkeys;role=output)
INSERT INTO dam_asset
  (bucket, cos_key, lib, source, ext_table, ext_id,
   width, height, bytes, fmt, category, ave_rgb, hue, created_ts, status)
SELECT 'example-assets-1234567890', gf.cos_key, 'gen', gf.source, 'gen_run', gf.instance_id,
       gf.width, gf.height, gf.bytes, gf.fmt, gf.category, gf.ave_rgb, gf.hue, gr.start_time,
       CASE WHEN gr.status = 'COMPLETED' THEN 'active' ELSE 'hidden' END
FROM gen_file gf
JOIN gen_run gr ON gr.instance_id = gf.instance_id
WHERE gf.role = 'output' AND gf.cos_key IS NOT NULL
ON CONFLICT (bucket, cos_key) DO NOTHING;

-- 2) 生成区向量 → dam_embedding(拷贝现成 gen_clip.embedding_vec)
INSERT INTO dam_embedding (asset_id, model, embedding)
SELECT a.asset_id, 'clip-vit-b32', gc.embedding_vec
FROM dam_asset a
JOIN gen_clip gc ON gc.instance_id = a.ext_id
WHERE a.lib = 'gen' AND a.ext_table = 'gen_run' AND gc.embedding_vec IS NOT NULL
ON CONFLICT (asset_id) DO NOTHING;

-- 3) 素材库 → dam_asset(lib='media')
INSERT INTO dam_asset
  (bucket, cos_key, lib, source, ext_table, ext_id,
   width, height, bytes, fmt, category, ave_rgb, hue, created_ts, status)
SELECT 'example-assets-1234567890', m.cos_key, 'media', 'zaowuyun_media', 'zaowuyun_media', m.src_id,
       m.width, m.height, m.size,
       lower(substring(m.cos_key from '\.([^.]+)$')),
       m.category, m.ave_rgb, m.hue, m.created_ts, 'active'
FROM zaowuyun_media m
WHERE m.status = 'stored' AND m.cos_key IS NOT NULL
ON CONFLICT (bucket, cos_key) DO NOTHING;

-- 4) 素材库向量 → dam_embedding(拷贝现成 zaowuyun_media.embedding_vec)
INSERT INTO dam_embedding (asset_id, model, embedding)
SELECT a.asset_id, 'clip-vit-b32', m.embedding_vec
FROM dam_asset a
JOIN zaowuyun_media m ON m.src_id = a.ext_id
WHERE a.lib = 'media' AND a.ext_table = 'zaowuyun_media' AND m.embedding_vec IS NOT NULL
ON CONFLICT (asset_id) DO NOTHING;
