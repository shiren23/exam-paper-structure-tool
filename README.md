# 试卷结构化工具

上传试卷图片，调用大模型 API，自动提取题目结构并可视化标注坐标框。

## 功能

- 输入试卷图片 + 提示词，调用 OpenAI 兼容接口（支持 Qwen、Doubao、DeepSeek 等）
- 展示模型返回的 JSON 结果（语法高亮，一键复制/导出）
- 在图片上绘制 bbox 标注，支持按部件类型（题干/小问/作答/画图区/学生）和题号筛选
- 提示词模板保存/加载，API 配置自动持久化

## 安装

```bash
pip install flask openai pillow
```

## 使用

```bash
python3 exam_gui.py
```

浏览器打开 **http://127.0.0.1:5000**

1. 填写 API 配置（Base URL、API Key、模型名称）
2. 上传试卷图片
3. 输入提示词（可保存为模板复用）
4. 点击「运行」，查看 JSON 结果和可视化标注
5. 导出 JSON 或标注图片

## 文件说明

| 文件 | 说明 |
|------|------|
| `exam_gui.py` | 主程序（Flask Web 应用） |
| `compare.py` | 从 docx 提取坐标并与标答对比，生成 HTML 报告 |
| `visualize.py` | 在试卷图片上绘制 bbox，生成可视化 HTML |

运行时自动生成：`config.json`（API配置）、`prompt_templates/`（模板）、`uploads/`、`results/`

## 支持的模型输出格式

模型返回的 JSON 需包含 `question_list`，每个题目支持以下 bbox 字段（坐标为 0-1000 归一化）：

```json
{
  "question_list": [{
    "question_id": "14",
    "stem": { "content": "题目文本", "bbox": [x1, y1, x2, y2] },
    "sub_questions": [{
      "sub_id": "(1)",
      "stem": { "content": "", "bbox": [x1, y1, x2, y2] },
      "student_solution": { "content": "", "bbox": [x1, y1, x2, y2] }
    }],
    "student_solution": { "content": "", "bbox": [x1, y1, x2, y2] },
    "drawing_area": { "bbox": [x1, y1, x2, y2] }
  }]
}
```
