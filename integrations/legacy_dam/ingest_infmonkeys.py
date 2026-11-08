"""Artist response extraction adapter; no hardcoded workflow IDs or credentials."""
import os,json,re,socket,requests
API_HOST = os.environ.get('ARTIST_API_HOST', 'artist.infmonkeys.com')
BASE = 'https://' + API_HOST
_orig = socket.getaddrinfo
S = requests.Session()
S.headers.update({'Authorization':'Bearer '+os.environ.get('PLATFORM_API_TOKEN',''),'Content-Type':'application/json'})
IMG_RE=re.compile(r'https?://[^\s"\\]+?\.(?:png|jpg|jpeg|webp)', re.I)
_PARAM_KEYS=('seed','steps','cfg','sampler_name','scheduler','denoise','ckpt_name','width','height','batch_size')

def find_urls(o): return IMG_RE.findall(o if isinstance(o,str) else json.dumps(o,ensure_ascii=False))

def _parse_waj(waj):
    """从 comfyui workflow_api_json(ComfyUI 图)挖生成参数"""
    if isinstance(waj,str):
        try: waj=json.loads(waj)
        except Exception: return {}
    found={}
    def walk(o):
        if isinstance(o,dict):
            for k,v in o.items():
                if k in _PARAM_KEYS and not isinstance(v,(dict,list)): found[k]=v
                else: walk(v)
        elif isinstance(o,list):
            for v in o: walk(v)
    walk(waj); return found

def extract(da):
    """抽:原始词/优化后词/系统词/输入图/输出图/真seed/生成参数(模型·步数·cfg·采样器·尺寸·LLM·token)"""
    tasks=da.get("tasks",[]) or []
    gt=[t for t in tasks if t.get("taskType")=="third_party_api:generate_text"]
    cf=[t for t in tasks if t.get("taskType")=="comfyui:run_comfyui_workflow"]
    input_prompt=final_prompt=system_prompt=llm_model=None; input_imgs=[]; tokens=None
    if gt:
        idata=gt[0].get("inputData") or {}
        input_prompt=idata.get("userMessage"); system_prompt=idata.get("systemPrompt"); llm_model=idata.get("model")
        input_imgs=find_urls(idata.get("userImageMessage") or "")
        odata=gt[0].get("outputData") or {}
        msg=odata.get("message")
        final_prompt=msg if isinstance(msg,str) else (json.dumps(msg,ensure_ascii=False) if msg else None)
        u=odata.get("usage") or {}
        if u: tokens={"in":u.get("prompt_tokens"),"out":u.get("completion_tokens"),"total":u.get("total_tokens")}
    gp={}; out_imgs=[]
    for c in cf:
        gp.update(_parse_waj((c.get("inputData") or {}).get("workflow_api_json")))
        for f in (c.get("outputData") or {}).get("file_list",[]) or []:
            u=f.get("url") if isinstance(f,dict) else f
            if u: out_imgs.append(u)
    if not out_imgs: out_imgs=find_urls(da.get("output") or {})
    if not input_imgs: input_imgs=find_urls(da.get("input") or {})
    # 兜底:正常路径没抽到输出图,扫全文捞 minio 出图 URL(comfyui 出图路径含 /comfyui/),排除输入图
    if not out_imgs:
        cand=[u for u in find_urls(da) if "/comfyui/" in u and u not in input_imgs]
        out_imgs=cand
    wf_type=(da.get("input") or {}).get("workflow_type")
    real_seed=str(gp.get("seed")) if gp.get("seed") is not None else (str((da.get("input") or {}).get("seed") or "") or None)
    params={"model":wf_type,"ckpt":gp.get("ckpt_name"),"seed":gp.get("seed"),"steps":gp.get("steps"),
            "cfg":gp.get("cfg"),"sampler":gp.get("sampler_name"),"scheduler":gp.get("scheduler"),
            "width":gp.get("width"),"height":gp.get("height"),"llm_model":llm_model,"tokens":tokens}
    def dedup(L):
        s=set();o=[]
        for x in L:
            if x not in s: s.add(x);o.append(x)
        return o
    return {"input_prompt":input_prompt,"final_prompt":final_prompt,"system_prompt":system_prompt,
            "input_images":dedup(input_imgs),"output_images":dedup(out_imgs),"seed":real_seed,"params":params}
