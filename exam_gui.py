#!/usr/bin/env python3
"""试卷结构化 Web 工具 — 输入图片+提示词+API，输出JSON结果和bbox可视化"""
import base64
import io
import json
import os
import re
import time
import uuid

import openai
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, request, jsonify, send_file, send_from_directory

DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(DIR, "uploads")
RESULT_DIR = os.path.join(DIR, "results")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

TEMPLATE_DIR = os.path.join(DIR, "prompt_templates")
CONFIG_PATH = os.path.join(DIR, "config.json")
os.makedirs(TEMPLATE_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)

PART_COLORS = {
    "题干": "#DC3232",
    "小问": "#3232DC",
    "作答": "#32A032",
    "画图": "#B46400",
    "学生": "#A020F0",
}
PART_COLORS_RGB = {
    "题干": (220, 50, 50),
    "小问": (50, 50, 220),
    "作答": (50, 160, 50),
    "画图": (180, 100, 0),
    "学生": (160, 32, 240),
}

# in-memory session store
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
                in_string = not in_string
                result.append(ch)
            elif ch == '\\' and in_string and i + 1 < len(s):
                nxt = s[i+1]
                result.append(ch if nxt in '"\\/bfnrtu' else '')
                result.append(nxt)
                i += 2
                continue
            else:
                result.append(ch)
            i += 1
        return ''.join(result)

    for i, ch in enumerate(text):
        if ch not in '[{':
            continue
        start, end_c = ch, ']' if ch == '[' else '}'
        depth, in_str, end_pos = 0, False, -1
        for j in range(i, len(text)):
            c = text[j]
            if c == '"' and (j == 0 or text[j-1] != '\\'):
                in_str = not in_str
            if not in_str:
                if c == start:
                    depth += 1
                elif c == end_c:
                    depth -= 1
                    if depth == 0:
                        end_pos = j + 1
                        break
        if end_pos == -1:
            continue
        try:
            return json.loads(fix_escapes(text[i:end_pos]))
        except json.JSONDecodeError:
            continue
    return None


# ── bbox 提取 ──────────────────────────────────────────────────
def extract_bboxes(data):
    questions = {}
    if isinstance(data, dict):
        if "question_list" in data:
            for q in data["question_list"]:
                _process_question(q, questions)
        elif "question_id" in data:
            _process_question(data, questions)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "question_id" in item:
                _process_question(item, questions)
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
            parts.append({"part": f"小问{sid}题干", "bbox": sq_stem["bbox"],
                          "content": sq_stem.get("content", "")})
        sol = sq.get("student_solution", {})
        if isinstance(sol, dict) and sol.get("bbox") not in (None, [], [0,0,0,0]):
            parts.append({"part": f"小问{sid}作答", "bbox": sol["bbox"],
                          "content": sol.get("content", "")})
    sol = q.get("student_solution", {})
    if isinstance(sol, dict) and sol.get("bbox") not in (None, [], [0,0,0,0]):
        parts.append({"part": "学生作答", "bbox": sol["bbox"], "content": sol.get("content", "")})
    da = q.get("drawing_area", {})
    if isinstance(da, dict) and da.get("bbox") not in (None, [], [0,0,0,0]):
        parts.append({"part": "画图区", "bbox": da["bbox"], "content": ""})
    if parts:
        out[qid] = parts


def _part_color_key(part_name):
    for key in PART_COLORS:
        if key in part_name:
            return key
    return "题干"


# ── 绘图 ──────────────────────────────────────────────────────
def draw_vis_image(image_path, bboxes, active_parts=None, active_qs=None):
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    sx, sy = w / 1000.0, h / 1000.0

    font = None
    for fp in ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"]:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, 16)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    if active_parts is None:
        active_parts = set(PART_COLORS.keys())
    if active_qs is None:
        active_qs = set(bboxes.keys())

    for qid, parts in bboxes.items():
        if qid not in active_qs:
            continue
        for p in parts:
            key = _part_color_key(p["part"])
            if key not in active_parts:
                continue
            bbox = p["bbox"]
            if len(bbox) != 4:
                continue
            x1, y1 = bbox[0] * sx, bbox[1] * sy
            x2, y2 = bbox[2] * sx, bbox[3] * sy
            rgb = PART_COLORS_RGB.get(key, (220, 50, 50))
            for off in range(3):
                draw.rectangle([x1+off, y1+off, x2-off, y2-off], outline=rgb)
            label = f"Q{qid} {p['part']}"
            bb = draw.textbbox((0, 0), label, font=font)
            tw, th = bb[2]-bb[0]+8, bb[3]-bb[1]+4
            draw.rectangle([x1, y1-th-4, x1+tw, y1-2], fill=rgb)
            draw.text((x1+4, y1-th-2), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


# ── API 路由 ──────────────────────────────────────────────────
@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    sid = request.form.get("session_id", "")
    if not sid:
        sid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(f.filename)[1] or ".png"
    fname = f"{sid}{ext}"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)
    if sid not in _sessions:
        _sessions[sid] = {}
    _sessions[sid]["image_path"] = path
    _sessions[sid]["image_name"] = f.filename
    return jsonify({"session_id": sid, "filename": f.filename})


@app.route("/api/image/<sid>")
def get_image(sid):
    s = _sessions.get(sid, {})
    path = s.get("image_path", "")
    if path and os.path.exists(path):
        return send_file(path, mimetype="image/png")
    return "", 404


@app.route("/api/run", methods=["POST"])
def run_api():
    data = request.json
    sid = data.get("session_id", "")
    s = _sessions.get(sid, {})
    image_path = s.get("image_path", "")
    if not image_path or not os.path.exists(image_path):
        return jsonify({"error": "请先上传图片"}), 400

    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    prompt = data.get("prompt", "").strip()
    if not all([base_url, api_key, model, prompt]):
        return jsonify({"error": "请填写完整配置和提示词"}), 400

    try:
        t0 = time.time()
        client = openai.OpenAI(base_url=base_url, api_key=api_key)
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(image_path)[1].lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp"}.get(ext.lstrip("."), "image/png")
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            max_tokens=4096,
        )
        raw = resp.choices[0].message.content
        elapsed = time.time() - t0
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    cleaned = clean_raw_response(raw)
    parsed = try_parse_json(cleaned)
    bboxes = extract_bboxes(parsed) if parsed else {}

    rid = uuid.uuid4().hex[:12]
    result_data = {
        "raw": raw,
        "parsed": parsed,
        "bboxes": bboxes,
        "model": model,
        "elapsed": round(elapsed, 1),
        "image_path": image_path,
    }
    result_path = os.path.join(RESULT_DIR, f"{rid}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2, default=str)

    if sid not in _sessions:
        _sessions[sid] = {}
    _sessions[sid]["last_result"] = rid
    _sessions[sid][rid] = result_data

    n_bbox = sum(len(v) for v in bboxes.values())
    return jsonify({
        "result_id": rid,
        "model": model,
        "elapsed": round(elapsed, 1),
        "n_questions": len(bboxes),
        "n_bboxes": n_bbox,
        "parsed_ok": parsed is not None,
    })


@app.route("/api/result/<sid>/<rid>")
def get_result(sid, rid):
    s = _sessions.get(sid, {})
    r = s.get(rid, {})
    if not r:
        path = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                r = json.load(f)
    if not r:
        return jsonify({"error": "not found"}), 404
    parsed = r.get("parsed")
    bboxes = r.get("bboxes", {})
    formatted = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else r.get("raw", "")
    return jsonify({
        "json": formatted,
        "bboxes": bboxes,
        "model": r.get("model", ""),
        "elapsed": r.get("elapsed", 0),
    })


@app.route("/api/vis/<sid>/<rid>")
def get_vis(sid, rid):
    s = _sessions.get(sid, {})
    r = s.get(rid, {})
    if not r:
        path = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                r = json.load(f)
    if not r:
        return "", 404

    bboxes = r.get("bboxes", {})
    image_path = r.get("image_path", "")
    if not image_path or not os.path.exists(image_path):
        return "", 404

    active_parts = set(request.args.get("parts", "").split(",")) if request.args.get("parts") else None
    active_qs = set(request.args.get("qs", "").split(",")) if request.args.get("qs") else None

    buf = draw_vis_image(image_path, bboxes, active_parts, active_qs)
    return send_file(buf, mimetype="image/jpeg")


@app.route("/api/export/json/<sid>/<rid>")
def export_json(sid, rid):
    s = _sessions.get(sid, {})
    r = s.get(rid, {})
    if not r:
        path = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                r = json.load(f)
    if not r:
        return "", 404
    parsed = r.get("parsed")
    text = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else r.get("raw", "")
    buf = io.BytesIO(text.encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name="result.json",
                     mimetype="application/json")


@app.route("/api/export/image/<sid>/<rid>")
def export_image(sid, rid):
    s = _sessions.get(sid, {})
    r = s.get(rid, {})
    if not r:
        path = os.path.join(RESULT_DIR, f"{rid}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                r = json.load(f)
    if not r:
        return "", 404
    bboxes = r.get("bboxes", {})
    image_path = r.get("image_path", "")
    if not image_path or not os.path.exists(image_path):
        return "", 404
    active_parts = set(request.args.get("parts", "").split(",")) if request.args.get("parts") else None
    active_qs = set(request.args.get("qs", "").split(",")) if request.args.get("qs") else None
    buf = draw_vis_image(image_path, bboxes, active_parts, active_qs)
    return send_file(buf, as_attachment=True, download_name="vis_result.jpg",
                     mimetype="image/jpeg")


# ── 配置 ──────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET", "POST"])
def handle_config():
    if request.method == "GET":
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        return jsonify({})
    else:
        cfg = request.json
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})


# ── 模板 ──────────────────────────────────────────────────────
@app.route("/api/templates", methods=["GET"])
def list_tpl():
    names = sorted(f[:-4] for f in os.listdir(TEMPLATE_DIR) if f.endswith(".txt"))
    return jsonify(names)


@app.route("/api/templates/<name>", methods=["GET", "POST"])
def tpl_detail(name):
    path = os.path.join(TEMPLATE_DIR, name + ".txt")
    if request.method == "GET":
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return jsonify({"name": name, "text": f.read()})
        return "", 404
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(request.json.get("text", ""))
        return jsonify({"ok": True})


# ── 前端页面 ──────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>试卷结构化工具</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#f0f2f5; color:#333; height:100vh; display:flex; flex-direction:column; }
.header { background:#fff; border-bottom:1px solid #e0e0e0; padding:10px 20px;
          display:flex; align-items:center; gap:12px; flex-shrink:0; }
.header h1 { font-size:18px; color:#1a1a2e; }
.header .status { margin-left:auto; color:#888; font-size:13px; }

.main { display:flex; flex:1; overflow:hidden; }

/* left panel */
.left { width:380px; min-width:320px; background:#fff; border-right:1px solid #e0e0e0;
        display:flex; flex-direction:column; overflow-y:auto; flex-shrink:0; }
.panel { padding:12px 14px; border-bottom:1px solid #f0f0f0; }
.panel-title { font-size:13px; font-weight:600; color:#555; margin-bottom:8px; }
.panel label { font-size:12px; color:#888; display:block; margin-bottom:2px; }
.panel input, .panel textarea, .panel select {
  width:100%; padding:6px 8px; border:1px solid #ddd; border-radius:4px; font-size:13px;
  font-family:inherit; }
.panel input:focus, .panel textarea:focus { outline:none; border-color:#6c5ce7; }
.panel textarea { resize:vertical; min-height:160px; font-family:"Menlo","SF Mono",monospace; font-size:12px; }
.panel .row { margin-bottom:6px; }
.btn-row { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }

/* buttons */
.btn { padding:6px 16px; border:1px solid #ddd; background:#fff; border-radius:4px;
       font-size:13px; cursor:pointer; transition:all .15s; }
.btn:hover { background:#f8f8f8; }
.btn-primary { background:#6c5ce7; color:#fff; border-color:#6c5ce7; }
.btn-primary:hover { background:#5a4bd1; }
.btn-primary:disabled { opacity:.5; cursor:not-allowed; }
.btn-success { background:#00b894; color:#fff; border-color:#00b894; }

/* right panel */
.right { flex:1; display:flex; flex-direction:column; overflow:hidden; }
.tabs { display:flex; background:#fff; border-bottom:2px solid #e0e0e0; flex-shrink:0; }
.tab-btn { padding:10px 20px; border:none; background:none; font-size:14px; cursor:pointer;
           color:#888; border-bottom:2px solid transparent; margin-bottom:-2px; }
.tab-btn.active { color:#6c5ce7; border-bottom-color:#6c5ce7; font-weight:500; }
.tab-content { display:none; flex:1; overflow:auto; }
.tab-content.active { display:flex; flex-direction:column; }

/* json viewer */
.json-wrap { flex:1; overflow:auto; padding:12px; }
.json-wrap pre { font-family:"Menlo","SF Mono",monospace; font-size:12px; line-height:1.6;
                 white-space:pre; color:#333; }
.json-wrap .jk { color:#0060A0; font-weight:bold; }
.json-wrap .js { color:#008000; }
.json-wrap .jn { color:#C00000; }
.json-wrap .jb { color:#800080; }
.json-actions { padding:8px 12px; background:#fafafa; border-bottom:1px solid #eee;
                display:flex; gap:8px; }

/* vis panel */
.vis-controls { padding:8px 12px; background:#fafafa; border-bottom:1px solid #eee;
                display:flex; align-items:center; gap:8px; flex-wrap:wrap; flex-shrink:0; }
.vis-controls label { font-size:12px; display:flex; align-items:center; gap:4px; cursor:pointer; }
.color-dot { display:inline-block; width:10px; height:10px; border-radius:2px; }
.vis-image-wrap { flex:1; overflow:auto; background:#2b2b2b; display:flex;
                  align-items:center; justify-content:center; padding:12px; }
.vis-image-wrap img { max-width:100%; max-height:100%; object-fit:contain; }

/* image upload */
.upload-area { border:2px dashed #d0d0d0; border-radius:8px; padding:16px;
               text-align:center; cursor:pointer; transition:border .2s; }
.upload-area:hover { border-color:#6c5ce7; }
.upload-area.has-image { border-style:solid; border-color:#00b894; padding:8px; }
.upload-area img { max-width:100%; max-height:120px; border-radius:4px; }

/* no result placeholder */
.placeholder { flex:1; display:flex; align-items:center; justify-content:center;
               color:#bbb; font-size:16px; }
</style>
</head>
<body>

<div class="header">
  <h1>试卷结构化工具</h1>
  <span class="status" id="status">就绪</span>
</div>

<div class="main">
  <!-- left -->
  <div class="left">
    <div class="panel">
      <div class="panel-title">API 配置</div>
      <div class="row"><label>Base URL</label><input id="baseUrl" placeholder="https://api.openai.com/v1"></div>
      <div class="row"><label>API Key</label><input id="apiKey" type="password" placeholder="sk-..."></div>
      <div class="row"><label>Model</label><input id="model" placeholder="gpt-4o"></div>
    </div>

    <div class="panel">
      <div class="panel-title">图片</div>
      <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
        <div id="uploadHint">点击选择试卷图片</div>
        <img id="thumbImg" style="display:none">
        <input type="file" id="fileInput" accept="image/*" style="display:none">
      </div>
    </div>

    <div class="panel" style="flex:1; display:flex; flex-direction:column;">
      <div class="panel-title">提示词</div>
      <div class="btn-row" style="margin-bottom:6px;">
        <select id="tplSelect" style="width:120px" onchange="loadTemplate()">
          <option value="">加载模板...</option>
        </select>
        <button class="btn" onclick="saveTemplate()">保存模板</button>
      </div>
      <textarea id="promptText" style="flex:1" placeholder="输入提示词..."></textarea>
    </div>

    <div class="panel">
      <button class="btn btn-primary" style="width:100%;padding:10px;font-size:15px"
              id="runBtn" onclick="doRun()">▶ 运行</button>
    </div>
  </div>

  <!-- right -->
  <div class="right">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab(0)">JSON 结果</button>
      <button class="tab-btn" onclick="switchTab(1)">可视化绘图</button>
    </div>

    <div class="tab-content active" id="tabJson">
      <div class="json-actions">
        <button class="btn" onclick="copyJson()">复制 JSON</button>
        <button class="btn btn-success" onclick="exportJson()">导出 JSON</button>
      </div>
      <div class="json-wrap"><pre id="jsonView">等待运行...</pre></div>
    </div>

    <div class="tab-content" id="tabVis">
      <div class="vis-controls" id="visControls">
        <span style="font-size:12px;color:#888">筛选:</span>
      </div>
      <div class="vis-image-wrap">
        <img id="visImg" src="" style="display:none">
        <div class="placeholder" id="visPlaceholder">等待运行...</div>
      </div>
      <div style="padding:6px 12px;background:#fafafa;border-top:1px solid #eee;display:flex;gap:8px;flex-shrink:0;">
        <button class="btn btn-success" onclick="exportImage()">导出图片</button>
      </div>
    </div>
  </div>
</div>

<script>
let sessionId = Math.random().toString(36).slice(2,14);
let lastRid = null;
let currentBboxes = {};

// config
window.onload = function() {
  fetch('/api/config').then(r=>r.json()).then(c => {
    if(c.base_url) document.getElementById('baseUrl').value = c.base_url;
    if(c.api_key) document.getElementById('apiKey').value = c.api_key;
    if(c.model) document.getElementById('model').value = c.model;
    if(c.prompt) document.getElementById('promptText').value = c.prompt;
  });
  refreshTemplates();
};

function saveConfig() {
  fetch('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      base_url: document.getElementById('baseUrl').value,
      api_key: document.getElementById('apiKey').value,
      model: document.getElementById('model').value,
      prompt: document.getElementById('promptText').value,
    })
  });
}

// image upload
document.getElementById('fileInput').addEventListener('change', function(e) {
  const file = e.target.files[0];
  if(!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('session_id', sessionId);
  fetch('/api/upload', {method:'POST', body:fd}).then(r=>r.json()).then(d => {
    if(d.error) return alert(d.error);
    document.getElementById('thumbImg').src = '/api/image/'+sessionId+'?t='+Date.now();
    document.getElementById('thumbImg').style.display = 'block';
    document.getElementById('uploadHint').textContent = d.filename;
    document.getElementById('uploadArea').classList.add('has-image');
  });
});

// templates
function refreshTemplates() {
  fetch('/api/templates').then(r=>r.json()).then(names => {
    const sel = document.getElementById('tplSelect');
    sel.innerHTML = '<option value="">加载模板...</option>';
    names.forEach(n => { const o = document.createElement('option'); o.value=n; o.textContent=n; sel.appendChild(o); });
  });
}
function loadTemplate() {
  const name = document.getElementById('tplSelect').value;
  if(!name) return;
  fetch('/api/templates/'+name).then(r=>r.json()).then(d => {
    document.getElementById('promptText').value = d.text;
  });
}
function saveTemplate() {
  const name = prompt('模板名称:');
  if(!name) return;
  fetch('/api/templates/'+name, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text: document.getElementById('promptText').value})
  }).then(() => refreshTemplates());
}

// run
function doRun() {
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.textContent = '调用中...';
  document.getElementById('status').textContent = '正在调用 API...';
  saveConfig();

  fetch('/api/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: sessionId,
      base_url: document.getElementById('baseUrl').value,
      api_key: document.getElementById('apiKey').value,
      model: document.getElementById('model').value,
      prompt: document.getElementById('promptText').value,
    })
  }).then(r => r.json()).then(d => {
    btn.disabled = false;
    btn.textContent = '▶ 运行';
    if(d.error) {
      document.getElementById('status').textContent = '失败';
      alert(d.error);
      return;
    }
    lastRid = d.result_id;
    document.getElementById('status').textContent =
      `完成: ${d.model} (${d.elapsed}s) | ${d.n_questions}题 ${d.n_bboxes}个bbox`;
    loadResult();
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = '▶ 运行';
    document.getElementById('status').textContent = '网络错误';
    alert(e);
  });
}

function loadResult() {
  if(!lastRid) return;
  fetch('/api/result/'+sessionId+'/'+lastRid).then(r=>r.json()).then(d => {
    // JSON viewer
    const pre = document.getElementById('jsonView');
    pre.textContent = d.json || '无数据';
    highlightJson(pre);

    // vis
    currentBboxes = d.bboxes || {};
    buildVisControls();
    updateVisImage();
  });
}

function highlightJson(pre) {
  let html = pre.textContent;
  html = html.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  html = html.replace(/"([^"]+)"(\s*:)/g, '<span class="jk">"$1"</span>$2');
  html = html.replace(/:\s*"([^"]*?)"/g, ': <span class="js">"$1"</span>');
  html = html.replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="jn">$1</span>');
  html = html.replace(/\b(true|false|null)\b/g, '<span class="jb">$1</span>');
  pre.innerHTML = html;
}

// vis controls
const PART_COLORS = {"题干":"#DC3232","小问":"#3232DC","作答":"#32A032","画图":"#B46400","学生":"#A020F0"};
let activeParts = new Set(Object.keys(PART_COLORS));
let activeQs = new Set();

function buildVisControls() {
  const wrap = document.getElementById('visControls');
  wrap.innerHTML = '<span style="font-size:12px;color:#888">筛选:</span>';

  Object.entries(PART_COLORS).forEach(([name, color]) => {
    const lbl = document.createElement('label');
    lbl.innerHTML = `<input type="checkbox" checked onchange="togglePart('${name}',this.checked)">
      <span class="color-dot" style="background:${color}"></span>${name}`;
    wrap.appendChild(lbl);
  });

  const qs = Object.keys(currentBboxes);
  if(qs.length) {
    const sep = document.createElement('span');
    sep.textContent = ' | 题目:';
    sep.style.cssText = 'font-size:12px;color:#888;margin-left:8px';
    wrap.appendChild(sep);
    activeQs = new Set(qs);
    qs.forEach(qid => {
      const lbl = document.createElement('label');
      lbl.innerHTML = `<input type="checkbox" checked onchange="toggleQ('${qid}',this.checked)">Q${qid}`;
      wrap.appendChild(lbl);
    });
  }
}

function togglePart(name, checked) {
  if(checked) activeParts.add(name); else activeParts.delete(name);
  updateVisImage();
}
function toggleQ(qid, checked) {
  if(checked) activeQs.add(qid); else activeQs.delete(qid);
  updateVisImage();
}

function updateVisImage() {
  if(!lastRid || !Object.keys(currentBboxes).length) return;
  const params = new URLSearchParams();
  if(activeParts.size < Object.keys(PART_COLORS).length)
    params.set('parts', [...activeParts].join(','));
  if(activeQs.size < Object.keys(currentBboxes).length)
    params.set('qs', [...activeQs].join(','));
  const img = document.getElementById('visImg');
  img.src = '/api/vis/'+sessionId+'/'+lastRid+'?'+params.toString()+'&t='+Date.now();
  img.style.display = 'block';
  document.getElementById('visPlaceholder').style.display = 'none';
}

function switchTab(idx) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i===idx));
  document.getElementById('tabJson').classList.toggle('active', idx===0);
  document.getElementById('tabVis').classList.toggle('active', idx===1);
}

function copyJson() {
  const text = document.getElementById('jsonView').textContent;
  navigator.clipboard.writeText(text).then(() => {
    document.getElementById('status').textContent = '已复制到剪贴板';
  });
}

function exportJson() {
  if(!lastRid) return;
  window.open('/api/export/json/'+sessionId+'/'+lastRid);
}

function exportImage() {
  if(!lastRid) return;
  const params = new URLSearchParams();
  if(activeParts.size < Object.keys(PART_COLORS).length)
    params.set('parts', [...activeParts].join(','));
  if(activeQs.size < Object.keys(currentBboxes).length)
    params.set('qs', [...activeQs].join(','));
  window.open('/api/export/image/'+sessionId+'/'+lastRid+'?'+params.toString());
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  试卷结构化工具")
    print("  打开浏览器访问: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
