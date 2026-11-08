"""Platform response/database adapter extracted from production; config stays local."""
import os,json,psycopg2
from qcloud_cos import CosS3Client,CosConfig
API_BASE = os.environ.get('PLATFORM_API_BASE','https://app.zaowuyun.com')
TOKEN = os.environ.get('PLATFORM_API_TOKEN','')
BUCKET = os.environ.get('DAM_GENERATED_BUCKET','example-assets-1234567890')
def _cfg(p): return json.load(open(os.path.expanduser(p)))

def pg():
    d = _cfg("~/.opendam/.aha_pg.json")
    return psycopg2.connect(host=d["host"], port=d["port"], dbname=d["dbname"], user=d["user"], password=d["password"])

def extract_meta(it, wf_name):
    inp = it.get("input") or {}
    out = it.get("output") or {}
    tasks = it.get("tasks") or []
    input_prompt = None
    candidates = []
    for k, v in inp.items():
        if k == "__context": continue
        if isinstance(v, str) and len(v) > 10 and not v.startswith("http"):
            candidates.append((len(v), v))
    if candidates:
        candidates.sort(reverse=True)
        input_prompt = candidates[0][1]
    output_images = []
    if isinstance(out, dict):
        for key in ("data", "file_list"):
            data_list = out.get(key, [])
            if isinstance(data_list, list):
                output_images.extend(u for u in data_list if isinstance(u, str) and "http" in u)
    output_images = list(dict.fromkeys(output_images))
    input_images = []
    for k, v in inp.items():
        if k == "__context": continue
        if isinstance(v, str) and "http" in v:
            input_images.append(v)
    model = None
    params = {}
    for t in tasks:
        tt = t.get("taskType") or ""
        t_input = t.get("inputData") or {}
        if "openai" in tt or "gpt" in tt:
            if "model" in t_input: model = t_input["model"]
        if "comfyui" in tt:
            for pk in ("seed", "steps", "cfg", "sampler_name", "scheduler", "width", "height"):
                if pk in t_input: params[pk] = t_input[pk]
    ctx = inp.get("__context", {})
    group = ctx.get("group", "")
    return {"input_prompt": input_prompt, "model": model,
            "params": {**params, "group": group} if params else ({"group": group} if group else {}),
            "output_images": output_images, "input_images": input_images}

def cos():
    d=_cfg(os.environ.get('DAM_COS_CREDENTIAL_FILE','~/.opendam/.aha_cos.json'))
    return CosS3Client(CosConfig(Region=d['region'],SecretId=d['secret_id'],SecretKey=d['secret_key'],Timeout=30))
