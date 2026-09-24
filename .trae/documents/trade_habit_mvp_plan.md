# TradeHabitMVP 交易习惯约束器 实现计划

## 摘要

在 `services/trade_habit/` 下实现一个独立的本地 Tkinter GUI 应用，零第三方依赖，仅用 Python 3.11+ 标准库（`tkinter`、`sqlite3`、`csv`、`json`、`datetime`、`dataclasses`、`os`、`pathlib`、`tkinter.messagebox`、`tkinter.filedialog`、`tkinter.simpledialog`、`tkinter.ttk`）。

核心目标：**约束短线交易坏习惯（无计划交易=0、止损执行率=100%、计划执行率>90%、日亏上限即停）**，半自动，所有决策由人点击，系统只做计算/校验/记录/提醒/统计。

文件结构：
```
services/trade_habit/
├── main.py          # 启动入口（建库→建表→写默认设置→起 Tk 主循环）
├── db.py            # 数据库连接 + 建表 + 基础 CRUD
├── services.py      # 业务规则、计算、统计（仓位建议、盈亏R、红黄绿灯、清单校验）
├── ui.py            # 全部 Tkinter 界面（Notebook 5 页 + 弹窗）
├── README.md        # 运行说明 + 数据库位置 + 功能说明
└── trade_habit.db   # 首次启动自动生成（与 main.py 同目录）
```

## 当前状态分析

### 项目现有结构
- 工作目录：`/Users/a801/Linda/Work/project/gupiao-assistant/trading_lab`
- 服务目录：`services/<service_name>/`（已有 `alarming_monitor`、`sell_monitor`、`stock_character`、`bankuai-service` 等）
- 项目根 `pyproject.toml`：`requires-python = ">=3.11"`，统一共享 `.venv`，所有外部依赖在根 `pyproject.toml` 集中声明
- 各服务 `services/<name>/pyproject.toml` 是 `package = false` 的虚拟工作区成员，仅作依赖面文档

### 现有服务模式（参考 sell_monitor）
- 入口：`main.py` 用 `argparse` 解析 + `if __name__ == "__main__": sys.exit(main())`
- 持久化：`storage.py` 用 `@contextmanager` 包装 `sqlite3.connect`，`init_db()` 幂等建表
- DB 路径派生：`_SERVICE_DIR.parent.parent / "data" / "trading.db"`（共享项目级 DB）
- 状态类：`Position` 是 `@dataclass`，有 `to_dict` / `from_dict` 互转
- 行风格：模块级 `logger = logging.getLogger(__name__)`，错误返回码 `1`，成功 `0`
- 编码风格：`from __future__ import annotations`、`Optional[...]`、`Path` 处理路径
- 中文 docstring + 中文控制台输出（`✅`/`❌`/`⚠️`）

### 与本任务的差异
本任务是**独立的 GUI 应用**，不是 argparse CLI 工具：
1. **DB 路径不同**：用本服务目录下的 `trade_habit.db`（与 `main.py` 同级），而非项目根 `data/trading.db`，理由：(1) 该应用自我封闭，零外部数据交换；(2) 用户重启程序后数据仍在即可；(3) 不污染项目共享 DB
2. **依赖面不同**：仅标准库，不进根 `pyproject.toml` 的依赖列表；本服务 `pyproject.toml` 声明 `dependencies = []`
3. **入口不同**：直接 `python main.py`，无 `argparse`
4. **不用 pandas/numpy**：所有计算用纯 Python（仓位建议、盈亏R、红黄绿灯）
5. **不与 `run_all.py` 联动**：这是交互式 GUI，不进批量调度

## 提议改动

### 1. `services/trade_habit/pyproject.toml`（新建）

仅 6 行，声明无外部依赖、不打包：

```toml
[project]
name = "trading-lab-trade-habit"
version = "0.1.0"
description = "交易习惯约束器 MVP（本地 Tkinter GUI）"
requires-python = ">=3.11"
dependencies = []

[tool.uv]
package = false
```

### 2. `services/trade_habit/db.py`（新建）

负责：连接管理、幂等建表、6 张表 + 默认设置、基础 CRUD（按 key 取/存 settings；按主键查/插/改/删 stock_pool、trade_plan、trade_log、checklist_run、red_flag_event）。

**关键设计**

- 路径派生：`_DB_PATH = Path(__file__).resolve().parent / "trade_habit.db"`
- 连接：`@contextmanager def _connect():` → `sqlite3.connect(_DB_PATH)`，`row_factory = sqlite3.Row`，自动 commit/close
- `init_db()` 幂等：建 6 张表（CREATE TABLE IF NOT EXISTS）+ 写默认 settings（INSERT OR IGNORE）
- 全部 SQL 用命名占位符（`:key`），避免 SQL 注入
- 时间戳统一 ISO 字符串 `datetime.now().isoformat(timespec='seconds')`

**6 张表 DDL（按规范第 4 节）**

```python
# settings：键值配置
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)

# stock_pool：股票池
CREATE TABLE IF NOT EXISTS stock_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    logic TEXT,
    key_level TEXT,
    catalyst TEXT,
    risk TEXT,
    status TEXT NOT NULL DEFAULT '观察',  -- 观察/可交易/剔除/待确认
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)

# trade_plan：计划单
CREATE TABLE IF NOT EXISTS trade_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    entry_trigger TEXT,
    entry_price_low REAL,
    entry_price_high REAL,
    stop_loss REAL,
    time_stop_date TEXT,
    target_price REAL,
    planned_shares INTEGER,
    max_loss_amount REAL,
    invalidation TEXT,
    status TEXT NOT NULL DEFAULT '草稿',  -- 草稿/待触发/已入场/已结束/取消
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)

# trade_log：交易日志
CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    entry_time TEXT,
    entry_price REAL,
    shares INTEGER,
    exit_time TEXT,
    exit_price REAL,
    pnl_amount REAL,
    pnl_r REAL,
    fees REAL,
    followed_plan INTEGER,  -- 1/0
    deviation_reason TEXT,
    emotion_tag TEXT,
    emotion_intensity INTEGER,  -- 1-10
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES trade_plan(id)
)

# checklist_run：检查清单执行记录
CREATE TABLE IF NOT EXISTS checklist_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    trade_log_id INTEGER,
    checked_items TEXT,  -- JSON
    all_passed INTEGER,  -- 1/0
    override_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES trade_plan(id),
    FOREIGN KEY (trade_log_id) REFERENCES trade_log(id)
)

# red_flag_event：红灯事件
CREATE TABLE IF NOT EXISTS red_flag_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    detail TEXT,
    override_reason TEXT,
    created_at TEXT NOT NULL
)
```

**默认 settings（首次启动 INSERT OR IGNORE）**

```python
DEFAULT_SETTINGS = {
    'account_balance': '100000',
    'risk_per_trade_pct': '0.5',
    'daily_max_loss': '1000',
    'daily_max_trades': '3',
    'emotion_tags': '平静,焦虑,贪婪,恐惧,无聊,急躁',
}
```

**基础 CRUD 函数清单**
- `get_setting(key, default=None) -> str | None`
- `get_settings() -> dict[str, str]`（一次取全部）
- `set_setting(key, value)`
- `set_settings(dict_)`（批量保存）
- `list_stock_pool(status_filter=None) -> list[dict]`
- `get_stock_pool(pool_id) -> dict | None`
- `get_stock_pool_by_code(code, status=None) -> dict | None`
- `insert_stock_pool(data: dict) -> int`
- `update_stock_pool(pool_id, data: dict)`
- `delete_stock_pool(pool_id)`
- `list_trade_plans(status_filter=None) -> list[dict]`
- `get_trade_plan(plan_id) -> dict | None`
- `insert_trade_plan(data: dict) -> int`
- `update_trade_plan(plan_id, data: dict)`
- `list_trade_logs(date_str=None) -> list[dict]`
- `get_trade_log(log_id) -> dict | None`
- `get_trade_log_by_plan(plan_id) -> dict | None`
- `insert_trade_log(data: dict) -> int`
- `update_trade_log(log_id, data: dict)`
- `list_checklist_runs(date_str=None) -> list[dict]`
- `insert_checklist_run(data: dict) -> int`
- `list_red_flags(date_str=None) -> list[dict]`
- `insert_red_flag(event_type, detail, override_reason=None) -> int`
- `count_today_entries() -> int`（今日 entry_time 当天的 trade_log 数）
- `sum_today_pnl() -> float`（今日已出场 trade_log 的 pnl_amount 之和）
- `count_today_checklist_all_passed() -> int`（今日 checklist_run all_passed=1 数）
- `count_today_checklist_total() -> int`（今日 checklist_run 总数）
- `count_today_red_flag_overrides() -> int`（今日 red_flag_event 数）

### 3. `services/trade_habit/services.py`（新建）

业务规则与计算。**纯 Python 函数，无 tkinter 依赖**，便于单测与复用。

**函数清单**

```python
def suggest_position_size(account_balance: float, risk_pct: float,
                          entry_price_high: float, stop_loss: float
                          ) -> tuple[int, float, str]:
    """仓位建议计算。
    返回 (建议股数, 建议最大亏损, 警告信息)。
    - 单笔风险金额 = account_balance * risk_pct / 100
    - 每股风险 = abs(entry_price_high - stop_loss)
    - 建议股数 = max(0, floor(单笔风险金额 / 每股风险 / 100)) * 100
    - 建议最大亏损 = 建议股数 * 每股风险
    - 警告：若建议最大亏损 > 单笔风险金额，返回红字警告
    """

def validate_plan(data: dict) -> list[str]:
    """新增/编辑计划单校验。返回错误列表（空 = 通过）。
    必填：stock_code、entry_trigger、stop_loss、time_stop_date、planned_shares、max_loss_amount
    多头规则：stop_loss < entry_price_high（若 entry_price_high 给定且 > 0）
    若 stop_loss >= entry_price_high，返回 ['做多计划要求止损价 < 入场价上沿']
    """

def calc_pnl(entry_price: float, exit_price: float, shares: int,
              fees: float, stop_loss: float) -> tuple[float, float]:
    """计算盈亏金额与盈亏R。
    - pnl_amount = (exit_price - entry_price) * shares - fees
    - 初始风险 = abs(entry_price - stop_loss) * shares
    - pnl_r = pnl_amount / 初始风险（初始风险=0 时返回 0.0）
    """

def today_iso() -> str: ...  # date.today().isoformat()

def is_today(ts_iso: str) -> bool: ...  # 解析 ISO 取 date 部分 == today

def get_traffic_light(settings: dict, today_pnl: float,
                      today_entries: int) -> tuple[str, str]:
    """红黄绿灯。
    返回 (颜色, 说明)。颜色 ∈ {'green', 'yellow', 'red'}。
    - 红：today_pnl <= -daily_max_loss OR today_entries >= daily_max_trades
    - 黄：today_pnl <= -daily_max_loss*0.8 OR today_entries >= daily_max_trades*0.8
    - 绿：其他
    注意：today_pnl 为负数才触发；正盈亏不影响
    """

def build_checklist(plan: dict, settings: dict,
                    today_entries: int, traffic_light: str
                    ) -> list[dict]:
    """构建 8 项检查清单。
    每项 {key, label, auto, passed, note}：
    - auto=True 的项由系统判断；auto=False 的项默认 passed=False，待用户勾选
    - 第1项：股票在股票池且状态='可交易' → 查 get_stock_pool_by_code(plan.code, '可交易')
    - 第2项：已创建完整计划单 → plan 存在且 status != '草稿'
    - 第3项：入场触发条件已满足 → 人工勾选
    - 第4项：止损价明确且可执行 → 人工勾选
    - 第5项：时间止损已设定 → 人工勾选
    - 第6项：仓位/最大亏损未超单笔风险上限 → 比较 plan.max_loss_amount <= account_balance*risk_pct/100
    - 第7项：今日交易次数未超上限 → today_entries < daily_max_trades
    - 第8项：当前无红灯 → traffic_light != 'red'
    """

def all_checklist_passed(items: list[dict]) -> bool: ...

def build_condition_order_text(plan: dict, stock: dict | None) -> str:
    """生成条件单提醒文本（复制到剪贴板）。"""

def export_trade_logs_csv(logs: list[dict], path: str) -> None:
    """导出交易日志 CSV，编码 utf-8-sig。"""

def export_trade_plans_csv(plans: list[dict], path: str) -> None:
    """导出计划单 CSV，编码 utf-8-sig。"""

def import_stock_pool_csv(path: str) -> list[dict]:
    """从 CSV 导入股票池候选。
    每行需含 code/name（其他可选）。返回待插入的 dict 列表，status='待确认'。
    """

def import_stock_pool_json(path: str) -> list[dict]:
    """从 JSON 导入股票池候选。返回 dict 列表，status='待确认'。"""
```

### 4. `services/trade_habit/ui.py`（新建）

全部 Tkinter 界面。`TradeHabitApp(tk.Tk)` 主窗口类，构造时初始化 DB + 构建 Notebook。

**主窗口 `TradeHabitApp`**

```python
class TradeHabitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('交易习惯约束器 MVP')
        self.geometry('1280x800')
        init_db()  # 幂等建库 + 默认设置
        self._build_notebook()
        self.refresh_all()  # 各页刷新
```

**5 个 Tab 类（均继承 `ttk.Frame`）**

- `DashboardTab`：仪表盘
- `StockPoolTab`：股票池
- `TradePlanTab`：计划单
- `TradeLogTab`：交易日志
- `SettingsTab`：设置

**通用组件**

- `class EditableTreeView(ttk.Frame)`：包装 `ttk.Treeview` + 滚动条 + 行选中事件，提供 `set_columns`、`set_rows`、`get_selected_id` 方法
- `class FormDialog(tk.Toplevel)`：表单弹窗基类，提供 `add_entry`、`add_combobox`、`add_text`、`add_date_entry`、`validate_and_get` 方法，子类化各弹窗

#### 4.1 DashboardTab 仪表盘

布局：顶部 5 个统计卡片（`ttk.LabelFrame` 横排），下方灯号大色块（`tk.Canvas` 或带背景色的 `ttk.Label`）。

显示：
- 今日入场次数 = `count_today_entries()`
- 今日已实现盈亏 = `sum_today_pnl()`（红字若负）
- 计划执行率 = `count_today_checklist_all_passed() / count_today_checklist_total()`（无数据时显示 `—`）
- 红灯覆盖次数 = `count_today_red_flag_overrides()`
- 当前红黄绿灯 = `get_traffic_light(...)`（绿/黄/红 背景色 + 文字）

刷新：`refresh()` 重新查询并更新；切换 Tab 时自动调用。

#### 4.2 StockPoolTab 股票池

`EditableTreeView` 列：ID、代码、名称、逻辑、关键位、催化剂、风险、状态、更新时间。

按钮区（`ttk.Frame` 底部）：新增、编辑、删除、刷新、导入 CSV、导入 JSON。

- **新增/编辑**：弹 `StockPoolFormDialog`，状态 `ttk.Combobox` 取值 `['观察', '可交易', '剔除']`（**不含 '待确认'**——该状态仅导入时设置，用户不可手动选）
- **删除**：`messagebox.askyesno` 二次确认，软删（直接 DELETE，无回收站，但记录 `red_flag_event` event_type='delete_stock'，包含 detail=code+name）
- **导入 CSV/JSON**：`filedialog.askopenfilename` 选文件 → 调 `import_stock_pool_*` 解析 → 弹预览框（可全选/反选/取消） → 确认后批量 `insert_stock_pool`，状态 `'待确认'`
- 状态 '待确认' 的行用淡黄背景（`tag_configure`），按钮区额外提供"批量转可交易"按钮，把选中行 status 改为 `'可交易'`

#### 4.3 TradePlanTab 计划单

`EditableTreeView` 列：ID、代码、名称、入场触发、入场区间、止损、时间止损、目标、计划股数、最大亏损、状态。

按钮区：新增、编辑、删除、运行检查清单并入场、记录出场、取消计划、复制条件单提醒、导出 CSV。

- **新增/编辑**：弹 `TradePlanFormDialog`
  - 必填项红 `*` 标记：股票代码、入场触发条件、止损价、时间止损日期、计划股数、最大亏损金额
  - 股票代码旁有"从股票池选择"下拉（仅列出 status='可交易' 的股票，选择后自动填名称）
  - 下方"仓位建议计算器"区：输入入场价上沿 + 止损价（已填）→ 实时调 `suggest_position_size` → 显示建议股数 + 建议最大亏损 + 警告（红字 `ttk.Label`）
  - 点"保存"前调 `validate_plan`，错误用 `messagebox.showerror` 阻断
  - 状态默认 `'草稿'`；保存后可改为 `'待触发'`（用户手动改）
- **运行检查清单并入场**：仅 status='待触发' 的计划可点
  - 弹 `ChecklistDialog`（见 4.3.1）
  - 通过/覆盖后弹出 `EntryDialog` 填实际入场时间/价格/数量/费用 → 生成 trade_log，plan.status='已入场'
- **记录出场**：仅 status='已入场' 的计划可点
  - 弹 `ExitDialog` 填出场时间/价格/费用/是否按计划/偏差原因/情绪标签/情绪强度/备注
  - 调 `calc_pnl` → 写入 trade_log 对应字段 → plan.status='已结束'
- **取消计划**：status 改 `'取消'`，二次确认
- **复制条件单提醒**：调 `build_condition_order_text` → `clipboad_clear()` + `clipboard_append(text)` → `messagebox.showinfo` 提示已复制
- **导出 CSV**：`filedialog.asksaveasfilename` → `export_trade_plans_csv`

##### 4.3.1 ChecklistDialog

`tk.Toplevel` 模态弹窗。显示 8 项检查清单，每行：`[✓/☐]` 复选框 + 标签 + 状态/备注。

- auto=True 的项：复选框禁用（`state='disabled'`），系统自动打勾或不打勾，旁边显示 `[自动]` 标签 + 状态文本（如"股票池可交易"、"今日 1/3 次"）
- auto=False 的项（3、4、5）：复选框可勾，默认不勾，标签旁显示 `[人工]`
- 底部两个按钮：
  - "确认入场"（绿色高亮）：仅当 `all_checklist_passed(items)` 为 True 时 `state='normal'`，否则 `disabled`
  - "覆盖并记录原因"（红色）：永远 `normal`，点击弹 `simpledialog.askstring` 输入覆盖原因（非空校验）→ `insert_red_flag('override_checklist', detail, reason)` → 写 `checklist_run(all_passed=0, override_reason=reason)` → 视为通过
- 关闭窗口（X 或 ESC）= 取消，不入场

##### 4.3.2 EntryDialog

填写实际入场信息：
- 入场时间（默认 now()，可改）
- 入场价（必填，>0）
- 数量（必填，>0，默认=plan.planned_shares）
- 费用（默认 0）

点"确认入场"：调 `insert_trade_log({plan_id, stock_code, stock_name, entry_time, entry_price, shares, fees, followed_plan=1})` → `update_trade_plan(plan_id, {status:'已入场'})` → 关闭 → `messagebox.showinfo("入场已记录")`

##### 4.3.3 ExitDialog

填写出场信息：
- 出场时间（默认 now()）
- 出场价（必填，>0）
- 费用（默认 0）
- 是否按计划（默认 1）
- 偏差原因（仅在"是否按计划=0"时启用，必填）
- 情绪标签（`ttk.Combobox`，取值来自 settings.emotion_tags）
- 情绪强度（`ttk.Spinbox` 1-10）
- 备注（`tk.Text` 多行）

点"确认出场"：先读 trade_log 旧记录拿 entry_price/shares/stop_loss（从 plan 反查） → `calc_pnl` → `update_trade_log(log_id, {exit_time, exit_price, pnl_amount, pnl_r, fees, followed_plan, deviation_reason, emotion_tag, emotion_intensity, notes})` → `update_trade_plan(plan_id, {status:'已结束'})` → 关闭

#### 4.4 TradeLogTab 交易日志

`EditableTreeView` 列：ID、计划ID、代码、名称、入场时间、入场价、数量、出场时间、出场价、盈亏、盈亏R、是否按计划、偏差原因、情绪、强度。

按钮区：编辑（补全出场/情绪/备注）、删除、导出 CSV。

- **编辑**：弹 `ExitDialog` 预填现有值，保存调 `update_trade_log`
- **删除**：二次确认，DELETE；若该 log 关联 plan 处于"已结束"，提示并询问是否回退 plan 状态为"已入场"（默认否，只删 log）
- **导出 CSV**：`filedialog.asksaveasfilename` 默认文件名 `trade_logs_YYYY-MM-DD.csv` → `export_trade_logs_csv`，编码 utf-8-sig

#### 4.5 SettingsTab 设置

表单字段：
- 账户资金（数值）
- 单笔风险比例 %（数值，0-100）
- 单日最大亏损（数值）
- 单日最大交易次数（整数）
- 情绪标签（多行文本，逗号分隔；下方预览解析后列表）

按钮：保存。

- 保存前校验：account_balance>0、risk_pct∈[0,100]、daily_max_loss≥0、daily_max_trades∈[1,100]、emotion_tags 非空
- 保存调 `set_settings(...)` → `messagebox.showinfo("设置已保存，立即生效")` → 触发 `DashboardTab.refresh()` 重算红黄绿灯

### 5. `services/trade_habit/main.py`（新建）

```python
#!/usr/bin/env python3
"""交易习惯约束器 MVP 启动入口。

运行方式：
    cd services/trade_habit
    python main.py

首次启动自动创建 trade_habit.db 与全部表，写入默认设置。
"""
from __future__ import annotations
import sys

from db import init_db
from ui import TradeHabitApp


def main() -> int:
    init_db()  # 幂等：建库、建表、写默认设置
    app = TradeHabitApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### 6. `services/trade_habit/README.md`（新建）

包含：
- 功能简介
- 运行方式：`cd services/trade_habit && python main.py`（不需 `uv run`，因零依赖）
- 数据库位置：`services/trade_habit/trade_habit.db`（首次启动自动生成）
- 5 个 Tab 功能说明
- 检查清单 8 项说明
- 红黄绿灯规则
- 仓位建议公式
- 盈亏R公式
- 操作顺序（用户提供的 11.1-11.9）
- 核心约束（4 条）
- 未实现项与 TODO（如：多账户、自动同步券商流水、AI 分析对接预留）

## 假设与决策

### 决策

1. **DB 路径**：`services/trade_habit/trade_habit.db`（与 `main.py` 同级），理由：应用自我封闭、便于备份/迁移、不污染项目共享 DB
2. **`pyproject.toml`**：声明 `dependencies = []` + `package = false`，不进根 `pyproject.toml` 依赖列表
3. **股票池 status 枚举**：`['观察', '可交易', '剔除', '待确认']`——前 3 个用户可选；`'待确认'` 仅由 CSV/JSON 导入设置，不在 Combobox 出现，需用"批量转可交易"按钮显式确认
4. **计划单 status 流转**：草稿 → 待触发 → 已入场 → 已结束；任何状态可 → 取消（除已结束外，已结束不可取消，只能新增补偿单）
5. **入场即写 trade_log**：trade_log 在确认入场时就创建（exit_* 字段为 NULL），出场时 UPDATE；不拆成两表
6. **红黄绿灯阈值**：亏损用 `<= -上限`（含等号）触发红，避免边界歧义；黄灯用 80% 阈值
7. **检查清单第 8 项"当前无红灯"**：红灯时该自动项 passed=False，强制走覆盖流程
8. **覆盖流程**：必填原因 → 写 `red_flag_event` + `checklist_run(all_passed=0, override_reason=reason)` → 允许继续入场（不阻断）
9. **CSV 导入**：UTF-8 with BOM 优先，回退 GBK；JSON 支持 `[{...}, {...}]` 或 `{pool: [...]}` 两种结构
10. **时间格式**：全部 ISO 字符串 `datetime.now().isoformat(timespec='seconds')`，"今日"判定取 ISO 前 10 字符（`YYYY-MM-DD`）== `date.today().isoformat()`
11. **盈亏R 零风险保护**：`abs(entry - stop_loss) * shares == 0` 时 `pnl_r = 0.0`，避免除零
12. **`planned_shares` vs 建议股数**：用户填入的 `planned_shares` 不强制等于建议值，但若 `max_loss_amount > 单笔风险金额` 在 `validate_plan` 中加红字警告（不阻断保存，但检查清单第 6 项自动不通过）

### 假设

1. **运行环境**：macOS / Linux / Windows 通用 Python 3.11+，Tkinter 已随标准库安装（macOS 默认 Python 自带；若用户用 Homebrew Python 需 `brew install python-tk`，README 注明）
2. **单机单用户**：不考虑并发、多用户、远程访问
3. **无券商 API**：不接券商，所有成交价由用户手动录入
4. **无 AI**：CSV/JSON 导出后用户自行交给 AI 分析
5. **金额单位**：人民币元（CNY），股数最小 100（一手）
6. **csv 模块**：标准库自带，utf-8-sig 编码 Excel 可直接打开
7. **`simpledialog`/`messagebox`/`filedialog`**：均属 tkinter 标准子模块，零外部依赖

## 验证步骤（对照规范第 9 节）

1. `cd services/trade_habit && python main.py` 启动，无 ImportError、无第三方包警告
2. 首次启动后 `ls services/trade_habit/trade_habit.db` 存在；`sqlite3 trade_habit.db ".tables"` 显示 6 张表
3. `sqlite3 trade_habit.db "SELECT * FROM settings;"` 显示 5 条默认设置
4. 股票池 Tab → 新增 600000 浦发银行 → 状态改"可交易" → 列表显示一行
5. 计划单 Tab → 新增计划（stock=600000、entry_trigger=突破10.20且放量、entry_price_high=10.20、stop_loss=9.80、time_stop_date=2026-09-20、planned_shares=1000、max_loss_amount=400）→ 仓位建议区显示"建议股数=1250，建议最大亏损=500"红字警告（500>400，因 risk_per_trade_pct=0.5 → 单笔风险=500）
6. 计划单 → 状态改"待触发" → 点"运行检查清单并入场" → 8 项检查（第 1、2、6、7、8 自动；3、4、5 人工勾）→ 全过 → "确认入场"按钮可点 → 填实际入场 10.18/1000/5 → trade_log 出现新行（exit_* 为空），plan.status="已入场"
7. 计划单 → 选中该计划 → 点"记录出场" → 填 10.30/3/1/平静/3 → 计划 status="已结束"，trade_log 的 exit_price=10.30、pnl_amount=(10.30-10.18)*1000-8=112、pnl_r=112/((10.18-9.80)*1000)=112/380≈0.295
8. 仪表盘 Tab → 今日入场次数=1、已实现盈亏=112、计划执行率=1/1=100%、红灯覆盖=0、绿灯
9. 设置 Tab → daily_max_loss 改为 50 → 保存 → 仪表盘红灯（112>50 但已实现盈亏=112>0 不触发；若改为 -200 则 today_pnl=112 仍为正不触发；需手动构造负盈亏场景测：先 -200 出场 → today_pnl=-200 ≤ -50 → 红灯，"当前无红灯"自动 false）
10. 交易日志 Tab → 导出 CSV → 用 Excel 打开显示正常（utf-8-sig BOM）
11. 关闭程序 → 重新 `python main.py` → 数据仍在
12. 股票池 → 导入 CSV（含 3 行）→ 列表多 3 行 status='待确认'（淡黄背景）→ 全选 → "批量转可交易" → 状态变 '可交易'
13. 红灯场景：daily_max_loss=50，今日已有 -200 盈亏 → 新建计划 → 运行检查清单 → 第 8 项"当前无红灯"自动 false → "确认入场"按钮 disabled → 必须点"覆盖并记录原因" → 填原因 → 入场成功 → red_flag_event 表新增一行
14. README 检查包含：运行方式、DB 位置、5 Tab 功能、清单 8 项、红黄绿灯规则、仓位建议公式、盈亏R公式、操作顺序、核心约束

## 未实现项（在 README "未实现项" 节列出）

1. **多账户支持**：当前 settings 仅一组 account_balance / risk_per_trade_pct
2. **券商 API 自动同步**：所有成交价手动录入
3. **AI 分析自动接入**：仅提供 CSV 导出，后续可交给 AI 处理
4. **历史回测**：trade_log 仅记录实盘，不含回测标记
5. **多周期统计**：仪表盘仅显示今日，未实现周/月统计
6. **止损执行率单独统计**：当前以"是否按计划"间接体现，未单独拆分"是否触发止损价出场"指标
7. **跨日持仓跟踪**：plan 与 trade_log 是 1:1，未实现加仓/分批出场
8. **数据备份/恢复 UI**：用户可手动复制 `trade_habit.db` 文件备份
