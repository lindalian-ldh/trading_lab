"""交易习惯约束器 MVP Tkinter 界面层。

零第三方依赖，仅用 tkinter / ttk / messagebox / filedialog / simpledialog。

主窗口 TradeHabitApp 使用 ttk.Notebook 分 5 页：
    1. 仪表盘 DashboardTab
    2. 股票池 StockPoolTab
    3. 计划单 TradePlanTab
    4. 交易日志 TradeLogTab
    5. 设置 SettingsTab

各业务弹窗：
    - StockPoolFormDialog：股票池新增/编辑
    - TradePlanFormDialog：计划单新增/编辑（含仓位建议计算器）
    - ChecklistDialog：检查清单 8 项 + 确认入场/覆盖
    - EntryDialog：填写实际入场信息
    - ExitDialog：填写出场信息
    - ImportPreviewDialog：导入预览（CSV/JSON）
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog, ttk
from typing import Callable, Optional

import db
import services


# ====================================================================
# 通用：表格组件
# ====================================================================

class EditableTreeView(ttk.Frame):
    """ttk.Treeview + 滚动条 + 选中事件。"""

    def __init__(self, master, columns: list[tuple[str, str, int]],
                 on_select: Optional[Callable[[int], None]] = None):
        """
        Args:
            columns: [(col_id, title, width), ...]
            on_select: 选中行回调，参数为行 id（int）或 None
        """
        super().__init__(master)
        self._on_select = on_select
        self.tree = ttk.Treeview(
            self,
            columns=[c[0] for c in columns],
            show="headings",
            selectmode="browse",
        )
        for col_id, title, width in columns:
            self.tree.heading(col_id, text=title)
            self.tree.column(col_id, width=width, anchor="w")
        vsb = ttk.Scrollbar(self, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal",
                            command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set,
                            xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._handle_select)

    def _handle_select(self, _evt):
        if self._on_select:
            sel = self.tree.selection()
            if not sel:
                self._on_select(None)
                return
            values = self.tree.item(sel[0], "values")
            try:
                self._on_select(int(values[0]))
            except (ValueError, IndexError):
                self._on_select(None)

    def set_rows(self, rows: list[list], tagger: Optional[Callable[[list, dict], str]] = None):
        """清空并重设数据行。每行第一列必须是 id。

        Args:
            rows: list[list] 每行 cells
            tagger: (row_values, item_kwargs) -> tag_name，用于行级样式
        """
        # 清空
        for it in self.tree.get_children():
            self.tree.delete(it)
        for r in rows:
            if not r:
                continue
            tag = None
            kwargs: dict = {}
            if tagger:
                tag = tagger(r, kwargs)
            tags = (tag,) if tag else ()
            self.tree.insert("", "end", values=r, tags=tags, **kwargs)

    def get_selected_id(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        values = self.tree.item(sel[0], "values")
        try:
            return int(values[0])
        except (ValueError, IndexError):
            return None

    def tag_configure(self, name, **kwargs):
        self.tree.tag_configure(name, **kwargs)


# ====================================================================
# 通用：表单弹窗基类
# ====================================================================

class FormDialog(tk.Toplevel):
    """表单弹窗基类。提供字段管理、布局、校验、提交。"""

    def __init__(self, master, title: str, width: int = 480):
        super().__init__(master)
        self.title(title)
        self.geometry(f"{width}x600")
        self.transient(master)
        self.grab_set()
        self._result: Optional[dict] = None
        self._fields: dict[str, tk.Widget] = {}
        self._labels: dict[str, ttk.Label] = {}
        self._row = 0
        # 主体 frame
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=12, pady=8)
        # 底部按钮区
        self.btn_frame = ttk.Frame(self)
        self.btn_frame.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(self.btn_frame, text="取消", command=self.destroy
                   ).pack(side="right", padx=(4, 0))
        ttk.Button(self.btn_frame, text="保存",
                   command=self._on_save).pack(side="right")

    def add_entry(self, key: str, label: str, default: str = "",
                 required: bool = False) -> ttk.Entry:
        lbl_text = (f"{label} *" if required else label)
        lbl = ttk.Label(self.body, text=lbl_text)
        lbl.grid(row=self._row, column=0, sticky="w", pady=4)
        ent = ttk.Entry(self.body, width=40)
        ent.insert(0, str(default) if default else "")
        ent.grid(row=self._row, column=1, sticky="ew", pady=4, padx=(8, 0))
        self.body.grid_columnconfigure(1, weight=1)
        self._fields[key] = ent
        self._labels[key] = lbl
        self._row += 1
        return ent

    def add_combobox(self, key: str, label: str, values: list[str],
                     default: str = "", required: bool = False,
                     state: str = "readonly") -> ttk.Combobox:
        lbl_text = (f"{label} *" if required else label)
        lbl = ttk.Label(self.body, text=lbl_text)
        lbl.grid(row=self._row, column=0, sticky="w", pady=4)
        cb = ttk.Combobox(self.body, values=values, state=state)
        if default:
            cb.set(default)
        cb.grid(row=self._row, column=1, sticky="ew", pady=4, padx=(8, 0))
        self.body.grid_columnconfigure(1, weight=1)
        self._fields[key] = cb
        self._labels[key] = lbl
        self._row += 1
        return cb

    def add_text(self, key: str, label: str, default: str = "",
                 height: int = 3) -> tk.Text:
        lbl = ttk.Label(self.body, text=label)
        lbl.grid(row=self._row, column=0, sticky="nw", pady=4)
        txt = tk.Text(self.body, width=40, height=height, wrap="word")
        if default:
            txt.insert("1.0", default)
        txt.grid(row=self._row, column=1, sticky="nsew", pady=4, padx=(8, 0))
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_rowconfigure(self._row, weight=1)
        self._fields[key] = txt
        self._labels[key] = lbl
        self._row += 1
        return txt

    def add_spinbox(self, key: str, label: str, frm: int, to: int,
                    default: int = 0) -> ttk.Spinbox:
        lbl = ttk.Label(self.body, text=label)
        lbl.grid(row=self._row, column=0, sticky="w", pady=4)
        sp = ttk.Spinbox(self.body, from_=frm, to=to,
                         width=10, state="readonly")
        sp.delete(0, "end")
        sp.insert(0, str(default))
        sp.grid(row=self._row, column=1, sticky="w", pady=4, padx=(8, 0))
        self._fields[key] = sp
        self._labels[key] = lbl
        self._row += 1
        return sp

    def add_separator(self):
        sep = ttk.Separator(self.body, orient="horizontal")
        sep.grid(row=self._row, column=0, columnspan=2,
                 sticky="ew", pady=(8, 4))
        self._row += 1

    def add_label(self, key: str, text: str) -> ttk.Label:
        lbl = ttk.Label(self.body, text=text)
        lbl.grid(row=self._row, column=0, columnspan=2,
                 sticky="w", pady=2)
        self._labels[key] = lbl
        self._row += 1
        return lbl

    def get_value(self, key: str) -> str:
        w = self._fields.get(key)
        if w is None:
            return ""
        if isinstance(w, tk.Text):
            return w.get("1.0", "end").rstrip("\n")
        return w.get().strip()

    def set_label(self, key: str, text: str, foreground: str = ""):
        lbl = self._labels.get(key)
        if lbl is None:
            return
        lbl.config(text=text)
        if foreground:
            lbl.config(foreground=foreground)

    def _on_save(self):
        # 子类 override validate_and_get
        result = self.validate_and_get()
        if result is None:
            return
        self._result = result
        self.destroy()

    def validate_and_get(self) -> Optional[dict]:
        return {}

    def show(self) -> Optional[dict]:
        self.wait_window()
        return self._result


# ====================================================================
# 弹窗 1：股票池新增/编辑
# ====================================================================

class StockPoolFormDialog(FormDialog):
    def __init__(self, master, existing: Optional[dict] = None):
        title = "编辑股票" if existing else "新增股票"
        super().__init__(master, title=title, width=520)
        self._existing = existing
        d = existing or {}
        self.add_entry("code", "代码", default=d.get("code", ""),
                       required=True)
        self.add_entry("name", "名称", default=d.get("name", ""))
        self.add_entry("logic", "选股逻辑", default=d.get("logic", ""))
        self.add_entry("key_level", "关键位", default=d.get("key_level", ""))
        self.add_entry("catalyst", "催化剂", default=d.get("catalyst", ""))
        self.add_text("risk", "风险", default=d.get("risk", ""), height=2)
        self.add_combobox(
            "status", "状态",
            values=["观察", "可交易", "剔除"],
            default=d.get("status", "观察"),
            required=True,
        )

    def validate_and_get(self) -> Optional[dict]:
        code = self.get_value("code")
        if not code:
            messagebox.showerror("错误", "股票代码不能为空",
                                 parent=self)
            return None
        status = self.get_value("status")
        if status not in ("观察", "可交易", "剔除"):
            messagebox.showerror("错误",
                                 "状态必须为「观察/可交易/剔除」之一",
                                 parent=self)
            return None
        return {
            "code": code,
            "name": self.get_value("name"),
            "logic": self.get_value("logic"),
            "key_level": self.get_value("key_level"),
            "catalyst": self.get_value("catalyst"),
            "risk": self.get_value("risk"),
            "status": status,
        }


# ====================================================================
# 弹窗 2：计划单新增/编辑
# ====================================================================

class TradePlanFormDialog(FormDialog):
    def __init__(self, master, existing: Optional[dict] = None):
        title = "编辑计划单" if existing else "新增计划单"
        super().__init__(master, title=title, width=580)
        self._existing = existing
        d = existing or {}

        # 股票池下拉（仅可交易的；这是检查清单第 1 项的硬约束）
        tradable = db.list_stock_pool(status_filter="可交易")
        self._stock_options = {
            f"{s['code']} {s['name']}": s for s in tradable
        }
        # 默认选中
        default_stock_key = ""
        if d.get("stock_code"):
            for k, s in self._stock_options.items():
                if s["code"] == d["stock_code"]:
                    default_stock_key = k
                    break
        self.add_combobox(
            "stock_pick", "从股票池选择", sorted(self._stock_options.keys()),
            default=default_stock_key,
        )
        # 下拉框为空时的提示标签
        if self._stock_options:
            self.add_label(
                "stock_pick_hint",
                f"共 {len(self._stock_options)} 只可交易股票可选；"
                "选好后自动填充代码与名称。",
            )
        else:
            self.add_label(
                "stock_pick_hint",
                "⚠️ 下拉框只显示状态为「可交易」的股票。请先到「股票池」"
                "页面把目标股票状态改为「可交易」（选中行后点底部"
                "「转可交易」按钮，或在编辑里手动改状态）。",
            )
            # 用红色突出
            self._labels["stock_pick_hint"].config(foreground="red")
        self.add_entry("stock_code", "股票代码 *",
                       default=d.get("stock_code", ""))
        self.add_entry("stock_name", "股票名称",
                       default=d.get("stock_name", ""))
        self.add_entry("entry_trigger", "入场触发条件 *",
                       default=d.get("entry_trigger", "") or "")
        self.add_entry("entry_price_low", "入场价下沿",
                       default=d.get("entry_price_low", "") or "")
        self.add_entry("entry_price_high", "入场价上沿",
                       default=d.get("entry_price_high", "") or "")
        self.add_entry("stop_loss", "止损价 *",
                       default=d.get("stop_loss", "") or "")
        self.add_entry("time_stop_date", "时间止损日期 *",
                       default=d.get("time_stop_date", "") or "")
        self.add_entry("target_price", "目标价",
                       default=d.get("target_price", "") or "")
        self.add_entry("planned_shares", "计划股数 *",
                       default=d.get("planned_shares", "") or "")
        self.add_entry("max_loss_amount", "最大亏损金额 *",
                       default=d.get("max_loss_amount", "") or "")
        self.add_text("invalidation", "失效条件",
                      default=d.get("invalidation", "") or "", height=2)

        # 股票池选择联动
        cb = self._fields["stock_pick"]
        cb.bind("<<ComboboxSelected>>", self._on_stock_pick)

        # 仓位建议计算器
        self.add_separator()
        self.add_label("hint_section", "▼ 仓位建议计算器（实时）")
        self.add_label("hint_shares", "建议股数：—")
        self.add_label("hint_max_loss", "建议最大亏损：—")
        self.add_label("hint_warning", "")
        # 绑定入场价上沿/止损价变化
        for key in ("entry_price_high", "stop_loss"):
            w = self._fields[key]
            w.bind("<KeyRelease>", self._recalc_suggest)
            w.bind("<FocusOut>", self._recalc_suggest)
        # 初始化建议
        self._recalc_suggest()

        # 从分析报告填充
        self.add_separator()
        self.add_label("report_section",
                       "▼ 从分析报告填充（粘贴文本自动提取价格/止损/目标/触发条件）")
        report_btn_row = self._row
        self._row += 1
        btn_frame = ttk.Frame(self.body)
        btn_frame.grid(row=report_btn_row, column=0, columnspan=2,
                       sticky="w", pady=4)
        ttk.Button(btn_frame, text="📋 粘贴分析报告并提取",
                   command=self._on_import_report).pack(side="left", padx=2)
        self._report_status = ttk.Label(btn_frame, text="", foreground="green")
        self._report_status.pack(side="left", padx=8)

        # 状态：用户可选值 = 草稿 / 待触发 / 取消
        # 「已入场」「已结束」是系统状态，由检查清单入场/记录出场流程自动设定
        self.add_separator()
        user_selectable_statuses = ["草稿", "待触发", "取消"]
        cur_status = d.get("status", "草稿") if existing else "草稿"
        if cur_status in ("已入场", "已结束"):
            # 编辑已有记录时，若当前状态已是系统状态，显示为只读 Label
            self.add_label(
                "status_readonly",
                f"当前状态：{cur_status}（系统设定，不可手动修改）",
            )
        else:
            self.add_combobox(
                "status", "状态",
                values=user_selectable_statuses,
                default=cur_status,
            )

    def _on_stock_pick(self, _evt):
        key = self.get_value("stock_pick")
        s = self._stock_options.get(key)
        if not s:
            return
        self._fields["stock_code"].delete(0, "end")
        self._fields["stock_code"].insert(0, s["code"])
        self._fields["stock_name"].delete(0, "end")
        self._fields["stock_name"].insert(0, s.get("name", ""))

    def _recalc_suggest(self, _evt=None):
        settings = db.get_settings()
        try:
            balance = float(settings.get("account_balance", "0"))
        except ValueError:
            balance = 0.0
        try:
            risk_pct = float(settings.get("risk_per_trade_pct", "0"))
        except ValueError:
            risk_pct = 0.0
        try:
            eh = float(self.get_value("entry_price_high") or 0)
        except ValueError:
            eh = 0.0
        try:
            sl = float(self.get_value("stop_loss") or 0)
        except ValueError:
            sl = 0.0
        if balance <= 0 or risk_pct < 0 or eh <= 0 or sl <= 0:
            self.set_label("hint_shares", "建议股数：—（请填写入场价上沿与止损价）")
            self.set_label("hint_max_loss", "建议最大亏损：—")
            self.set_label("hint_warning", "")
            return
        shares, max_loss, warn = services.suggest_position_size(
            balance, risk_pct, eh, sl
        )
        self.set_label("hint_shares", f"建议股数：{shares}")
        self.set_label("hint_max_loss", f"建议最大亏损：{max_loss:.2f}")
        if warn:
            self.set_label("hint_warning", f"⚠️ {warn}", foreground="red")
        else:
            self.set_label("hint_warning", "✓ 仓位未超单笔风险上限",
                          foreground="green")

    def _on_import_report(self):
        """弹出多行文本框，粘贴分析报告 → 提取 → 自动填表。"""
        dlg = tk.Toplevel(self)
        dlg.title("粘贴分析报告")
        dlg.geometry("720x520")
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg,
                  text="粘贴你的分析报告（含 维度A/B/C、支撑位、压力位等），"
                       "点「提取并填充」自动填入表单。"
                  ).pack(anchor="w", padx=12, pady=(8, 4))
        text_widget = tk.Text(dlg, wrap="word", font=("Menlo", 11))
        text_widget.pack(fill="both", expand=True, padx=12, pady=4)
        # 尝试从剪贴板预填
        try:
            clip = self.clipboard_get()
            if clip and ("\n" in clip or "维度" in clip
                         or "支撑位" in clip):
                text_widget.insert("1.0", clip)
        except Exception:
            pass
        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        status_lbl = ttk.Label(btns, text="", foreground="green")
        status_lbl.pack(side="left", padx=8)
        ttk.Button(btns, text="取消", command=dlg.destroy
                   ).pack(side="right", padx=4)
        def on_extract():
            raw = text_widget.get("1.0", "end").rstrip("\n")
            if not raw.strip():
                status_lbl.config(text="⚠️ 文本为空", foreground="red")
                return
            parsed = services.parse_analysis_report(raw)
            if not parsed:
                status_lbl.config(
                    text="⚠️ 未识别到任何字段，请检查报告格式",
                    foreground="red",
                )
                return
            dlg.destroy()
            self._apply_parsed_report(parsed)
        ttk.Button(btns, text="提取并填充",
                   command=on_extract).pack(side="right", padx=4)

    def _apply_parsed_report(self, parsed: dict):
        """把解析结果填入表单对应字段。已有值会被覆盖。"""
        # 数值字段
        field_map = {
            "entry_price_low": ("entry_price_low", "入场价下沿"),
            "entry_price_high": ("entry_price_high", "入场价上沿"),
            "stop_loss": ("stop_loss", "止损价"),
            "target_price": ("target_price", "目标价"),
        }
        filled: list[str] = []
        for k, (field_key, label) in field_map.items():
            v = parsed.get(k)
            if v is None:
                continue
            w = self._fields.get(field_key)
            if w is None:
                continue
            w.delete(0, "end")
            w.insert(0, str(v))
            filled.append(f"{label}={v}")
        # 文本字段
        if parsed.get("entry_trigger"):
            w = self._fields.get("entry_trigger")
            if w is not None:
                # 仅当用户当前为空时覆盖；非空时追加
                cur = w.get().strip()
                new_text = parsed["entry_trigger"]
                if cur:
                    w.delete(0, "end")
                    w.insert(0, f"{cur}；{new_text}")
                else:
                    w.delete(0, "end")
                    w.insert(0, new_text)
                filled.append("入场触发条件已填")
        if parsed.get("invalidation"):
            w = self._fields.get("invalidation")
            if w is not None:
                cur = w.get("1.0", "end").rstrip("\n")
                new_text = parsed["invalidation"]
                if cur:
                    w.delete("1.0", "end")
                    w.insert("1.0", f"{cur}\n{new_text}")
                else:
                    w.delete("1.0", "end")
                    w.insert("1.0", new_text)
                filled.append("失效条件已填")
        # 重新计算仓位建议
        self._recalc_suggest()
        # 状态提示
        rr = parsed.get("rr_ratio")
        end_date = parsed.get("data_end_date")
        status_parts = list(filled)
        if rr:
            status_parts.append(f"盈亏比={rr}:1")
        if end_date:
            status_parts.append(f"数据截止={end_date}")
        self._report_status.config(
            text="✓ 已填：" + "；".join(status_parts)
                 if status_parts else "⚠️ 未识别到可填字段",
            foreground="green" if status_parts else "red",
        )

    def validate_and_get(self) -> Optional[dict]:
        data = {
            "stock_code": self.get_value("stock_code"),
            "stock_name": self.get_value("stock_name"),
            "entry_trigger": self.get_value("entry_trigger"),
            "entry_price_low": self.get_value("entry_price_low"),
            "entry_price_high": self.get_value("entry_price_high"),
            "stop_loss": self.get_value("stop_loss"),
            "time_stop_date": self.get_value("time_stop_date"),
            "target_price": self.get_value("target_price"),
            "planned_shares": self.get_value("planned_shares"),
            "max_loss_amount": self.get_value("max_loss_amount"),
            "invalidation": self.get_value("invalidation"),
            # status 可能不在 _fields（走了只读 Label 分支）
            "status": self.get_value("status") or (
                self._existing.get("status") if self._existing else "草稿"
            ) or "草稿",
        }
        # 数值字段转 float（空字符串保留 None）
        for k in ("entry_price_low", "entry_price_high", "stop_loss",
                  "target_price", "max_loss_amount"):
            v = data[k]
            if v == "":
                data[k] = None
            else:
                try:
                    data[k] = float(v)
                except ValueError:
                    messagebox.showerror("错误",
                                         f"{k} 不是有效数字", parent=self)
                    return None
        if data["planned_shares"]:
            try:
                data["planned_shares"] = int(float(data["planned_shares"]))
            except ValueError:
                messagebox.showerror("错误", "计划股数不是有效整数",
                                     parent=self)
                return None
        else:
            data["planned_shares"] = None
        errs = services.validate_plan(data)
        if errs:
            messagebox.showerror("校验失败", "\n".join(errs), parent=self)
            return None
        return data


# ====================================================================
# 弹窗 3：检查清单
# ====================================================================

class ChecklistDialog(tk.Toplevel):
    """8 项检查清单弹窗。返回 dict：{action: 'enter'|'override'|'cancel', override_reason: ''}"""

    def __init__(self, master, items: list[dict], plan: dict):
        super().__init__(master)
        self.title("检查清单")
        self.geometry("640x540")
        self.transient(master)
        self.grab_set()
        self._result: Optional[dict] = None
        self._items = items
        self._plan = plan
        self._checks: dict[str, tk.BooleanVar] = {}
        self._notes: dict[str, ttk.Label] = {}

        ttk.Label(self, text=f"计划 #{plan.get('id')} "
                  f"{plan.get('stock_code','')} {plan.get('stock_name','')}"
                  ).pack(anchor="w", padx=12, pady=(8, 4))
        ttk.Separator(self, orient="horizontal").pack(
            fill="x", padx=12, pady=4)

        form = ttk.Frame(self)
        form.pack(fill="both", expand=True, padx=12, pady=4)

        headers = ttk.Frame(form)
        headers.pack(fill="x")
        ttk.Label(headers, text="✓", width=4).grid(row=0, column=0)
        ttk.Label(headers, text="检查项", width=24).grid(row=0, column=1)
        ttk.Label(headers, text="类型").grid(row=0, column=2)
        ttk.Label(headers, text="状态/备注").grid(row=0, column=3)

        rows_frame = ttk.Frame(form)
        rows_frame.pack(fill="both", expand=True)
        for i, it in enumerate(items, start=1):
            var = tk.BooleanVar(value=bool(it.get("passed")))
            self._checks[it["key"]] = var
            cb = ttk.Checkbutton(rows_frame, variable=var)
            cb.grid(row=i, column=0, padx=2, pady=2)
            if it.get("auto"):
                cb.configure(state="disabled")
            ttk.Label(rows_frame, text=it["label"], width=24,
                     wraplength=180, anchor="w").grid(row=i, column=1,
                                                       sticky="w")
            type_lbl = "自动" if it.get("auto") else "人工"
            ttk.Label(rows_frame, text=type_lbl, width=4).grid(
                row=i, column=2, padx=4)
            note = ttk.Label(rows_frame, text=it.get("note", ""),
                            wraplength=320, anchor="w")
            note.grid(row=i, column=3, sticky="w", padx=2)
            self._notes[it["key"]] = note

        # 底部按钮
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(4, 8))
        self.btn_enter = ttk.Button(btns, text="确认入场",
                                    command=self._on_enter,
                                    state="normal")
        self.btn_enter.pack(side="right", padx=(4, 0))
        self.btn_override = ttk.Button(
            btns, text="覆盖并记录原因",
            command=self._on_override
        )
        self.btn_override.pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="取消", command=self._on_cancel).pack(
            side="right")

        self._refresh_enter_state()
        # 监听人工勾选变化
        for var in self._checks.values():
            var.trace_add("write", lambda *a: self._refresh_enter_state())

    def _refresh_enter_state(self):
        all_passed = all(
            v.get() for v in self._checks.values()
        )
        self.btn_enter.configure(
            state="normal" if all_passed else "disabled"
        )

    def _on_enter(self):
        # 重新构建 items（人工项的 passed 用当前勾选状态）
        for it in self._items:
            if not it.get("auto"):
                it["passed"] = bool(self._checks[it["key"]].get())
        if not services.all_checklist_passed(self._items):
            messagebox.showerror("错误",
                                 "还有未通过项，请勾选或走覆盖流程",
                                 parent=self)
            return
        self._result = {"action": "enter", "override_reason": "",
                        "items": self._items}
        self.destroy()

    def _on_override(self):
        reason = simpledialog.askstring(
            "覆盖原因",
            "请输入覆盖原因（必填）：",
            parent=self,
        )
        if reason is None:
            return
        reason = reason.strip()
        if not reason:
            messagebox.showerror("错误", "覆盖原因不能为空",
                                 parent=self)
            return
        # 重新构建 items
        for it in self._items:
            if not it.get("auto"):
                it["passed"] = bool(self._checks[it["key"]].get())
        self._result = {
            "action": "override",
            "override_reason": reason,
            "items": self._items,
        }
        self.destroy()

    def _on_cancel(self):
        self._result = {"action": "cancel", "override_reason": "",
                        "items": self._items}
        self.destroy()

    def show(self) -> Optional[dict]:
        self.wait_window()
        return self._result


# ====================================================================
# 弹窗 4：实际入场
# ====================================================================

class EntryDialog(FormDialog):
    def __init__(self, master, plan: dict):
        super().__init__(master, title="实际入场信息", width=440)
        self._plan = plan
        self.add_entry("entry_time", "入场时间",
                       default=services.now_iso(), required=True)
        self.add_entry("entry_price", "实际入场价",
                       default=str(plan.get("entry_price_high") or ""),
                       required=True)
        self.add_entry("shares", "实际数量",
                       default=str(plan.get("planned_shares") or ""),
                       required=True)
        self.add_entry("fees", "费用", default="0")

    def validate_and_get(self) -> Optional[dict]:
        time = self.get_value("entry_time")
        if not time:
            messagebox.showerror("错误", "入场时间必填", parent=self)
            return None
        try:
            ep = float(self.get_value("entry_price"))
            if ep <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "实际入场价必须为正数",
                                 parent=self)
            return None
        try:
            sh = int(float(self.get_value("shares")))
            if sh <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "实际数量必须为正整数",
                                 parent=self)
            return None
        try:
            fees = float(self.get_value("fees") or 0)
        except ValueError:
            messagebox.showerror("错误", "费用不是有效数字",
                                 parent=self)
            return None
        return {
            "entry_time": time,
            "entry_price": ep,
            "shares": sh,
            "fees": fees,
        }


# ====================================================================
# 弹窗 5：出场
# ====================================================================

class ExitDialog(FormDialog):
    def __init__(self, master, plan: dict, log: Optional[dict] = None):
        super().__init__(master, title="记录出场", width=520)
        self._plan = plan
        self._log = log
        d = log or {}
        self.add_entry("exit_time", "出场时间",
                       default=d.get("exit_time") or services.now_iso(),
                       required=True)
        self.add_entry("exit_price", "出场价",
                       default=str(d.get("exit_price") or ""),
                       required=True)
        self.add_entry("fees", "费用",
                       default=str(d.get("fees") or "0"))
        self.add_combobox("followed_plan", "是否按计划",
                          values=["1 是", "0 否"],
                          default=("1 是" if d.get("followed_plan", 1)
                                   else "0 否"))
        self.add_entry("deviation_reason", "偏差原因",
                       default=d.get("deviation_reason", "") or "")
        # 情绪标签
        tags_str = db.get_setting("emotion_tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        self.add_combobox("emotion_tag", "情绪",
                          values=tags or ["无"],
                          default=d.get("emotion_tag", "") or "")
        self.add_spinbox("emotion_intensity", "情绪强度", 1, 10,
                         default=d.get("emotion_intensity") or 5)
        self.add_text("notes", "备注",
                      default=d.get("notes", "") or "", height=3)
        # 是否按计划联动偏差原因
        cb = self._fields["followed_plan"]
        cb.bind("<<ComboboxSelected>>", self._refresh_deviation)
        self._refresh_deviation()

    def _refresh_deviation(self, _evt=None):
        fp = self.get_value("followed_plan")
        if fp.startswith("0"):
            self.set_label_required_visual("deviation_reason", required=True)
        else:
            self.set_label_required_visual("deviation_reason", required=False)

    def set_label_required_visual(self, key: str, required: bool):
        """重写 Label 文本加 *。"""
        # 简化：只更新偏差原因
        lbl = self._labels.get(key)
        if lbl is None:
            return
        base = "偏差原因"
        lbl.config(text=f"{base} *" if required else base)

    def validate_and_get(self) -> Optional[dict]:
        time = self.get_value("exit_time")
        if not time:
            messagebox.showerror("错误", "出场时间必填", parent=self)
            return None
        try:
            ep = float(self.get_value("exit_price"))
            if ep <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "出场价必须为正数",
                                 parent=self)
            return None
        try:
            fees = float(self.get_value("fees") or 0)
        except ValueError:
            messagebox.showerror("错误", "费用不是有效数字",
                                 parent=self)
            return None
        fp_str = self.get_value("followed_plan")
        followed = 1 if fp_str.startswith("1") else 0
        dev = self.get_value("deviation_reason")
        if not followed and not dev:
            messagebox.showerror("错误",
                                 "未按计划出场时偏差原因必填",
                                 parent=self)
            return None
        try:
            intensity = int(float(self.get_value("emotion_intensity")))
            if not (1 <= intensity <= 10):
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "情绪强度需 1-10",
                                 parent=self)
            return None
        return {
            "exit_time": time,
            "exit_price": ep,
            "fees": fees,
            "followed_plan": followed,
            "deviation_reason": dev,
            "emotion_tag": self.get_value("emotion_tag"),
            "emotion_intensity": intensity,
            "notes": self.get_value("notes"),
        }


# ====================================================================
# 弹窗 6：导入预览
# ====================================================================

class ImportPreviewDialog(tk.Toplevel):
    """导入股票池预览。返回选中的行 dict 列表。"""

    def __init__(self, master, rows: list[dict]):
        super().__init__(master)
        self.title("导入预览")
        self.geometry("720x460")
        self.transient(master)
        self.grab_set()
        self._result: list[dict] = []
        self._rows = rows

        ttk.Label(self,
                  text=f"共 {len(rows)} 条候选，状态默认「待确认」；"
                       "勾选要导入的行，确认后写入股票池。"
                  ).pack(anchor="w", padx=12, pady=(8, 4))

        cols = [("sel", "选", 40),
                ("code", "代码", 80),
                ("name", "名称", 100),
                ("logic", "逻辑", 160),
                ("key_level", "关键位", 100),
                ("catalyst", "催化剂", 100),
                ("risk", "风险", 100)]
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in cols],
            show="headings",
        )
        for cid, title, w in cols:
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self._vars: list[tk.BooleanVar] = []
        for r in rows:
            var = tk.BooleanVar(value=True)
            self._vars.append(var)
            item_id = self.tree.insert(
                "", "end",
                values=("✓" if var.get() else "☐",
                        r.get("code", ""), r.get("name", ""),
                        r.get("logic", ""), r.get("key_level", ""),
                        r.get("catalyst", ""), r.get("risk", "")),
            )
            # 绑定点击事件
            self.tree.tag_bind(item_id, "<Double-1>",
                               lambda e, v=var, iid=item_id: self._toggle(v, iid))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(4, 8))
        ttk.Button(btns, text="全选",
                   command=self._select_all).pack(side="left", padx=4)
        ttk.Button(btns, text="全不选",
                   command=self._select_none).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self._on_cancel).pack(
            side="right", padx=4)
        ttk.Button(btns, text="确认导入",
                   command=self._on_confirm).pack(side="right", padx=4)

    def _toggle(self, var: tk.BooleanVar, item_id: str):
        var.set(not var.get())
        new_text = "✓" if var.get() else "☐"
        values = list(self.tree.item(item_id, "values"))
        values[0] = new_text
        self.tree.item(item_id, values=values)

    def _select_all(self):
        for v, iid in zip(self._vars, self.tree.get_children()):
            v.set(True)
            values = list(self.tree.item(iid, "values"))
            values[0] = "✓"
            self.tree.item(iid, values=values)

    def _select_none(self):
        for v, iid in zip(self._vars, self.tree.get_children()):
            v.set(False)
            values = list(self.tree.item(iid, "values"))
            values[0] = "☐"
            self.tree.item(iid, values=values)

    def _on_confirm(self):
        self._result = [r for r, v in zip(self._rows, self._vars) if v.get()]
        self.destroy()

    def _on_cancel(self):
        self._result = []
        self.destroy()

    def show(self) -> list[dict]:
        self.wait_window()
        return self._result


# ====================================================================
# Tab 1：仪表盘
# ====================================================================

class DashboardTab(ttk.Frame):
    def __init__(self, master, app: "TradeHabitApp"):
        super().__init__(master)
        self._app = app
        ttk.Label(self, text="今日仪表盘",
                  font=("", 14, "bold")).pack(anchor="w", padx=12, pady=(8, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", padx=12, pady=4)
        self._card_entries = self._make_card(cards, "今日入场次数", "0", 0)
        self._card_pnl = self._make_card(cards, "今日已实现盈亏", "0", 1)
        self._card_rate = self._make_card(cards, "计划执行率", "—", 2)
        self._card_red = self._make_card(cards, "红灯覆盖次数", "0", 3)

        # 灯号
        light_frame = ttk.LabelFrame(self, text="当前红黄绿灯")
        light_frame.pack(fill="both", expand=True, padx=12, pady=8)
        self._light_canvas = tk.Canvas(light_frame, height=160,
                                       bg="#f5f5f5", highlightthickness=0)
        self._light_canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self._light_text_id = None

    def _make_card(self, parent, title: str, value: str,
                   col: int) -> ttk.Label:
        frame = ttk.LabelFrame(parent, text=title, width=220)
        frame.grid(row=0, column=col, padx=6, pady=4, sticky="nsew")
        parent.grid_columnconfigure(col, weight=1)
        lbl = ttk.Label(frame, text=value, font=("", 18, "bold"),
                        anchor="center")
        lbl.pack(fill="both", expand=True, padx=8, pady=12)
        return lbl

    def refresh(self):
        settings = db.get_settings()
        entries = db.count_today_entries()
        pnl = db.sum_today_pnl()
        passed = db.count_today_checklist_all_passed()
        total = db.count_today_checklist_total()
        red = db.count_today_red_flag_overrides()
        self._card_entries.config(text=str(entries))
        pnl_text = f"{pnl:.2f}"
        self._card_pnl.config(
            text=pnl_text,
            foreground=("red" if pnl < 0 else
                        "green" if pnl > 0 else "black"),
        )
        rate_text = (
            f"{passed / total * 100:.0f}%" if total > 0 else "—"
        )
        self._card_rate.config(text=rate_text)
        self._card_red.config(text=str(red))
        # 灯号
        color, reason = services.get_traffic_light(settings, pnl, entries)
        color_map = {
            "green": "#3ec07d",
            "yellow": "#f0ad4e",
            "red": "#d9534f",
        }
        text_map = {"green": "绿灯：正常",
                    "yellow": "黄灯：警示",
                    "red": "红灯：禁止入场"}
        bg = color_map.get(color, "#cccccc")
        text = f"{text_map.get(color, '')}\n\n{reason}" if reason else text_map.get(color, "")
        self._light_canvas.delete("all")
        w = self._light_canvas.winfo_width() or 600
        h = self._light_canvas.winfo_height() or 160
        # 大圆
        r = min(h // 2 - 20, 60)
        cx, cy = 80, h // 2
        self._light_canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=bg, outline="black", width=2,
        )
        # 文字
        self._light_canvas.create_text(
            cx + r + 30, cy, anchor="w",
            text=text, font=("", 12, "bold"),
            fill=bg,
        )


# ====================================================================
# Tab 2：股票池
# ====================================================================

class StockPoolTab(ttk.Frame):
    def __init__(self, master, app: "TradeHabitApp"):
        super().__init__(master)
        self._app = app
        ttk.Label(self, text="股票池",
                  font=("", 14, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        self._table = EditableTreeView(
            self,
            columns=[
                ("id", "ID", 50),
                ("code", "代码", 80),
                ("name", "名称", 100),
                ("logic", "逻辑", 180),
                ("key_level", "关键位", 100),
                ("catalyst", "催化剂", 100),
                ("risk", "风险", 100),
                ("status", "状态", 80),
                ("updated_at", "更新时间", 160),
            ],
            on_select=self._on_select,
        )
        self._table.pack(fill="both", expand=True, padx=12, pady=4)
        # '待确认' 行高亮
        self._table.tag_configure("pending", background="#fff7d6")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(btns, text="新增", command=self._on_add).pack(side="left", padx=2)
        ttk.Button(btns, text="编辑", command=self._on_edit).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self._on_delete).pack(side="left", padx=2)
        ttk.Button(btns, text="刷新", command=self.refresh).pack(side="left", padx=2)
        ttk.Button(btns, text="导入 CSV",
                   command=self._on_import_csv).pack(side="left", padx=2)
        ttk.Button(btns, text="导入 JSON",
                   command=self._on_import_json).pack(side="left", padx=2)
        ttk.Button(btns, text="转可交易",
                   command=self._on_promote).pack(side="left", padx=8)
        self._selected_id: Optional[int] = None

    def _on_select(self, pid: Optional[int]):
        self._selected_id = pid

    def refresh(self):
        rows = db.list_stock_pool()
        cells = [
            [r["id"], r["code"], r["name"], r.get("logic", "") or "",
             r.get("key_level", "") or "",
             r.get("catalyst", "") or "",
             r.get("risk", "") or "",
             r.get("status", ""),
             r.get("updated_at", "") or ""]
            for r in rows
        ]
        def tagger(row, kwargs):
            status = row[7] if len(row) > 7 else ""
            if status == "待确认":
                return "pending"
            return None
        self._table.set_rows(cells, tagger=tagger)

    def _on_add(self):
        dlg = StockPoolFormDialog(self._app)
        result = dlg.show()
        if result:
            db.insert_stock_pool(result)
            self.refresh()

    def _on_edit(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        existing = db.get_stock_pool(self._selected_id)
        if not existing:
            messagebox.showerror("错误", "记录不存在")
            return
        dlg = StockPoolFormDialog(self._app, existing=existing)
        result = dlg.show()
        if result:
            db.update_stock_pool(self._selected_id, result)
            self.refresh()

    def _on_delete(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        existing = db.get_stock_pool(self._selected_id)
        if not existing:
            return
        if not messagebox.askyesno(
            "确认删除",
            f"删除 {existing.get('code','')} {existing.get('name','')}？"
        ):
            return
        db.delete_stock_pool(self._selected_id)
        db.insert_red_flag(
            "delete_stock",
            detail=f"删除 {existing.get('code','')} "
                  f"{existing.get('name','')}",
        )
        self._selected_id = None
        self.refresh()

    def _on_import_csv(self):
        path = filedialog.askopenfilename(
            title="选择 CSV 文件",
            filetypes=[("CSV", "*.csv"), ("所有", "*.*")],
        )
        if not path:
            return
        rows = services.import_stock_pool_csv(path)
        if not rows:
            messagebox.showinfo("提示", "未解析到可导入的行")
            return
        dlg = ImportPreviewDialog(self._app, rows)
        chosen = dlg.show()
        if not chosen:
            return
        for r in chosen:
            db.insert_stock_pool(r)
        messagebox.showinfo("导入完成", f"已导入 {len(chosen)} 条")
        self.refresh()

    def _on_import_json(self):
        path = filedialog.askopenfilename(
            title="选择 JSON 文件",
            filetypes=[("JSON", "*.json"), ("所有", "*.*")],
        )
        if not path:
            return
        rows = services.import_stock_pool_json(path)
        if not rows:
            messagebox.showinfo("提示", "未解析到可导入的行")
            return
        dlg = ImportPreviewDialog(self._app, rows)
        chosen = dlg.show()
        if not chosen:
            return
        for r in chosen:
            db.insert_stock_pool(r)
        messagebox.showinfo("导入完成", f"已导入 {len(chosen)} 条")
        self.refresh()

    def _on_promote(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        existing = db.get_stock_pool(self._selected_id)
        if not existing:
            return
        cur_status = existing.get("status")
        if cur_status == "可交易":
            messagebox.showinfo("提示", "该记录已是「可交易」状态")
            return
        if cur_status == "剔除":
            if not messagebox.askyesno(
                "确认",
                f"{existing.get('code','')} {existing.get('name','')} "
                "当前状态为「剔除」，确认要恢复为「可交易」？"
            ):
                return
        # 「观察」「待确认」「剔除」都可转「可交易」
        db.update_stock_pool(self._selected_id, {"status": "可交易"})
        self.refresh()


# ====================================================================
# Tab 3：计划单
# ====================================================================

class TradePlanTab(ttk.Frame):
    def __init__(self, master, app: "TradeHabitApp"):
        super().__init__(master)
        self._app = app
        ttk.Label(self, text="计划单",
                  font=("", 14, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        self._table = EditableTreeView(
            self,
            columns=[
                ("id", "ID", 50),
                ("code", "代码", 80),
                ("name", "名称", 100),
                ("trigger", "入场触发", 160),
                ("range", "入场区间", 120),
                ("stop", "止损", 70),
                ("time_stop", "时间止损", 100),
                ("target", "目标", 70),
                ("shares", "计划股数", 80),
                ("max_loss", "最大亏损", 80),
                ("status", "状态", 80),
            ],
            on_select=self._on_select,
        )
        self._table.pack(fill="both", expand=True, padx=12, pady=4)
        # 状态色
        self._table.tag_configure("entered", background="#d4edda")
        self._table.tag_configure("done", background="#d1ecf1")
        self._table.tag_configure("canceled", background="#f8d7da")
        self._table.tag_configure("pending", background="#fff3cd")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(btns, text="新增", command=self._on_add).pack(side="left", padx=2)
        ttk.Button(btns, text="编辑", command=self._on_edit).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self._on_delete).pack(side="left", padx=2)
        ttk.Button(btns, text="运行检查清单并入场",
                   command=self._on_run_checklist).pack(side="left", padx=2)
        ttk.Button(btns, text="记录出场",
                   command=self._on_record_exit).pack(side="left", padx=2)
        ttk.Button(btns, text="补建交易日志（已入场但无记录）",
                   command=self._on_backfill_log).pack(side="left", padx=2)
        ttk.Button(btns, text="取消计划",
                   command=self._on_cancel).pack(side="left", padx=2)
        ttk.Button(btns, text="复制条件单提醒",
                   command=self._on_copy_order).pack(side="left", padx=2)
        ttk.Button(btns, text="导出 CSV",
                   command=self._on_export).pack(side="left", padx=2)
        ttk.Button(btns, text="刷新", command=self.refresh).pack(side="left", padx=2)
        self._selected_id: Optional[int] = None

    def _on_select(self, pid: Optional[int]):
        self._selected_id = pid

    def refresh(self):
        rows = db.list_trade_plans()
        cells = []
        for r in rows:
            eh = r.get("entry_price_high")
            el = r.get("entry_price_low")
            if eh is not None and el is not None:
                range_str = f"{el}~{eh}"
            elif eh is not None:
                range_str = f"≤{eh}"
            else:
                range_str = "—"
            cells.append([
                r["id"], r.get("stock_code", ""),
                r.get("stock_name", ""),
                r.get("entry_trigger", "") or "",
                range_str,
                r.get("stop_loss", "") if r.get("stop_loss") is not None else "—",
                r.get("time_stop_date", "") or "—",
                r.get("target_price", "") if r.get("target_price") is not None else "—",
                r.get("planned_shares", "") if r.get("planned_shares") is not None else "—",
                r.get("max_loss_amount", "") if r.get("max_loss_amount") is not None else "—",
                r.get("status", ""),
            ])
        def tagger(row, kwargs):
            status = row[10] if len(row) > 10 else ""
            mapping = {
                "已入场": "entered",
                "已结束": "done",
                "取消": "canceled",
                "待触发": "pending",
            }
            return mapping.get(status)
        self._table.set_rows(cells, tagger=tagger)

    def _on_add(self):
        dlg = TradePlanFormDialog(self._app)
        result = dlg.show()
        if result:
            db.insert_trade_plan(result)
            self.refresh()

    def _on_edit(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        existing = db.get_trade_plan(self._selected_id)
        if not existing:
            return
        dlg = TradePlanFormDialog(self._app, existing=existing)
        result = dlg.show()
        if result:
            db.update_trade_plan(self._selected_id, result)
            self.refresh()

    def _on_delete(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        existing = db.get_trade_plan(self._selected_id)
        if not existing:
            return
        if existing.get("status") in ("已入场", "已结束"):
            messagebox.showerror("错误",
                                 "已入场或已结束的计划不可删除")
            return
        if not messagebox.askyesno(
            "确认删除",
            f"删除计划 #{existing['id']} "
            f"{existing.get('stock_code','')}？"
        ):
            return
        try:
            db.delete_trade_plan(self._selected_id)
        except Exception as e:
            messagebox.showerror("错误",
                                 f"删除失败：{e}\n（如有关联 trade_log 不可删除）")
            return
        self._selected_id = None
        self.refresh()

    def _on_run_checklist(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        plan = db.get_trade_plan(self._selected_id)
        if not plan:
            return
        if plan.get("status") != "待触发":
            messagebox.showerror("错误",
                                 "仅「待触发」状态的计划可入场")
            return
        settings = db.get_settings()
        today_entries = db.count_today_entries()
        today_pnl = db.sum_today_pnl()
        traffic, _ = services.get_traffic_light(
            settings, today_pnl, today_entries
        )
        items = services.build_checklist(
            plan, settings, today_entries, traffic
        )
        dlg = ChecklistDialog(self._app, items, plan)
        result = dlg.show()
        if not result or result.get("action") == "cancel":
            return
        override = result.get("override_reason", "")
        items_result = result.get("items", items)
        # 写 checklist_run
        db.insert_checklist_run({
            "plan_id": plan["id"],
            "checked_items": [
                {"key": it["key"], "passed": it["passed"]}
                for it in items_result
            ],
            "all_passed": 0 if override else 1,
            "override_reason": override,
        })
        if override:
            db.insert_red_flag(
                "override_checklist",
                detail=f"计划 #{plan['id']} {plan.get('stock_code','')}",
                override_reason=override,
            )
        # 弹入场对话框
        edlg = EntryDialog(self._app, plan)
        enter_data = edlg.show()
        if not enter_data:
            messagebox.showinfo("提示",
                                "未填写入场信息，已取消")
            return
        # 写 trade_log
        log_id = db.insert_trade_log({
            "plan_id": plan["id"],
            "stock_code": plan.get("stock_code", ""),
            "stock_name": plan.get("stock_name", ""),
            "entry_time": enter_data["entry_time"],
            "entry_price": enter_data["entry_price"],
            "shares": enter_data["shares"],
            "fees": enter_data["fees"],
            "followed_plan": 1,
        })
        # 关联 checklist_run 到 trade_log
        # 用最近一条 checklist_run 更新 trade_log_id
        runs = db.list_checklist_runs()
        if runs:
            db.update_checklist_run_trade_log_id(
                runs[0]["id"], log_id
            )
        # 计划状态改「已入场」
        db.update_trade_plan(plan["id"], {"status": "已入场"})
        messagebox.showinfo("入场已记录",
                            f"trade_log #{log_id} 已创建")
        self._app.refresh_all()

    def _on_backfill_log(self):
        """给已入场但没有 trade_log 的计划补建交易日志。

        触发场景：用户之前手动把计划状态改成了「已入场」，
        跳过了检查清单 + 入场弹窗，导致 trade_log 表里没记录。
        本按钮打开 EntryDialog 让用户补填实际入场信息。
        """
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        plan = db.get_trade_plan(self._selected_id)
        if not plan:
            return
        # 检查是否已有 trade_log
        existing_log = db.get_trade_log_by_plan(plan["id"])
        if existing_log:
            messagebox.showinfo(
                "提示",
                f"计划 #{plan['id']} 已有交易日志 #{existing_log['id']}，"
                "无需补建。"
            )
            return
        if plan.get("status") != "已入场":
            # 允许「已结束」状态也补建（出场时已填但没入场记录的场景）
            if plan.get("status") not in ("已入场", "已结束"):
                messagebox.showerror(
                    "错误",
                    f"仅「已入场」或「已结束」状态的计划可补建，"
                    f"当前状态：{plan.get('status')}"
                )
                return
        # 弹 EntryDialog 补填入场信息
        edlg = EntryDialog(self._app, plan)
        enter_data = edlg.show()
        if not enter_data:
            return
        log_id = db.insert_trade_log({
            "plan_id": plan["id"],
            "stock_code": plan.get("stock_code", ""),
            "stock_name": plan.get("stock_name", ""),
            "entry_time": enter_data["entry_time"],
            "entry_price": enter_data["entry_price"],
            "shares": enter_data["shares"],
            "fees": enter_data["fees"],
            "followed_plan": 1,
        })
        # 如果计划已结束，保持状态；如果已入场，也保持不变
        db.insert_red_flag(
            "backfill_trade_log",
            detail=f"计划 #{plan['id']} {plan.get('stock_code','')} "
                   f"补建交易日志 #{log_id}（之前手动改状态跳过了入场流程）",
        )
        messagebox.showinfo(
            "已补建",
            f"trade_log #{log_id} 已创建\n"
            f"注意：此条为补建记录，已记入红灯事件。\n"
            "请到「交易日志」Tab 确认。"
        )
        self._app.refresh_all()

    def _on_record_exit(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        plan = db.get_trade_plan(self._selected_id)
        if not plan:
            return
        if plan.get("status") != "已入场":
            messagebox.showerror("错误",
                                 "仅「已入场」状态的计划可记录出场")
            return
        log = db.get_trade_log_by_plan(plan["id"])
        if not log:
            messagebox.showerror("错误",
                                 "未找到对应的 trade_log 记录")
            return
        dlg = ExitDialog(self._app, plan, log=log)
        exit_data = dlg.show()
        if not exit_data:
            return
        # 计算盈亏
        entry_price = float(log.get("entry_price") or 0)
        shares = int(log.get("shares") or 0)
        stop_loss = float(plan.get("stop_loss") or 0)
        pnl, pnl_r = services.calc_pnl(
            entry_price, exit_data["exit_price"], shares,
            exit_data["fees"], stop_loss,
        )
        db.update_trade_log(log["id"], {
            "exit_time": exit_data["exit_time"],
            "exit_price": exit_data["exit_price"],
            "fees": exit_data["fees"],
            "pnl_amount": pnl,
            "pnl_r": pnl_r,
            "followed_plan": exit_data["followed_plan"],
            "deviation_reason": exit_data["deviation_reason"],
            "emotion_tag": exit_data["emotion_tag"],
            "emotion_intensity": exit_data["emotion_intensity"],
            "notes": exit_data["notes"],
        })
        db.update_trade_plan(plan["id"], {"status": "已结束"})
        messagebox.showinfo("出场已记录",
                            f"盈亏 {pnl:.2f}（{pnl_r:.2f}R）")
        self._app.refresh_all()

    def _on_cancel(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        plan = db.get_trade_plan(self._selected_id)
        if not plan:
            return
        if plan.get("status") == "已结束":
            messagebox.showerror("错误", "已结束计划不可取消")
            return
        if not messagebox.askyesno("确认取消",
                                   f"取消计划 #{plan['id']}？"):
            return
        db.update_trade_plan(plan["id"], {"status": "取消"})
        self.refresh()

    def _on_copy_order(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        plan = db.get_trade_plan(self._selected_id)
        if not plan:
            return
        stock = db.get_stock_pool_by_code(plan.get("stock_code", ""))
        text = services.build_condition_order_text(plan, stock)
        self._app.clipboard_clear()
        self._app.clipboard_append(text)
        messagebox.showinfo("已复制",
                            "条件单提醒已复制到剪贴板：\n\n" + text)

    def _on_export(self):
        path = filedialog.asksaveasfilename(
            title="保存计划单 CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"trade_plans_{services.today_iso()}.csv",
        )
        if not path:
            return
        plans = db.list_trade_plans()
        services.export_trade_plans_csv(plans, path)
        messagebox.showinfo("导出完成",
                            f"已导出 {len(plans)} 条到\n{path}")


# ====================================================================
# Tab 4：交易日志
# ====================================================================

class TradeLogTab(ttk.Frame):
    def __init__(self, master, app: "TradeHabitApp"):
        super().__init__(master)
        self._app = app
        ttk.Label(self, text="交易日志",
                  font=("", 14, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        self._table = EditableTreeView(
            self,
            columns=[
                ("id", "ID", 50),
                ("plan_id", "计划ID", 60),
                ("code", "代码", 70),
                ("name", "名称", 90),
                ("entry_time", "入场时间", 140),
                ("entry_price", "入场价", 70),
                ("shares", "数量", 60),
                ("exit_time", "出场时间", 140),
                ("exit_price", "出场价", 70),
                ("pnl", "盈亏", 80),
                ("pnl_r", "盈亏R", 60),
                ("followed", "是否按计划", 80),
                ("deviation", "偏差原因", 140),
                ("emotion", "情绪", 60),
                ("intensity", "强度", 50),
            ],
            on_select=self._on_select,
        )
        self._table.pack(fill="both", expand=True, padx=12, pady=4)
        self._table.tag_configure("loss", background="#f8d7da")
        self._table.tag_configure("win", background="#d4edda")
        self._table.tag_configure("no_exit", background="#fff3cd")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(btns, text="编辑（补全出场/情绪）",
                   command=self._on_edit).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self._on_delete).pack(side="left", padx=2)
        ttk.Button(btns, text="导出 CSV",
                   command=self._on_export).pack(side="left", padx=2)
        ttk.Button(btns, text="刷新", command=self.refresh).pack(side="left", padx=2)
        self._selected_id: Optional[int] = None

    def _on_select(self, lid: Optional[int]):
        self._selected_id = lid

    def refresh(self):
        rows = db.list_trade_logs()
        cells = []
        for r in rows:
            pnl = r.get("pnl_amount")
            pnl_str = f"{pnl:.2f}" if pnl is not None else "—"
            pnl_r = r.get("pnl_r")
            pnl_r_str = f"{pnl_r:.2f}" if pnl_r is not None else "—"
            followed = r.get("followed_plan")
            followed_str = ("是" if followed == 1 else
                            "否" if followed == 0 else "—")
            cells.append([
                r["id"],
                r.get("plan_id") or "",
                r.get("stock_code", ""),
                r.get("stock_name", ""),
                r.get("entry_time", "") or "—",
                r.get("entry_price") if r.get("entry_price") is not None else "—",
                r.get("shares") if r.get("shares") is not None else "—",
                r.get("exit_time", "") or "—",
                r.get("exit_price") if r.get("exit_price") is not None else "—",
                pnl_str,
                pnl_r_str,
                followed_str,
                r.get("deviation_reason", "") or "",
                r.get("emotion_tag", "") or "",
                r.get("emotion_intensity") if r.get("emotion_intensity") is not None else "—",
            ])
        def tagger(row, kwargs):
            pnl_str = row[9] if len(row) > 9 else ""
            exit_str = row[7] if len(row) > 7 else ""
            if pnl_str == "—" or pnl_str == "":
                if exit_str == "—" or not exit_str:
                    return "no_exit"
                return None
            try:
                pnl = float(pnl_str)
                if pnl > 0:
                    return "win"
                if pnl < 0:
                    return "loss"
            except ValueError:
                pass
            return None
        self._table.set_rows(cells, tagger=tagger)

    def _on_edit(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        log = db.get_trade_log(self._selected_id)
        if not log:
            return
        plan = (db.get_trade_plan(log["plan_id"])
                if log.get("plan_id") else None)
        if not plan:
            messagebox.showerror("错误", "关联计划单不存在")
            return
        dlg = ExitDialog(self._app, plan, log=log)
        exit_data = dlg.show()
        if not exit_data:
            return
        entry_price = float(log.get("entry_price") or 0)
        shares = int(log.get("shares") or 0)
        stop_loss = float(plan.get("stop_loss") or 0)
        pnl, pnl_r = services.calc_pnl(
            entry_price, exit_data["exit_price"], shares,
            exit_data["fees"], stop_loss,
        )
        db.update_trade_log(log["id"], {
            "exit_time": exit_data["exit_time"],
            "exit_price": exit_data["exit_price"],
            "fees": exit_data["fees"],
            "pnl_amount": pnl,
            "pnl_r": pnl_r,
            "followed_plan": exit_data["followed_plan"],
            "deviation_reason": exit_data["deviation_reason"],
            "emotion_tag": exit_data["emotion_tag"],
            "emotion_intensity": exit_data["emotion_intensity"],
            "notes": exit_data["notes"],
        })
        # 若 plan 处于"已入场"，更新为"已结束"
        if plan.get("status") == "已入场":
            db.update_trade_plan(plan["id"], {"status": "已结束"})
        messagebox.showinfo("已保存",
                            f"盈亏 {pnl:.2f}（{pnl_r:.2f}R）")
        self._app.refresh_all()

    def _on_delete(self):
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一行")
            return
        log = db.get_trade_log(self._selected_id)
        if not log:
            return
        if not messagebox.askyesno(
            "确认删除",
            f"删除 trade_log #{log['id']}？"
        ):
            return
        db.delete_trade_log(self._selected_id)
        self._selected_id = None
        self._app.refresh_all()

    def _on_export(self):
        path = filedialog.asksaveasfilename(
            title="保存交易日志 CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"trade_logs_{services.today_iso()}.csv",
        )
        if not path:
            return
        logs = db.list_trade_logs()
        services.export_trade_logs_csv(logs, path)
        messagebox.showinfo("导出完成",
                            f"已导出 {len(logs)} 条到\n{path}")


# ====================================================================
# Tab 5：设置
# ====================================================================

class SettingsTab(ttk.Frame):
    def __init__(self, master, app: "TradeHabitApp"):
        super().__init__(master)
        self._app = app
        ttk.Label(self, text="设置",
                  font=("", 14, "bold")).pack(anchor="w", padx=12, pady=(8, 4))

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=8)
        self._vars: dict[str, tk.StringVar] = {}

        s = db.get_settings()
        self._add_row(form, 0, "账户资金", "account_balance",
                      s.get("account_balance", "100000"))
        self._add_row(form, 1, "单笔风险比例 %（0-100）",
                      "risk_per_trade_pct",
                      s.get("risk_per_trade_pct", "0.5"))
        self._add_row(form, 2, "单日最大亏损", "daily_max_loss",
                      s.get("daily_max_loss", "1000"))
        self._add_row(form, 3, "单日最大交易次数（1-100）",
                      "daily_max_trades",
                      s.get("daily_max_trades", "3"))
        # 情绪标签
        ttk.Label(form, text="情绪标签（逗号分隔）").grid(
            row=4, column=0, sticky="nw", pady=4)
        emo_text = tk.Text(form, width=40, height=3, wrap="word")
        emo_text.insert("1.0", s.get("emotion_tags", ""))
        emo_text.grid(row=4, column=1, sticky="nsew", pady=4, padx=(8, 0))
        self._emo_text = emo_text
        form.grid_columnconfigure(1, weight=1)

        # 预览
        ttk.Label(form, text="解析后预览：").grid(
            row=5, column=0, sticky="w", pady=4)
        self._preview = ttk.Label(form, text="", wraplength=400)
        self._preview.grid(row=5, column=1, sticky="w", pady=4, padx=(8, 0))
        emo_text.bind("<KeyRelease>", self._refresh_preview)
        self._refresh_preview()

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(btns, text="保存", command=self._on_save).pack(side="left", padx=2)
        ttk.Button(btns, text="重置默认",
                   command=self._on_reset).pack(side="left", padx=2)

    def _add_row(self, parent, row, label, key, default):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=4)
        var = tk.StringVar(value=str(default))
        ent = ttk.Entry(parent, textvariable=var, width=30)
        ent.grid(row=row, column=1, sticky="w", pady=4, padx=(8, 0))
        self._vars[key] = var

    def _refresh_preview(self, _evt=None):
        text = self._emo_text.get("1.0", "end").rstrip("\n")
        tags = [t.strip() for t in text.split(",") if t.strip()]
        self._preview.config(text="、".join(tags) if tags else "（空）")

    def _on_save(self):
        try:
            balance = float(self._vars["account_balance"].get())
            if balance <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "账户资金必须为正数")
            return
        try:
            rp = float(self._vars["risk_per_trade_pct"].get())
            if not (0 <= rp <= 100):
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "单笔风险比例需 0-100")
            return
        try:
            dml = float(self._vars["daily_max_loss"].get())
            if dml < 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "单日最大亏损需 ≥0")
            return
        try:
            dmt = int(float(self._vars["daily_max_trades"].get()))
            if not (1 <= dmt <= 100):
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "单日最大交易次数需 1-100")
            return
        emo = self._emo_text.get("1.0", "end").rstrip("\n")
        if not emo.strip():
            messagebox.showerror("错误", "情绪标签不能为空")
            return
        db.set_settings({
            "account_balance": str(balance),
            "risk_per_trade_pct": str(rp),
            "daily_max_loss": str(dml),
            "daily_max_trades": str(dmt),
            "emotion_tags": emo,
        })
        messagebox.showinfo("已保存", "设置已保存，立即生效")
        self._app.refresh_all()

    def _on_reset(self):
        if not messagebox.askyesno("确认", "恢复默认设置？"):
            return
        for k, v in db.DEFAULT_SETTINGS.items():
            self._vars[k].set(v) if k in self._vars else None
        self._emo_text.delete("1.0", "end")
        self._emo_text.insert("1.0", db.DEFAULT_SETTINGS.get("emotion_tags", ""))
        self._refresh_preview()


# ====================================================================
# 主窗口
# ====================================================================

class TradeHabitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("交易习惯约束器 MVP")
        self.geometry("1280x800")
        self._build_notebook()
        self.refresh_all()

    def _build_notebook(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self._dashboard = DashboardTab(nb, self)
        self._stock_pool = StockPoolTab(nb, self)
        self._trade_plan = TradePlanTab(nb, self)
        self._trade_log = TradeLogTab(nb, self)
        self._settings = SettingsTab(nb, self)
        nb.add(self._dashboard, text="仪表盘")
        nb.add(self._stock_pool, text="股票池")
        nb.add(self._trade_plan, text="计划单")
        nb.add(self._trade_log, text="交易日志")
        nb.add(self._settings, text="设置")
        # 切换 Tab 时刷新
        nb.bind("<<NotebookTabChanged>>",
                lambda e: self.refresh_all())

    def refresh_all(self):
        """刷新所有 Tab（dashboard 优先）。"""
        try:
            self._dashboard.refresh()
        except Exception:
            pass
        try:
            self._stock_pool.refresh()
        except Exception:
            pass
        try:
            self._trade_plan.refresh()
        except Exception:
            pass
        try:
            self._trade_log.refresh()
        except Exception:
            pass
