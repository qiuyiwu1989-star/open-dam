# open-dam

**AI 原生、可自托管的开源数字资产管理（DAM）** —— 把图拖进来，然后用语义、颜色和相似度找到它。

An AI-native, self-hosted Digital Asset Management system. Drop your images in; find them by meaning, color, and similarity.

## 为什么

设计团队的图越来越多（拍摄稿、设计稿、AI 出图），而云盘只能按文件名找。
open-dam 让每张图在入库时自动完成：**去重 → 主色提取 → CLIP 向量 → 零样本打标 → 质量检查**，
之后你可以：

- 🔍 **语义搜索** —— 打字"红色沙发"，不靠文件名
- 🖼️ **以图搜图** —— 点一张图，找出所有相似的
- 🎨 **色彩检索** —— 按色相/指定颜色筛全库
- 🗂️ **集合管理** —— 把图组织成项目集合
- 🧬 **秒传去重** —— 内容寻址存储，重复的图上传瞬间就告诉你

全部本地模型（CLIP ViT-B/32），**不依赖任何外部 API，断网可用**。

## 快速开始（30 秒）

```bash
git clone <repo> && cd open-dam
cp .env.example .env
docker compose up -d --build
```

打开 http://localhost ，把一整个文件夹的图拖进浏览器 → 看实时去重报告 → 试着搜"红色沙发"。

> 硬件：4C8G 即可，无 GPU 也能跑（CPU 向量约 0.5s/张）。首次启动 worker 会下载 CLIP 权重（约 600MB，存入 `models/` 卷，只下一次）。

## 架构

```
浏览器 ──► Caddy(单入口 :80)
             ├── /*      → 静态前端
             ├── /img/*  → 内容寻址存储(uploads/<sha256>)
             └── /api/*  → app (FastAPI)
                            │  上传秒判重(sha256) ──► 入队
                            ▼
                     Postgres + pgvector
                     (asset / embedding HNSW / collection / task 队列)
                            ▲
                     worker(轮询任务):
                     phash 去重 → 主色+色相 → CLIP 向量
                     → 零样本打标 → 质检(测糊/测废)
```

详见 [docs/architecture.html](docs/architecture.html)（浏览器打开）。

## 技术栈

PostgreSQL + pgvector（HNSW 向量索引）· FastAPI · OpenCLIP (ViT-B/32) · 原生前端（零构建）· Docker Compose

全链路**幂等**：内容寻址 + `ON CONFLICT` + 任务队列，任何一步重跑都安全。

## API 一览

| 端点 | 说明 |
|---|---|
| `POST /api/upload` | 多文件上传，逐文件返回 新增/重复 |
| `GET /api/assets` | 浏览（色相/分类/关键词筛选，分页） |
| `GET /api/search/text?q=` | 语义搜索 |
| `GET /api/search/color?hex=` | 色彩检索 |
| `GET /api/similar/{id}` | 以图搜图 |
| `GET/POST /api/collections` | 集合管理 |
| `GET /api/stats` | 库统计 |

## Roadmap

- [ ] S3 兼容存储后端（MinIO/OSS/COS）
- [ ] zip 批量导入 / 共享盘目录扫描
- [ ] 多用户与权限（当前为单团队模式）
- [ ] 视频/设计文件预览
- [ ] Webhook 与开放 API key

## License

[Apache-2.0](LICENSE)
