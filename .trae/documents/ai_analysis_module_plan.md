# 新建 services/ai_analysis 模块实施计划

## Context（背景）

项目 `trading_lab` 已有一套成熟的 services 子模块体系（macro-data-service、yanbao-info、bankuai-service 等），每个模块遵循 `main.py` + 辅助模块 + `pyproject.toml` 的标准结构。配置统一由 `config/settings.py`（Pydantic BaseSettings）从根目录 `.env` 注入，日志统一走 `core/logger.py`。

现需新增一个 AI 分析模块 `services/ai_analysis`，调用 DeepSeek 官方 OpenAI 兼容端点的 `deepseek-v4-flash-vision-exp` 视觉模型，对 `data/reports/` 下的股票截图（含 K 线、MA5/20/60、MACD、RSI 及止损/止盈标注）进行多模态分析，结合用户输入的盘中模拟盘口数据（现价/量比/量比倍数），输出结构化 JSON 评估结果（是否值得开仓的概率与风险提示）。

现有 `core/ai_client.py` 仅是文本情绪分析的 Mock 占位（返回 0.5），不适用本场景，故在服务模块内新建专用视觉客户端，保持职责清晰。

## 关键决策（已与用户确认）

1. **输出格式**：使用 prompt 中定义的详细 JSON Schema（conditions/probability/hold_conditions/main_reason/risk_alert 五字段），不使用需求描述中的简化三字段。
2. **API 端点**：DeepSeek 官方 OpenAI 兼容端点 `https://api.deepseek.com/v1/chat/completions`，覆盖 `.env` 中 `AI_API_URL` 占位值。
3. **CLI 入参**：`--image`（PNG 文件名）、`--price-now`、`--volume-ratio`、`--vol-multiplier` 四个均为必填。

## 文件变更清单

### 1. 新建 `services/ai_analysis/main.py`（CLI 入口 + 编排）

职责：
- argparse 解析四个必填参数：`--image`（PNG 图片路径）、`--price-now`（float）、`--volume-ratio`（float）、`--vol-multiplier`（float）
- 可选参数：`--timeout`（默认 60s）、`--save`（将结果 JSON 追加保存到与图片同目录下的 `ai_analysis_<原文件名>.json`）
- **图片路径解析逻辑**：接收任意本地路径（绝对路径或相对路径）。若传入的是纯文件名（无目录分隔符，如 `report_002579_20260908.png`），则默认从 `settings.reports_dir`（即 `data/reports/`）目录下查找；若传入路径含目录（绝对路径或 `a/b/x.png` 相对路径），则按原路径解析。校验文件存在性，不存在则友好报错退出。

### 2. 新建 `services/ai_analysis/client.py`（DeepSeek 视觉客户端）

职责：
- `DeepSeekVisionClient` 类，构造时从 `settings` 读取 `ai_api_key`/`ai_api_url`/`ai_model`
- `_load_image_as_base64(path: Path) -> str`：读取 PNG 文件，返回 `data:image/png;base64,{b64}` 形式的 data URL
- `analyze_chart(image_path, price_now, volume_ratio, vol_multiplier) -> dict`：
  - 构建 OpenAI 兼容请求体：`messages` 为单条 user 消息，`content` 列表含 `{"type":"text","text":...}` 和 `{"type":"image_url","image_url":{"url": <data_url>}}`
  - 启用 `response_format={"type":"json_object"}` 强制 JSON 输出
  - 设置 `temperature=0.1`（严谨分析）
  - 用 `requests.post` 发送，带 `Authorization: Bearer {api_key}` 头
  - 内置重试（3 次，间隔 1s），复用 `macro-data-service/fetchers.py:_with_retry` 的模式
  - 解析 `resp.json()["choices"][0]["message"]["content"]` 为 JSON dict
  - 校验返回 dict 包含必需字段（conditions/probability/hold_conditions/main_reason/risk_alert），缺失字段补默认值
- 复用 `core/logger.get_logger("ai_analysis.client")`
- AI_ENABLED=False 或 api_key 为空时，返回 Mock 结果并 log.warning（便于离线测试）

### 3. 新建 `services/ai_analysis/prompt.py`（提示词模板）

职责：
- `build_prompt(price_now, volume_ratio, vol_multiplier) -> str`：返回完整提示词文本
- 提示词内容即用户提供的完整 prompt（资深量化分析师角色 + 三要素拆解 + 突破判定标准 + 概率推导逻辑 + JSON Schema 约束），将 `price_now`/`volume_ratio`/`vol_multiplier` 动态填入"强制输入的模拟数据"段落
- 严禁输出 Markdown 代码块或额外解释性文字的约束写入其中

### 4. 新建 `services/ai_analysis/pyproject.toml`（依赖面文档）

- name = "trading-lab-ai-analysis"
- dependencies = `["requests>=2.31"]`（仅作依赖面文档，实际安装由根 pyproject.toml 统一声明）

### 5. 修改 `config/settings.py`（新增 AI 模型名 + 更新默认端点）

- 在 AI 配置段新增字段：`ai_model: str = "deepseek-v4-flash-vision-exp"`
- 修改 `ai_api_url` 默认值：`"https://api.deepseek.com/v1/chat/completions"`（覆盖原占位 `https://api.example.com/v1/analyze`）

### 6. 修改 `.env.example`（同步默认值）

- `AI_API_URL` 改为 `https://api.deepseek.com/v1/chat/completions`
- 新增 `AI_MODEL=deepseek-v4-flash-vision-exp`

### 7. 修改根 `pyproject.toml`（声明 requests 依赖）

- 在 `dependencies` 列表末尾新增 `"requests>=2.31"`，并标注 `# AI 截图分析 (ai_analysis) 所需`

## 实现要点

- **图片路径**：支持任意本地路径。纯文件名（如 `report_002579_20260908.png`）默认从 `data/reports/` 查找；含目录的路径（绝对或相对，如 `/tmp/x.png` 或 `20260908-png/one/xxx.png`）按原路径解析。
- **Base64 内联**：使用 `base64.b64encode(path.read_bytes()).decode()`，拼成 `data:image/png;base64,{b64}` 的 data URL，符合 OpenAI 兼容多模态格式。
- **JSON 强制输出**：`response_format={"type":"json_object"}` 确保 DeepSeek 返回合法 JSON；解析时容错处理模型可能包裹的 ```json 代码块（strip 后 json.loads）。
- **重试机制**：网络异常重试 3 次（间隔 1s），HTTP 4xx/5xx 不重试直接报错（鉴权/配额问题重试无意义）。
- **Mock 模式**：`ai_enabled=False` 或 `ai_api_key` 为空时，返回结构正确的 Mock 结果（probability=0.5），便于离线验证 CLI 流程。
- **日志**：复用 `core/logger.get_logger`，记录请求耗时、响应状态、解析结果摘要。
- **超时**：`signal.alarm(120)` 防止 API 调用无限挂起。

## 验证方式

1. **Mock 模式离线验证**（不消耗 API 配额）：
   ```bash
   # 确保 .env 中 AI_ENABLED=false
   uv run services/ai_analysis/main.py \
     --image report_002579_20260908.png \
     --price-now 15.51 --volume-ratio 1.2 --vol-multiplier 0.9
   ```
   预期：打印 Mock JSON（probability=0.5），含完整五字段结构。

2. **真实 API 调用**（需在 .env 设置 `AI_ENABLED=true` 和真实 `AI_API_KEY`）：
   ```bash
   uv run services/ai_analysis/main.py \
     --image report_002579_20260908.png \
     --price-now 15.51 --volume-ratio 1.8 --vol-multiplier 1.5
   ```
   预期：返回含 conditions.passes_structure_A、probability、hold_conditions、main_reason、risk_alert 的 JSON。

3. **参数缺失校验**：
   ```bash
   uv run services/ai_analysis/main.py --image foo.png
   ```
   预期：argparse 报错提示 `--price-now` 等必填参数缺失。

4. **图片不存在校验**（纯文件名从 data/reports 查找）：
   ```bash
   uv run services/ai_analysis/main.py --image not_exist.png --price-now 15 --volume-ratio 1 --vol-multiplier 1
   ```
   预期：友好提示"在 data/reports/ 中未找到 not_exist.png"并退出码 1。

5. **任意路径图片**（绝对路径/相对路径）：
   ```bash
   # 绝对路径
   uv run services/ai_analysis/main.py --image /tmp/chart.png --price-now 15 --volume-ratio 1 --vol-multiplier 1
   # 相对路径（含目录）
   uv run services/ai_analysis/main.py --image 20260908-png/one/xxx.png --price-now 15 --volume-ratio 1 --vol-multiplier 1
   ```
   预期：按传入路径直接解析，不 fallback 到 data/reports。

## 复用的现有代码

- [config/settings.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/config/settings.py)：`Settings` 单例 + `reports_dir` 派生路径
- [core/logger.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/core/logger.py)：`get_logger(name)`
- [services/macro-data-service/main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/macro-data-service/main.py)：CLI 超时信号机制模式
- [services/macro-data-service/fetchers.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/macro-data-service/fetchers.py)：`_with_retry` 重试模式
