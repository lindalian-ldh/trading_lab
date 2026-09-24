"""数据读取与清洗层。

职责：
1. 按扩展名分流读取 .xls（xlrd）/ .xlsx（openpyxl）。
2. 必需列缺失时明确报错（如"缺少关键列：佣金"）。
3. 可选规费列缺失时补 0。
4. 类型归一：成交日期→datetime；数值列→float。
5. 剔除现金理财（天添利 / 产品申购 / 产品赎回），仅保留股票/ETF二级市场买卖。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import (
    CASH_PRODUCT_KEYWORDS,
    OPTIONAL_FEE_COLUMNS,
    REQUIRED_COLUMNS,
)

# 数值列统一转 float
_NUMERIC_COLUMNS = [
    "成交价格",
    "成交数量",
    "发生金额",
    "剩余数量",
    "佣金",
    "印花税",
]


def _clean_cell(value):
    """清洗单元格：去 BOM(\\ufeff) 与零宽字符。
    招商证券导出的 xls 中，数值与文本单元格前常带 \\ufeff BOM 前缀
    （如 '\\ufeff27.03' 被识别为字符串，pd.to_numeric 转换失败后 fillna(0) 全部归零）。
    对非字符串原值（int/float）原样返回。
    """
    if isinstance(value, str):
        for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u3000"):
            value = value.replace(ch, "")
        return value.strip()
    return value


def _clean_column_name(name) -> str:
    """清洗列名：去 BOM、全角空格、前后空白/换行/tab/回车。"""
    return _clean_cell(name) if isinstance(name, str) else str(name)


def load_flow(file_path: str) -> tuple[pd.DataFrame, int]:
    """读取资金流水文件并清洗。

    Returns:
        (df, cleaned_count) —— 清洗后 DataFrame 与被剔除的现金理财条数。
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    # 按扩展名选引擎
    ext = path.suffix.lower()
    if ext == ".xls":
        engine = "xlrd"
    elif ext == ".xlsx":
        engine = "openpyxl"
    else:
        raise ValueError(f"仅支持 .xls / .xlsx 文件，当前扩展名：{ext}")

    df = pd.read_excel(path, engine=engine)

    # —— 全局清洗：列名 + 所有单元格去 BOM 与不可见字符 ——
    # 招商证券 xls 的数值单元格前也带 \ufeff，导致 pd.to_numeric 失败后归零
    df.columns = [_clean_column_name(c) for c in df.columns]
    # pandas 2.1+ 移除 applymap 改名 map；旧版用 applymap
    _map = getattr(df, "map", None) or df.applymap
    df = _map(_clean_cell)

    # —— 列校验：必需列缺失即报错（附实际列名便于诊断） ——
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        actual = list(df.columns)
        raise ValueError(
            f"缺少关键列：{', '.join(missing)}（实际读取到的列名：{actual}）"
        )

    # —— 可选规费列：缺失补 0 ——
    for col in OPTIONAL_FEE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    # —— 类型归一 ——
    # 成交日期：int(20260908) 或字符串 → datetime
    df["成交日期"] = pd.to_datetime(df["成交日期"].astype(str), format="%Y%m%d", errors="coerce")
    for col in _NUMERIC_COLUMNS + OPTIONAL_FEE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # —— 剔除现金理财 ——
    before = len(df)
    mask_cash = df["证券名称"].astype(str).str.contains(
        "|".join(CASH_PRODUCT_KEYWORDS), na=False
    )
    df = df[~mask_cash].reset_index(drop=True)
    cleaned_count = before - len(df)

    if df.empty:
        raise ValueError("清洗后无有效交易记录（可能全部为现金理财，或文件为空）")

    return df, cleaned_count
