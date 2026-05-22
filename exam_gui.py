#!/usr/bin/env python3
"""试卷结构化 Web 工具 — 多Prompt批量对比测试"""
import base64
import difflib
import io
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify, send_file

DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(DIR, "uploads")
RESULT_DIR = os.path.join(DIR, "results")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

TEMPLATE_DIR = os.path.join(DIR, "prompt_templates")
CONFIG_PATH = os.path.join(DIR, "config.json")
os.makedirs(TEMPLATE_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)

PART_COLORS = {"题干": "#DC3232", "小问": "#3232DC", "作答": "#32A032",
               "画图": "#B46400", "学生": "#A020F0"}
PART_COLORS_RGB = {"题干": (220,50,50), "小问": (50,50,220), "作答": (50,160,50),
                   "画图": (180,100,0), "学生": (160,32,240)}
COMP_COLORS = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6",
               "#1abc9c", "#e67e22", "#34495e", "#16a085", "#c0392b"]

_sessions = {}


# ── 健壮 JSON 解析 ──────────────────────────────────────────────
def clean_raw_response(text):
    text = text.strip()
    text = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text)
    if '<thinking>' in text and '</thinking>' not in text:
        text = text[:text.find('<thinking>')]
    for prefix in ["Bash\n", "JSON\n", "SQL\n", "```json\n", "```json", "```"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def try_parse_json(text):
    def fix_escapes(s):
        result, i, in_string = [], 0, False
        while i < len(s):
            ch = s[i]
            if ch == '"' and (i == 0 or s[i-1] != '\\'):
                in_string = not in_string; result.append(ch)
            elif ch == '\\' and in_string and i + 1 < len(s):
                nxt = s[i+1]
                result.append(ch if nxt in '"\\/bfnrtu' else '')
                result.append(nxt); i += 2; continue
            else:
                result.append(ch)
            i += 1
        return ''.join(result)
    for i, ch in enumerate(text):
        if ch not in '[{': continue
        start, end_c = ch, ']' if ch == '[' else '}'
        depth, in_str, end_pos = 0, False, -1
        for j in range(i, len(text)):
            c = text[j]
            if c == '"' and (j == 0 or text[j-1] != '\\'): in_str = not in_str
            if not in_str:
                if c == start: depth += 1
                elif c == end_c:
                    depth -= 1
                    if depth == 0: end_pos = j + 1; break
        if end_pos == -1: continue
        try: return json.loads(fix_escapes(text[i:end_pos]))
        except json.JSONDecodeError: continue
    return None


# ── bbox / text 提取 ──────────────────────────────────────────
def extract_bboxes(data):
    questions = {}
    if isinstance(data, dict):
        if "question_list" in data:
            for q in data["question_list"]: _process_question(q, questions)
        elif "question_id" in data: _process_question(data, questions)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "question_id" in item: _process_question(item, questions)
    return questions


def _process_question(q, out):
    qid = str(q.get("question_id", "?"))
    parts = []
    stem = q.get("stem", {})
    if isinstance(stem, dict) and stem.get("bbox") not in (None, [], [0,0,0,0]):
        parts.append({"part": "题干", "bbox": stem["bbox"], "content": stem.get("content", "")})
    for sq in q.get("sub_questions", []):
        sid = sq.get("sub_id", "")
        sq_stem = sq.get("stem", {})
        if isinstance(sq_stem, dict) and sq_stem.get("bbox") not in (None, [], [0,0,0,0]):
            parts.append({"part": f"小问{sid}题干", "bbox": sq_stem["bbox"], "content": sq_stem.get("content", "")})
        sol = sq.get("student_solution", {})
        if isinstance(sol, dict) and sol.get("bbox") not in (None, [], [0,0,0,0]):
            parts.append({"part": f"小问{sid}作答", "bbox": sol["bbox"], "content": sol.get("content", "")})
    sol = q.get("student_solution", {})
    if isinstance(sol, dict) and sol.get("bbox") not in (None, [], [0,0,0,0]):
        parts.append({"part": "学生作答", "bbox": sol["bbox"], "content": sol.get("content", "")})
    da = q.get("drawing_area", {})
    if isinstance(da, dict) and da.get("bbox") not in (None, [], [0,0,0,0]):
        parts.append({"part": "画图区", "bbox": da["bbox"], "content": ""})
    if parts: out[qid] = parts


def extract_texts(data):
    questions = {}
    if isinstance(data, dict):
        if "question_list" in data:
            for q in data["question_list"]: _process_text(q, questions)
        elif "question_id" in data: _process_text(data, questions)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "question_id" in item: _process_text(item, questions)
    return questions


def _process_text(q, out):
    qid = str(q.get("question_id", "?"))
    parts = []
    stem = q.get("stem", {})
    if isinstance(stem, dict) and stem.get("content"):
        parts.append({"part": "题干", "content": stem["content"]})
    for sq in q.get("sub_questions", []):
        sid = sq.get("sub_id", "")
        sq_stem = sq.get("stem", {})
        if isinstance(sq_stem, dict) and sq_stem.get("content"):
            parts.append({"part": f"小问{sid}题干", "content": sq_stem["content"]})
        sol = sq.get("student_solution", {})
        if isinstance(sol, dict) and sol.get("content"):
            parts.append({"part": f"小问{sid}作答", "content": sol["content"]})
    sol = q.get("student_solution", {})
    if isinstance(sol, dict) and sol.get("content"):
        parts.append({"part": "学生作答", "content": sol["content"]})
    if parts: out[qid] = parts


def _part_color_key(part_name):
    for key in PART_COLORS:
        if key in part_name: return key
    return "题干"


def compute_text_diff(text1, text2):
    if not text1 and not text2:
        return {"similarity": 1.0, "status": "match", "diff_html": ""}
    if not text1 or not text2:
        t1e = esc_html(text1 or ""); t2e = esc_html(text2 or "")
        return {"similarity": 0.0, "status": "miss",
                "diff_html": f'<span class="diff-del">{t1e}</span><span class="diff-add">{t2e}</span>'}
    ratio = difflib.SequenceMatcher(None, text1, text2).ratio()
    ops = difflib.SequenceMatcher(None, text1, text2).get_opcodes()
    dp = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal": dp.append(esc_html(text1[i1:i2]))
        elif tag == "replace":
            dp.append(f'<span class="diff-del">{esc_html(text1[i1:i2])}</span>')
            dp.append(f'<span class="diff-add">{esc_html(text2[j1:j2])}</span>')
        elif tag == "delete":
            dp.append(f'<span class="diff-del">{esc_html(text1[i1:i2])}</span>')
        elif tag == "insert":
            dp.append(f'<span class="diff-add">{esc_html(text2[j1:j2])}</span>')
    status = "match" if ratio > 0.9 else ("partial" if ratio > 0.5 else "miss")
    return {"similarity": round(ratio, 3), "status": status, "diff_html": "".join(dp)}


def esc_html(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ── 绘图 ──────────────────────────────────────────────────────
def draw_vis_image(image_path, layers, active_parts=None, active_qs=None):
    """layers: [{label, color, bboxes:{qid:[parts]}}]"""
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    sx, sy = w / 1000.0, h / 1000.0
    font = None
    for fp in ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"]:
        if os.path.exists(fp):
            try: font = ImageFont.truetype(fp, 14); break
            except: pass
    if font is None: font = ImageFont.load_default()
    if active_parts is None: active_parts = set(PART_COLORS.keys())
    if active_qs is None:
        active_qs = set()
        for layer in layers:
            active_qs.update(layer["bboxes"].keys())
    for layer in layers:
        for qid, parts in layer["bboxes"].items():
            if active_qs and qid not in active_qs: continue
            for p in parts:
                key = _part_color_key(p["part"])
                if key not in active_parts: continue
                bbox = p["bbox"]
                if len(bbox) != 4: continue
                x1, y1 = bbox[0]*sx, bbox[1]*sy
                x2, y2 = bbox[2]*sx, bbox[3]*sy
                c = layer["color"]
                for off in range(3): draw.rectangle([x1+off,y1+off,x2-off,y2-off], outline=c)
                label = f"{layer['label']} Q{qid} {p['part']}"
                bb = draw.textbbox((0,0), label, font=font)
                tw, th = bb[2]-bb[0]+6, bb[3]-bb[1]+4
                draw.rectangle([x1, y1-th-4, x1+tw, y1-2], fill=c)
                draw.text((x1+3, y1-th-2), label, fill=(255,255,255), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


# ── API 调用 ──────────────────────────────────────────────────
def _call_one(image_path, cfg):
    base_url, api_key, model, prompt = cfg["base_url"], cfg["api_key"], cfg["model"], cfg["prompt"]
    client = openai.OpenAI(base_url=base_url, api_key=api_key)
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(image_path)[1].lower()
    mime = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg","webp":"image/webp"}.get(ext.lstrip("."),"image/png")
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role":"user","content":[
            {"type":"text","text":prompt},
            {"type":"image_url","image_url":{"url":f"data:{mime};base64,{b64}"}},
        ]}],
        max_tokens=4096,
    )
    elapsed = time.time() - t0
    raw = resp.choices[0].message.content
    cleaned = clean_raw_response(raw)
    parsed = try_parse_json(cleaned)
    bboxes = extract_bboxes(parsed) if parsed else {}
    return {"raw": raw, "parsed": parsed, "bboxes": bboxes,
            "model": model, "elapsed": round(elapsed,1), "image_path": image_path}


# ── 路由 ──────────────────────────────────────────────────────
@app.route("/")
def index(): return HTML_PAGE


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files: return jsonify({"error":"no file"}), 400
    f = request.files["file"]
    sid = request.form.get("session_id","") or uuid.uuid4().hex[:12]
    ext = os.path.splitext(f.filename)[1] or ".png"
    path = os.path.join(UPLOAD_DIR, f"{sid}{ext}")
    f.save(path)
    _sessions.setdefault(sid, {})["image_path"] = path
    return jsonify({"session_id": sid, "filename": f.filename})


@app.route("/api/image/<sid>")
def get_image(sid):
    p = _sessions.get(sid,{}).get("image_path","")
    if p and os.path.exists(p): return send_file(p, mimetype="image/png")
    return "", 404


@app.route("/api/run", methods=["POST"])
def run_api():
    d = request.json
    sid = d.get("session_id","")
    image_path = _sessions.get(sid,{}).get("image_path","")
    if not image_path or not os.path.exists(image_path):
        return jsonify({"error":"请先上传图片"}), 400
    cfg = {"base_url": d.get("base_url","").strip(), "api_key": d.get("api_key","").strip(),
           "model": d.get("model","").strip(), "prompt": d.get("prompt","").strip()}
    if not all(cfg.values()): return jsonify({"error":"请填写完整配置"}), 400
    try:
        r = _call_one(image_path, cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    rid = uuid.uuid4().hex[:12]
    rpath = os.path.join(RESULT_DIR, f"{rid}.json")
    with open(rpath, "w", encoding="utf-8") as f: json.dump(r, f, ensure_ascii=False, indent=2, default=str)
    _sessions.setdefault(sid,{}).update({"last_result": rid, rid: r})
    return jsonify({"result_id": rid, "model": r["model"], "elapsed": r["elapsed"],
                    "n_questions": len(r["bboxes"]), "n_bboxes": sum(len(v) for v in r["bboxes"].values()),
                    "parsed_ok": r["parsed"] is not None})


@app.route("/api/batch_run", methods=["POST"])
def batch_run():
    d = request.json
    sid = d.get("session_id","")
    image_path = _sessions.get(sid,{}).get("image_path","")
    if not image_path or not os.path.exists(image_path):
        return jsonify({"error":"请先上传图片"}), 400
    configs = d.get("configs", [])
    if not configs: return jsonify({"error":"无测试配置"}), 400

    results = []
    errors = []
    def run_one(i, cfg):
        try:
            r = _call_one(image_path, cfg)
            rid = uuid.uuid4().hex[:12]
            rpath = os.path.join(RESULT_DIR, f"{rid}.json")
            with open(rpath, "w", encoding="utf-8") as f: json.dump(r, f, ensure_ascii=False, indent=2, default=str)
            _sessions.setdefault(sid,{})[rid] = r
            return {"idx": i, "name": cfg.get("name", f"#{i+1}"), "result_id": rid,
                    "model": r["model"], "elapsed": r["elapsed"],
                    "n_bboxes": sum(len(v) for v in r["bboxes"].values()),
                    "parsed_ok": r["parsed"] is not None, "error": None}
        except Exception as e:
            return {"idx": i, "name": cfg.get("name", f"#{i+1}"), "result_id": None,
                    "model": cfg.get("model",""), "elapsed": 0, "n_bboxes": 0,
                    "parsed_ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=min(len(configs), 5)) as ex:
        futures = [ex.submit(run_one, i, c) for i, c in enumerate(configs)]
        results = [f.result() for f in as_completed(futures)]
    results.sort(key=lambda x: x["idx"])
    return jsonify({"results": results})


@app.route("/api/result/<sid>/<rid>")
def get_result(sid, rid):
    r = _sessions.get(sid,{}).get(rid,{})
    if not r:
        rp = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(rp):
            with open(rp,"r",encoding="utf-8") as f: r = json.load(f)
    if not r: return jsonify({"error":"not found"}), 404
    parsed = r.get("parsed")
    return jsonify({"json": json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else r.get("raw",""),
                    "bboxes": r.get("bboxes",{}), "model": r.get("model",""), "elapsed": r.get("elapsed",0)})


@app.route("/api/batch_compare", methods=["POST"])
def batch_compare():
    d = request.json
    sid = d.get("session_id","")
    rids = d.get("result_ids", [])
    ref_json_str = d.get("ref_json", "").strip()

    # collect results
    results = []
    for rid in rids:
        r = _sessions.get(sid,{}).get(rid,{})
        if not r:
            rp = os.path.join(RESULT_DIR, f"{rid}.json")
            if os.path.exists(rp):
                with open(rp,"r",encoding="utf-8") as f: r = json.load(f)
        if r: results.append(r)

    if not results: return jsonify({"error":"无有效结果"}), 400

    # extract texts from all results
    all_texts = []
    for i, r in enumerate(results):
        parsed = r.get("parsed")
        texts = extract_texts(parsed) if parsed else {}
        all_texts.append({"name": r.get("model", f"#{i+1}"), "texts": texts, "bboxes": r.get("bboxes",{})})

    # ref texts
    ref_texts = None
    if ref_json_str:
        ref_data = try_parse_json(ref_json_str) or try_parse_json(clean_raw_response(ref_json_str))
        if ref_data: ref_texts = extract_texts(ref_data)

    # compare
    all_qids = sorted(set().union(*(set(at["texts"].keys()) for at in all_texts)))
    if ref_texts: all_qids = sorted(set(all_qids) | set(ref_texts.keys()))

    rows = []
    summary = []

    for qid in all_qids:
        # collect all parts across results + ref
        all_parts = set()
        for at in all_texts:
            for p in at["texts"].get(qid, []): all_parts.add(p["part"])
        if ref_texts:
            for p in ref_texts.get(qid, []): all_parts.add(p["part"])

        for part in sorted(all_parts):
            row = {"qid": qid, "part": part, "entries": []}
            for at in all_texts:
                content = ""
                for p in at["texts"].get(qid, []):
                    if p["part"] == part: content = p["content"]; break
                row["entries"].append({"name": at["name"], "content": content})
            if ref_texts:
                ref_content = ""
                for p in ref_texts.get(qid, []):
                    if p["part"] == part: ref_content = p["content"]; break
                row["ref_content"] = ref_content
            rows.append(row)

    # summary per model
    for at in all_texts:
        total_sim, count = 0.0, 0
        if ref_texts:
            for qid in set(at["texts"].keys()) | set(ref_texts.keys()):
                m_parts = {p["part"]: p["content"] for p in at["texts"].get(qid, [])}
                r_parts = {p["part"]: p["content"] for p in ref_texts.get(qid, [])}
                for part in set(m_parts) | set(r_parts):
                    diff = compute_text_diff(m_parts.get(part,""), r_parts.get(part,""))
                    total_sim += diff["similarity"]; count += 1
        summary.append({"name": at["name"], "avg_similarity": round(total_sim/count, 3) if count else None,
                         "n_questions": len(at["texts"]), "n_bboxes": sum(len(v) for v in at["bboxes"].values())})

    return jsonify({"rows": rows, "summary": summary, "models": [at["name"] for at in all_texts]})


@app.route("/api/batch_vis/<sid>")
def batch_vis(sid):
    rids = request.args.get("rids","").split(",")
    parts_str = request.args.get("parts","")
    qs_str = request.args.get("qs","")
    active_parts = set(parts_str.split(",")) if parts_str else set(PART_COLORS.keys())
    active_qs = set(qs_str.split(",")) if qs_str else set()

    layers = []
    for i, rid in enumerate(rids):
        r = _sessions.get(sid,{}).get(rid,{})
        if not r:
            rp = os.path.join(RESULT_DIR, f"{rid}.json")
            if os.path.exists(rp):
                with open(rp,"r",encoding="utf-8") as f: r = json.load(f)
        if not r: continue
        image_path = r.get("image_path","")
        if not image_path or not os.path.exists(image_path): continue
        color_hex = COMP_COLORS[i % len(COMP_COLORS)]
        layers.append({"label": r.get("model", f"#{i+1}"), "color": _hex_to_rgb(color_hex), "bboxes": r.get("bboxes",{})})

    if not layers: return "", 404
    buf = draw_vis_image(image_path, layers, active_parts, active_qs or None)
    return send_file(buf, mimetype="image/jpeg")


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


@app.route("/api/vis/<sid>/<rid>")
def get_vis(sid, rid):
    r = _sessions.get(sid,{}).get(rid,{})
    if not r:
        rp = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(rp):
            with open(rp,"r",encoding="utf-8") as f: r = json.load(f)
    if not r: return "", 404
    image_path = r.get("image_path","")
    if not image_path or not os.path.exists(image_path): return "", 404
    active_parts = set(request.args.get("parts","").split(",")) if request.args.get("parts") else None
    active_qs = set(request.args.get("qs","").split(",")) if request.args.get("qs") else None
    layer = [{"label": r.get("model",""), "color": PART_COLORS_RGB.get("题干", (220,50,50)), "bboxes": r.get("bboxes",{})}]
    buf = draw_vis_image(image_path, layer, active_parts, active_qs)
    return send_file(buf, mimetype="image/jpeg")


@app.route("/api/export/json/<sid>/<rid>")
def export_json(sid, rid):
    r = _sessions.get(sid,{}).get(rid,{})
    if not r:
        rp = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(rp):
            with open(rp,"r",encoding="utf-8") as f: r = json.load(f)
    if not r: return "", 404
    text = json.dumps(r.get("parsed"), ensure_ascii=False, indent=2) if r.get("parsed") else r.get("raw","")
    return send_file(io.BytesIO(text.encode("utf-8")), as_attachment=True, download_name="result.json", mimetype="application/json")


@app.route("/api/export/image/<sid>/<rid>")
def export_image(sid, rid):
    return get_vis(sid, rid)


@app.route("/api/config", methods=["GET","POST"])
def handle_config():
    if request.method == "GET":
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH,"r",encoding="utf-8") as f: return jsonify(json.load(f))
        return jsonify({})
    else:
        with open(CONFIG_PATH,"w",encoding="utf-8") as f:
            json.dump(request.json, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})


@app.route("/api/templates", methods=["GET"])
def list_tpl():
    return jsonify(sorted(f[:-4] for f in os.listdir(TEMPLATE_DIR) if f.endswith(".txt")))


@app.route("/api/templates/<name>", methods=["GET","POST"])
def tpl_detail(name):
    path = os.path.join(TEMPLATE_DIR, name+".txt")
    if request.method == "GET":
        if os.path.exists(path):
            with open(path,"r",encoding="utf-8") as f: return jsonify({"name":name,"text":f.read()})
        return "", 404
    else:
        with open(path,"w",encoding="utf-8") as f: f.write(request.json.get("text",""))
        return jsonify({"ok": True})


# ── 前端页面 ──────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>试卷结构化工具</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#333;height:100vh;display:flex;flex-direction:column}
.header{background:#fff;border-bottom:1px solid #e0e0e0;padding:10px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.header h1{font-size:18px;color:#1a1a2e}
.header .status{margin-left:auto;color:#888;font-size:13px}
.main{display:flex;flex:1;overflow:hidden}
.left{width:400px;min-width:340px;background:#fff;border-right:1px solid #e0e0e0;display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
.panel{padding:12px 14px;border-bottom:1px solid #f0f0f0}
.panel-title{font-size:13px;font-weight:600;color:#555;margin-bottom:8px;display:flex;align-items:center;justify-content:space-between}
.panel label{font-size:12px;color:#888;display:block;margin-bottom:2px}
.panel input,.panel textarea,.panel select{width:100%;padding:6px 8px;border:1px solid #ddd;border-radius:4px;font-size:13px;font-family:inherit}
.panel input:focus,.panel textarea:focus{outline:none;border-color:#6c5ce7}
.panel textarea{resize:vertical;min-height:80px;font-family:"Menlo","SF Mono",monospace;font-size:12px}
.panel .row{margin-bottom:5px}
.btn{padding:5px 14px;border:1px solid #ddd;background:#fff;border-radius:4px;font-size:13px;cursor:pointer;transition:all .15s}
.btn:hover{background:#f8f8f8}
.btn-primary{background:#6c5ce7;color:#fff;border-color:#6c5ce7}
.btn-primary:hover{background:#5a4bd1}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.btn-success{background:#00b894;color:#fff;border-color:#00b894}
.btn-sm{padding:3px 10px;font-size:12px}
.btn-danger{color:#e74c3c;border-color:#e74c3c}

.right{flex:1;display:flex;flex-direction:column;overflow:hidden}
.tabs{display:flex;background:#fff;border-bottom:2px solid #e0e0e0;flex-shrink:0}
.tab-btn{padding:10px 18px;border:none;background:none;font-size:14px;cursor:pointer;color:#888;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab-btn.active{color:#6c5ce7;border-bottom-color:#6c5ce7;font-weight:500}
.tab-content{display:none;flex:1;overflow:auto}
.tab-content.active{display:flex;flex-direction:column}

.json-wrap{flex:1;overflow:auto;padding:12px}
.json-wrap pre{font-family:"Menlo","SF Mono",monospace;font-size:12px;line-height:1.6;white-space:pre;color:#333}
.json-actions{padding:8px 12px;background:#fafafa;border-bottom:1px solid #eee;display:flex;gap:8px}

.vis-controls{padding:8px 12px;background:#fafafa;border-bottom:1px solid #eee;display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex-shrink:0}
.vis-controls label{font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer}
.color-dot{display:inline-block;width:10px;height:10px;border-radius:2px}
.vis-image-wrap{flex:1;overflow:auto;background:#2b2b2b;display:flex;align-items:center;justify-content:center;padding:12px}
.vis-image-wrap img{max-width:100%;max-height:100%;object-fit:contain}

.upload-area{border:2px dashed #d0d0d0;border-radius:8px;padding:12px;text-align:center;cursor:pointer;transition:border .2s}
.upload-area:hover{border-color:#6c5ce7}
.upload-area.has-image{border-style:solid;border-color:#00b894;padding:8px}
.upload-area img{max-width:100%;max-height:100px;border-radius:4px}

.placeholder{flex:1;display:flex;align-items:center;justify-content:center;color:#bbb;font-size:16px}

/* batch config cards */
.cfg-card{background:#fafafa;border:1px solid #e0e0e0;border-radius:6px;padding:10px 12px;margin-bottom:8px;position:relative}
.cfg-card .cfg-head{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.cfg-card .cfg-head input{flex:1;padding:4px 6px;border:1px solid #ddd;border-radius:3px;font-size:12px}
.cfg-card .cfg-body textarea{width:100%;min-height:50px;padding:4px 6px;border:1px solid #ddd;border-radius:3px;font-size:11px;font-family:"Menlo",monospace;resize:vertical}
.cfg-card .cfg-body .row{margin-bottom:3px}
.cfg-card .cfg-body label{font-size:11px;color:#999}
.cfg-card .cfg-body input{padding:4px 6px;border:1px solid #ddd;border-radius:3px;font-size:12px}
.cfg-card .color-tag{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.cfg-collapsed .cfg-body{display:none}

/* batch compare table */
.cmp-wrap{flex:1;overflow:auto;padding:12px}
.cmp-wrap table{width:100%;border-collapse:collapse;font-size:12px;background:#fff}
.cmp-wrap th{background:#6c5ce7;color:#fff;padding:5px 8px;text-align:left;position:sticky;top:0;font-size:12px}
.cmp-wrap td{padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top}
.cmp-wrap tr:hover td{background:#f8f7ff}

.summary-bar{padding:8px 12px;background:#f0edff;display:flex;gap:16px;align-items:center;font-size:13px;flex-shrink:0;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}
.badge-match{background:#d4edda;color:#155724;padding:2px 8px;border-radius:10px;font-size:11px}
.badge-partial{background:#fff3cd;color:#856404;padding:2px 8px;border-radius:10px;font-size:11px}
.badge-miss{background:#f8d7da;color:#721c24;padding:2px 8px;border-radius:10px;font-size:11px}
.diff-del{background:#fdd;color:#900;text-decoration:line-through}
.diff-add{background:#dfd;color:#060}
.sim-bar{display:inline-block;height:8px;border-radius:4px;min-width:2px}
</style>
</head>
<body>

<div class="header">
  <h1>试卷结构化工具</h1>
  <span class="status" id="status">就绪</span>
</div>

<div class="main">
<!-- LEFT PANEL -->
<div class="left">
  <!-- image -->
  <div class="panel">
    <div class="panel-title">试卷图片</div>
    <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
      <div id="uploadHint">点击选择试卷图片</div>
      <img id="thumbImg" style="display:none">
      <input type="file" id="fileInput" accept="image/*" style="display:none">
    </div>
  </div>

  <!-- test configs -->
  <div class="panel" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
    <div class="panel-title">
      测试配置
      <span>
        <button class="btn btn-sm" onclick="addCfg()">+ 添加</button>
        <button class="btn btn-sm" onclick="saveAllCfg()">保存</button>
        <button class="btn btn-sm" onclick="loadAllCfg()">加载</button>
      </span>
    </div>
    <div id="cfgList" style="flex:1;overflow-y:auto;padding-right:4px"></div>
    <div style="margin-top:8px;display:flex;gap:6px">
      <button class="btn btn-primary" style="flex:1;padding:10px;font-size:15px" id="runBtn" onclick="batchRun()">▶ 批量运行</button>
    </div>
  </div>
</div>

<!-- RIGHT PANEL -->
<div class="right">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab(0)">JSON 结果</button>
    <button class="tab-btn" onclick="switchTab(1)">可视化绘图</button>
    <button class="tab-btn" onclick="switchTab(2)">批量对比</button>
  </div>

  <!-- Tab 0: JSON -->
  <div class="tab-content active" id="tabJson">
    <div class="json-actions">
      <select id="jsonRidSelect" onchange="loadJsonBySelect()" style="padding:4px 8px;border:1px solid #ddd;border-radius:4px"></select>
      <button class="btn" onclick="copyJson()">复制</button>
      <button class="btn btn-success" onclick="exportJson()">导出</button>
    </div>
    <div class="json-wrap"><pre id="jsonView">等待运行...</pre></div>
  </div>

  <!-- Tab 1: Vis -->
  <div class="tab-content" id="tabVis">
    <div class="vis-controls" id="visControls">
      <span style="font-size:12px;color:#888">结果:</span>
      <select id="visRidSelect" onchange="updateVisImage()" style="padding:4px 8px;border:1px solid #ddd;border-radius:4px"></select>
      <span style="font-size:12px;color:#888;margin-left:8px">筛选:</span>
    </div>
    <div class="vis-image-wrap">
      <img id="visImg" src="" style="display:none">
      <div class="placeholder" id="visPlaceholder">等待运行...</div>
    </div>
  </div>

  <!-- Tab 2: Batch Compare -->
  <div class="tab-content" id="tabCompare">
    <div style="padding:8px 12px;background:#fafafa;border-bottom:1px solid #eee;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap">
      <span style="font-size:13px;font-weight:600">标答 JSON (可选):</span>
      <button class="btn btn-primary btn-sm" onclick="doBatchCompare()">对比</button>
      <span id="compareStatus" style="color:#888;font-size:12px;margin-left:auto"></span>
    </div>
    <textarea id="refJsonInput" style="width:100%;height:60px;font-family:Menlo,monospace;font-size:11px;padding:6px 12px;border:none;border-bottom:1px solid #eee;resize:vertical;flex-shrink:0" placeholder='粘贴标答 JSON (可选)...'></textarea>
    <div class="summary-bar" id="compareSummary" style="display:none"></div>
    <div class="cmp-wrap" id="compareResult"><div class="placeholder">批量运行后点击「对比」查看结果</div></div>
  </div>
</div>
</div>

<script>
const COMP_COLORS = ["#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6","#1abc9c","#e67e22","#34495e","#16a085","#c0392b"];
const PART_COLORS = {"题干":"#DC3232","小问":"#3232DC","作答":"#32A032","画图":"#B46400","学生":"#A020F0"};
let sessionId = Math.random().toString(36).slice(2,14);
let batchResults = []; // [{name, result_id, model, elapsed, ...}]
let cfgCounter = 0;

// ── image upload ──
document.getElementById('fileInput').addEventListener('change', function(e) {
  const file = e.target.files[0]; if(!file) return;
  const fd = new FormData(); fd.append('file',file); fd.append('session_id',sessionId);
  fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d => {
    if(d.error) return alert(d.error);
    document.getElementById('thumbImg').src = '/api/image/'+sessionId+'?t='+Date.now();
    document.getElementById('thumbImg').style.display = 'block';
    document.getElementById('uploadHint').textContent = d.filename;
    document.getElementById('uploadArea').classList.add('has-image');
  });
});

// ── config cards ──
function addCfg(data) {
  const id = 'cfg_'+(cfgCounter++);
  const name = data ? data.name : '测试 '+(document.querySelectorAll('.cfg-card').length+1);
  const color = COMP_COLORS[document.querySelectorAll('.cfg-card').length % COMP_COLORS.length];
  const html = `<div class="cfg-card" id="${id}">
    <div class="cfg-head">
      <span class="color-tag" style="background:${color}"></span>
      <input class="cfg-name" value="${esc(name)}" placeholder="名称">
      <button class="btn btn-sm" onclick="toggleCfg('${id}')">折叠</button>
      <button class="btn btn-sm btn-danger" onclick="removeCfg('${id}')">×</button>
    </div>
    <div class="cfg-body">
      <div class="row"><label>Base URL</label><input class="cfg-url" value="${esc(data?data.base_url:'')}" placeholder="https://api.openai.com/v1"></div>
      <div class="row"><label>API Key</label><input class="cfg-key" type="password" value="${esc(data?data.api_key:'')}" placeholder="sk-..."></div>
      <div class="row"><label>Model</label><input class="cfg-model" value="${esc(data?data.model:'')}" placeholder="gpt-4o"></div>
      <div class="row"><label>Prompt</label><textarea class="cfg-prompt">${esc(data?data.prompt:'')}</textarea></div>
    </div>
  </div>`;
  document.getElementById('cfgList').insertAdjacentHTML('beforeend', html);
}

function toggleCfg(id) {
  document.getElementById(id).classList.toggle('cfg-collapsed');
}
function removeCfg(id) {
  document.getElementById(id).remove();
  // re-color
  document.querySelectorAll('.color-tag').forEach((t,i) => t.style.background = COMP_COLORS[i%COMP_COLORS.length]);
}

function collectConfigs() {
  const cards = document.querySelectorAll('.cfg-card');
  const configs = [];
  cards.forEach(c => {
    configs.push({
      name: c.querySelector('.cfg-name').value || '未命名',
      base_url: c.querySelector('.cfg-url').value,
      api_key: c.querySelector('.cfg-key').value,
      model: c.querySelector('.cfg-model').value,
      prompt: c.querySelector('.cfg-prompt').value,
    });
  });
  return configs;
}

function saveAllCfg() {
  const cfg = { configs: collectConfigs() };
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  document.getElementById('status').textContent = '配置已保存';
}
function loadAllCfg() {
  fetch('/api/config').then(r=>r.json()).then(c => {
    if(c.configs && c.configs.length) {
      document.getElementById('cfgList').innerHTML = '';
      c.configs.forEach(cfg => addCfg(cfg));
    }
  });
}
window.onload = () => loadAllCfg();

// ── batch run ──
function batchRun() {
  const configs = collectConfigs();
  if(!configs.length) return alert('请先添加测试配置');
  const btn = document.getElementById('runBtn');
  btn.disabled = true; btn.textContent = '运行中...';
  document.getElementById('status').textContent = `正在调用 ${configs.length} 个配置...`;

  fetch('/api/batch_run',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id:sessionId, configs})
  }).then(r=>r.json()).then(d => {
    btn.disabled = false; btn.textContent = '▶ 批量运行';
    if(d.error) { alert(d.error); return; }
    batchResults = d.results;
    const ok = d.results.filter(r=>!r.error).length;
    const fail = d.results.filter(r=>r.error).length;
    document.getElementById('status').textContent = `完成: ${ok} 成功${fail?' '+fail+' 失败':''}`;
    updateResultSelects();
    if(batchResults.length) loadJsonBySelect();
  }).catch(e => {
    btn.disabled = false; btn.textContent = '▶ 批量运行';
    alert(e);
  });
}

function updateResultSelects() {
  ['jsonRidSelect','visRidSelect'].forEach(id => {
    const sel = document.getElementById(id);
    sel.innerHTML = '';
    batchResults.forEach(r => {
      if(!r.result_id) return;
      const o = document.createElement('option');
      o.value = r.result_id;
      o.textContent = `${r.name} (${r.model}) ${r.error?'[失败]':''}`;
      sel.appendChild(o);
    });
  });
  updateVisControls();
}

// ── JSON tab ──
function loadJsonBySelect() {
  const rid = document.getElementById('jsonRidSelect').value;
  if(!rid) return;
  fetch('/api/result/'+sessionId+'/'+rid).then(r=>r.json()).then(d => {
    const pre = document.getElementById('jsonView');
    pre.textContent = d.json || '无数据';
    highlightJson(pre);
  });
}
function highlightJson(pre) {
  let h = pre.textContent;
  h = h.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h = h.replace(/"([^"]+)"(\s*:)/g,'<span style="color:#0060A0;font-weight:bold">"$1"</span>$2');
  h = h.replace(/:\s*"([^"]*?)"/g,': <span style="color:#008000">"$1"</span>');
  h = h.replace(/:\s*(-?\d+\.?\d*)/g,': <span style="color:#C00000">$1</span>');
  h = h.replace(/\b(true|false|null)\b/g,'<span style="color:#800080">$1</span>');
  pre.innerHTML = h;
}
function copyJson() { navigator.clipboard.writeText(document.getElementById('jsonView').textContent); document.getElementById('status').textContent='已复制'; }
function exportJson() { const rid=document.getElementById('jsonRidSelect').value; if(rid) window.open('/api/export/json/'+sessionId+'/'+rid); }

// ── Vis tab ──
let activeParts = new Set(Object.keys(PART_COLORS));
let activeQs = new Set();
function updateVisControls() {
  const wrap = document.getElementById('visControls');
  wrap.innerHTML = '<span style="font-size:12px;color:#888">结果:</span><select id="visRidSelect" onchange="updateVisImage()" style="padding:4px 8px;border:1px solid #ddd;border-radius:4px"></select><span style="font-size:12px;color:#888;margin-left:8px">筛选:</span>';
  Object.entries(PART_COLORS).forEach(([n,c]) => {
    const lbl = document.createElement('label');
    lbl.innerHTML = `<input type="checkbox" checked onchange="togglePart('${n}',this.checked)"><span class="color-dot" style="background:${c}"></span>${n}`;
    wrap.appendChild(lbl);
  });
  // re-populate select
  const sel = wrap.querySelector('#visRidSelect');
  batchResults.forEach(r => {
    if(!r.result_id) return;
    const o = document.createElement('option'); o.value=r.result_id; o.textContent=`${r.name} (${r.model})`;
    sel.appendChild(o);
  });
}
function togglePart(n,c) { if(c) activeParts.add(n); else activeParts.delete(n); updateVisImage(); }
function updateVisImage() {
  const rid = document.getElementById('visRidSelect')?.value;
  if(!rid) return;
  const params = new URLSearchParams();
  if(activeParts.size < Object.keys(PART_COLORS).length) params.set('parts',[...activeParts].join(','));
  document.getElementById('visImg').src = '/api/vis/'+sessionId+'/'+rid+'?'+params.toString()+'&t='+Date.now();
  document.getElementById('visImg').style.display = 'block';
  document.getElementById('visPlaceholder').style.display = 'none';
}

// ── Batch Compare ──
function doBatchCompare() {
  const rids = batchResults.filter(r=>r.result_id).map(r=>r.result_id);
  if(!rids.length) return alert('请先批量运行');
  document.getElementById('compareStatus').textContent = '对比中...';
  fetch('/api/batch_compare',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id:sessionId, result_ids:rids, ref_json:document.getElementById('refJsonInput').value.trim()})
  }).then(r=>r.json()).then(d => {
    if(d.error) { alert(d.error); document.getElementById('compareStatus').textContent=''; return; }
    renderBatchCompare(d);
    document.getElementById('compareStatus').textContent = `${d.rows.length} 项`;
  });
}

function renderBatchCompare(d) {
  const models = d.models;
  const hasRef = d.rows.length && d.rows[0].ref_content !== undefined;

  // summary
  const sumEl = document.getElementById('compareSummary');
  sumEl.style.display = 'flex';
  let sumHtml = '';
  d.summary.forEach((s,i) => {
    const color = COMP_COLORS[i % COMP_COLORS.length];
    const sim = s.avg_similarity;
    const simStr = sim !== null ? (sim*100).toFixed(1)+'%' : '—';
    const barColor = sim===null ? '#ccc' : sim>0.8?'#00b894':sim>0.5?'#fdcb6e':'#d63031';
    sumHtml += `<span style="display:flex;align-items:center;gap:4px"><span class="color-tag" style="background:${color}"></span><b>${esc(s.name)}</b>
      <span style="color:#888;font-size:12px">${s.n_bboxes} bbox</span>
      ${sim!==null?`<span class="sim-bar" style="width:${Math.max(sim*80,4)}px;background:${barColor}"></span>${simStr}`:''}
    </span>`;
  });
  sumEl.innerHTML = sumHtml;

  // table
  const wrap = document.getElementById('compareResult');
  let thCols = '<th>题号</th><th>部分</th>';
  models.forEach((m,i) => { thCols += `<th><span class="color-tag" style="background:${COMP_COLORS[i%COMP_COLORS.length]}"></span>${esc(m)}</th>`; });
  if(hasRef) thCols += '<th>标答</th><th>最佳相似度</th>';
  let html = `<table><tr>${thCols}</tr>`;

  d.rows.forEach(row => {
    html += `<tr><td>Q${row.qid}</td><td>${esc(row.part)}</td>`;
    row.entries.forEach(e => { html += `<td style="max-width:200px;word-break:break-all;font-size:11px">${esc(e.content)}</td>`; });
    if(hasRef) {
      html += `<td style="max-width:200px;word-break:break-all;font-size:11px">${esc(row.ref_content)}</td>`;
      // best similarity
      let bestSim = 0;
      row.entries.forEach(e => {
        const diff = _sim(e.content, row.ref_content);
        if(diff > bestSim) bestSim = diff;
      });
      const badge = bestSim>0.9?'badge-match':bestSim>0.5?'badge-partial':'badge-miss';
      html += `<td><span class="${badge}">${(bestSim*100).toFixed(1)}%</span></td>`;
    }
    html += '</tr>';
  });
  html += '</table>';
  wrap.innerHTML = html;
}

function _sim(a,b) {
  if(!a||!b) return 0;
  let matches=0, total=Math.max(a.length,b.length);
  if(total===0) return 1;
  const sm = new (window.TextEncoder?function(){}:function(){});
  // simple similarity using length ratio + common chars
  const la=a.length, lb=b.length, maxLen=Math.max(la,lb);
  if(maxLen===0) return 1;
  // use a simple approach
  let common=0;
  const shorter=a.length<b.length?a:b, longer=a.length>=b.length?a:b;
  for(let i=0;i<shorter.length;i++) { if(shorter[i]===longer[i]) common++; }
  return common/maxLen;
}

function switchTab(idx) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i===idx));
  document.getElementById('tabJson').classList.toggle('active', idx===0);
  document.getElementById('tabVis').classList.toggle('active', idx===1);
  document.getElementById('tabCompare').classList.toggle('active', idx===2);
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// init: add one default config if empty
setTimeout(() => { if(!document.querySelectorAll('.cfg-card').length) addCfg(); }, 200);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  试卷结构化工具 — 多Prompt批量对比")
    print("  打开浏览器访问: http://127.0.0.1:5050")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5050, debug=False)
