# macro-charts 宏观数据可视化图表模块

从 `macro_data`（中国宏观）或 `macro_us_data`（美国及商品）表读取指标时间序列，生成交互式 Plotly 折线图。**与采集模块解耦**：仅依赖数据表（数据契约）与 `core`/`config` 基建，不导入采集模块代码。跑完即退，输出 JSON 数据或自包含 HTML。

## 快速开始

```bash
cd trading_lab
uv sync                                                        # 安装含 plotly 的依赖

# JSON 数据（默认输出到终端）
uv run services/macro-charts/main.py --indicator CPI
uv run services/macro-charts/main.py --indicator CPI --start-date 2020-01-01 --limit 60

# Plotly HTML 文件（默认写到 data/reports/charts/{indicator}.html）
uv run services/macro-charts/main.py --indicator CPI --output html
uv run services/macro-charts/main.py --indicator M2 --output html --out data/reports/charts/M2.html

# 多指标对比（按单位自动分配 Y 轴，最多 3 个，支持三轴）
uv run services/macro-charts/main.py --indicator CPI,PPI --output html

# 多组合合并到同一个 HTML（; 分隔组，每组一张图纵向堆叠，plotly.js 只引一次）
uv run services/macro-charts/main.py --indicator "CPI,PPI;GOLD,SILVER;DGS10" --output html
uv run services/macro-charts/main.py --table macro_us_data --indicator "UNRATE;DGS1,DGS10;GOLD,SILVER" --output html

# 美国宏观指标（macro_us_data 表，country 默认 US）
uv run services/macro-charts/main.py --table macro_us_data --indicator UNRATE
uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,UNRATE --output html
```

生成的 HTML 自带 plotly.js（`include_plotlyjs=True`），断网也能用浏览器直接打开。

## 支持指标清单

数据来自两张表，通过 `--table` 切换；`--country` 默认随表（CN/US）。**注意 `GDP` 在两表中均存在**（中国为同比%，美国为名义GDP十亿美元），重名指标必须用 `--table` 区分。

### 中国宏观（macro_data 表，country=CN）

| indicator | 名称 | 频率 | 单位 |
|-----------|------|------|------|
| CPI | 居民消费价格指数 | M | % |
| PPI | 工业生产者出厂价格指数 | M | % |
| GDP | 国内生产总值（同比） | Q | % |
| PMI | 制造业PMI | M | 指数 |
| M2 | M2货币供应量 | M | 亿元 |
| LPR1Y | LPR-1年期 | M | % |
| LPR5Y | LPR-5年期 | M | % |
| SF | 社会融资规模增量 | M | 亿元 |
| IP | 工业增加值 | M | % |
| UE | 城镇调查失业率 | M | % |

```bash
uv run services/macro-charts/main.py --indicator CPI              # 默认 macro_data 表
uv run services/macro-charts/main.py --indicator CPI,PPI          # 双轴对比
uv run services/macro-charts/main.py --indicator GDP              # 中国 GDP（同比%）
```

### 美国及商品（macro_us_data 表，country=US）

| indicator | 名称 | 频率 | 单位 | 来源 |
|-----------|------|------|------|------|
| GDP | 名义GDP | Q | 十亿美元 | FRED |
| GDPC1 | 实际GDP | Q | 十亿美元 | FRED |
| CPIAUCSL | 消费者价格指数(CPI) | M | 指数 | FRED |
| CPILFESL | 核心CPI | M | 指数 | FRED |
| PPIACO | 生产者价格指数(PPI) | M | 指数 | FRED |
| UNRATE | 失业率 | M | % | FRED |
| PAYEMS | 非农就业人数 | M | 千人 | FRED |
| ICSA | 初请失业金人数 | W | 人 | FRED |
| DGS6MO | 6个月期国债收益率 | D | % | FRED |
| DGS1 | 1年期国债收益率 | D | % | FRED |
| DGS10 | 10年期国债收益率 | D | % | FRED |
| GS10 | 10年期国债收益率(月度) | M | % | FRED |
| DTWEXBGS | 名义广义美元指数 | D | 指数 | FRED |
| DTWEXM | 名义主要货币美元指数(已停止) | D | 指数 | FRED |
| DTWEXO | 名义其他重要贸易伙伴美元指数(已停止) | D | 指数 | FRED |
| RTWEXBGS | 实际广义美元指数 | M | 指数 | FRED |
| GOLD | 黄金现货(上海金基准价) | D | 元/克 | akshare |
| SILVER | 白银现货(上海银基准价) | D | 元/千克 | akshare |
| USA_PHS | 未决房屋销售月率 | M | % | akshare |
| GOLD_INV | 黄金库存(COMEX) | D | 吨 | akshare |
| SILVER_INV | 白银库存(COMEX) | D | 吨 | akshare |

```bash
uv run services/macro-charts/main.py --table macro_us_data --indicator UNRATE           # 默认 country=US
uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,UNRATE  # 双轴对比
uv run services/macro-charts/main.py --table macro_us_data --indicator GDP              # 美国名义GDP（十亿美元）
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS1,DGS10 --output html   # 国债收益率曲线对比
uv run services/macro-charts/main.py --table macro_us_data --indicator GOLD,SILVER --output html  # 黄金白银现货对比(双轴)
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS10 --output html         # 10年期国债收益率
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS10,GOLD,DTWEXBGS --output html  # 三指标三轴对比(%,元/克,指数)
```

> **多指标 Y 轴分配**：按单位自动分配——同单位共享一轴，不同单位各占独立轴，最多 3 轴。例如 `CPI,PPI`（同为 %）走单轴；`GOLD,SILVER`（元/克 vs 元/千克）走双轴；`DGS10,GOLD,DTWEXBGS`（% / 元/克 / 指数）走三轴。每个轴的标题与刻度颜色与对应曲线一致（Viridis 色板），便于区分。

## 多组合合并到同一个 HTML

用 `;` 把多组指标组合拼到一次命令里，每组渲染成一张独立图表，纵向堆叠进**同一个 HTML 页面**（plotly.js 只内联一次，断网可用）。适合一次生成多指标 dashboard。

- `,` 分隔的是**同图对比**的指标（共享一张图、按单位分 Y 轴，最多 3 个）；
- `;` 分隔的是**不同图表**（每段 `;` 之间为一张独立图）。

```bash
# 3 张图合并：CPI+PPI 对比 / GOLD+SILVER 对比 / DGS10 单线
uv run services/macro-charts/main.py --indicator "CPI,PPI;GOLD,SILVER;DGS10" --output html

# 跨表用 --table 限定（同一次命令只能查一张表）
uv run services/macro-charts/main.py --table macro_us_data \
  --indicator "UNRATE;DGS1,DGS10;GOLD,SILVER" --output html
```

行为说明：
- 默认输出文件名用全部指标拼接，组间以 `__` 连接，如 `CPI_PPI__GOLD_SILVER__DGS10.html`；可用 `--out` 覆盖。
- 每张图各自保留完整的多 Y 轴 / 缺口 / 修订 / 频率变更逻辑，互不干扰（日期范围与频率可不同）。
- 某组查询失败（指标不存在 / 库为空）时，该组渲染为红色提示块，其余图表正常输出；CLI 退出码为 1。
- JSON 输出时，多组合结果包裹为 `{"groups": [result1, result2, ...]}`；单组合仍返回原 `dict` 结构（向后兼容）。

> **区分中美 GDP**：两表均有 `indicator=GDP`，但含义不同——中国 `GDP`（macro_data, CN）为同比%，美国 `GDP`（macro_us_data, US）为名义GDP十亿美元。务必用 `--table` 指定来源表。
>
> 指标清单随数据采集动态变化，可用 `sqlite3 data/trading.db "SELECT DISTINCT indicator,country FROM macro_data UNION SELECT DISTINCT indicator,country FROM macro_us_data"` 查询当前可用指标。

## API 参数

| 参数 | 类型 | 必填 | 说明 | 默认 |
|------|------|------|------|------|
| `indicator` | string | 是 | 指标组合：单个(`CPI`)、逗号分隔同图对比(`CPI,PPI`，最多3)、分号分隔多图合并(`CPI,PPI;GOLD,SILVER`) | — |
| `country` | string | 否 | 国家代码；未指定时随 `table`（macro_data→CN, macro_us_data→US） | 随 `table` |
| `table` | string | 否 | 数据表：`macro_data`(中国) / `macro_us_data`(美国及商品) | `macro_data` |
| `start_date` | string | 否 | 起始日期 YYYY-MM-DD | 最早记录 |
| `end_date` | string | 否 | 结束日期 YYYY-MM-DD | 今天 |
| `limit` | int | 否 | 最近 N 期（**优先级低于日期范围**，给定日期范围时忽略） | 全部 |
| `chart_type` | string | 否 | 图表类型（暂只 `line`，预留扩展） | `line` |
| `output` | string | 否 | `json` 返回数据 / `html` 返回渲染 HTML | `json` |
| `out` | string | 否 | 输出文件路径 | json→终端；html→`data/reports/charts/{indicator}.html` |

Python 调用：

```python
from query import query_chart
from render import render_html

result = query_chart(indicator="CPI", start_date="2020-01-01", limit=60)
# 美国指标：query_chart(indicator="UNRATE", country="US", table="macro_us_data")
# result 为 dict；output=json 时直接 json.dumps
html = render_html(result)   # output=html 时渲染
```

## 输出结构（JSON）

成功时：

```json
{
  "ok": true,
  "error_code": null,
  "chart_type": "line",
  "title": "居民消费价格指数(CPI)  [CN]",
  "compare": false,
  "warnings": [],
  "series": [{
    "indicator": "CPI", "name": "居民消费价格指数", "country": "CN",
    "unit": "%", "frequency": "M",
    "dates": ["2024-07-01", ...], "values": [0.5, ...], "values_display": [0.5, ...],
    "fetch_times": ["2026-08-08 10:00:00", ...], "revision_status": ["", ...],
    "count": 24, "original_count": 24, "downsampled": false,
    "single_point": false, "few_samples": false, "no_fluctuation": false,
    "all_negative": false, "revised": false,
    "gaps": [{"start": "2024-09-01", "end": "2024-11-01"}],
    "frequency_changes": [],
    "date_range": {"start": "2024-07-01", "end": "2026-06-01"}
  }]
}
```

- `values`：原始精度（Tooltip 用）；`values_display`：四舍五入 2 位（坐标轴用）。
- `gaps`：缺失区间前后有效日期，渲染为虚线+三角标记。
- `revision_status`：取自 `extra.revision_status`（`preliminary`/`revised`）。

## 错误码

| 码 | 含义 | 触发 | 提示 |
|----|------|------|------|
| 1001 | 数据库为空 | 指定 `table` 不存在或 0 行 | 暂无数据，请先运行数据采集任务 |
| 1002 | 指标不存在 | 该 indicator+country 无记录 | 该指标未收录，请检查名称是否正确 |
| 1003 | 日期格式错误 | start/end_date 非 YYYY-MM-DD | 日期格式需为 YYYY-MM-DD |
| —（ok=false） | 超过对比上限 | indicator 多于 3 个 | 最多支持 3 个指标同图对比，建议分开展示 |

错误时 `ok=false`，`message` 含提示，`series=[]`。CLI 退出码 1。

## 边界条件覆盖（需求 7.1~7.5）

| 场景 | 处理 |
|------|------|
| 数据库为空 | 1001 |
| 指标不存在 | 1002 |
| 仅 1 条 | 单点 Marker + 标题下「当前仅1期数据，趋势尚不可见」 |
| 仅 2 条 | 正常折线 + Tooltip「样本量较少，趋势参考有限」 |
| >500 期 | 等间距降采样至 200 点 + 左下角「已降采样显示，原始数据共 N 条」 |
| 区间内无数据 | 空图表 +「所选时间段内无匹配记录，请调整筛选范围」 |
| start>end | 自动交换 + 警告日志 |
| 日期格式错 | 1003 |
| start 早于最早 | 截到最早 + 日志 |
| end 晚于今天 | 截到今天 |
| 日期缺失 | 虚线连接前后有效点 + 三角标记 +「该区间数据缺失」 |
| 全负值 | 正常绘制，Y 轴自动适配 |
| 全相同/全0 | 水平线 +「数据无波动，请核实源数据」 |
| 精度>4位 | 轴显示 2 位，Tooltip 原始精度 |
| 多指标对比 | 按单位自动分配 Y 轴（同单位共享，最多 3 轴；单位各异时三轴，轴色与曲线一致） |
| 频率变更 | 竖红虚线 +「频率变更:X→Y」 |
| 数据修订 | Tooltip 含「更新时间」；初值=虚线空心圆，终值=实线实心圆；右上角「⚠ 数据已修订」 |

色彩：Viridis 色盲友好调色板。图例：指标中文名+单位。标题：名称/单位/起止/条数。

## 缓存

HTML 渲染结果未内置缓存（遵循无服务依赖原则）。如需 24h 缓存，可在外层用文件 mtime 判断：若 `data/trading.db` 的 macro_data 有新记录（比较 `fetch_time` 最大值）则重新生成，否则复用 `data/reports/charts/*.html`。

## 前端调用示例

生成的 HTML 已是自包含页面，直接 `<iframe>` 或链接打开即可。若希望前端自行渲染（后端只返 JSON）：

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head>
<body>
  <div id="chart"></div>
  <script>
    // 后端：uv run services/macro-charts/main.py --indicator CPI --output json > cpi.json
    fetch('cpi.json').then(r => r.json()).then(res => {
      if (!res.ok) { document.body.innerText = res.message; return; }
      const s = res.series[0];
      Plotly.newPlot('chart', [{
        x: s.dates, y: s.values_display, mode: 'lines+markers',
        name: `${s.name} [${s.unit}]`
      }], {
        title: res.title, xaxis: {title: '日期'}, yaxis: {title: s.unit}
      }, {responsive: true});
    });
  </script>
</body>
</html>
```

## 测试

```bash
uv run pytest tests/test_macro_charts.py -v    # 36 个用例，覆盖 7.1~7.5 + macro_us_data + 三 Y 轴
```

单测通过 monkeypatch `query._load_records` / `query._db_has_data` 构造场景，不依赖真实数据库与网络。
