"""P2.3 收益审计的统计内核 —— 两个统计量 + 一个安慰剂，**零 IO**。

## 为什么需要这个模块（而不是直接用 `audit_regime_hold.py`）

[`audit_regime_hold.py`](../scripts/audit_regime_hold.py) 只报「差值 + placebo p」，
**没有 t 统计量**；而 P0.5 §⑥-1 要求 `|t| ≥ 1.96`。若用"把每一天当独立样本"的朴素 t，
在**重叠的 H 日持有窗口**（相邻两天的 `fwd20` 有 19 天重叠）加上 L1 这类**状态掩码**
（连续几十天为真）下，会**把噪声报成显著** —— 有效样本远小于天数。

所以本模块的主统计量用**块长 = H 的移动块 bootstrap**：
把连续 H 个交易日当作一块整体重采样（块内重叠"内部化"、块间近似独立），
在每次重采样里**同时**带上收益与掩码标记，重算差值。

## 主统计量（P0.5 修订记录 #8 冻结）

```
观测      fwd_H(i) = 第 i 日**次日开盘**入场、持 H 根**收盘**出场（%）
          与 audit_regime_hold.fwd_ret 逐步一致（含最后一根不足则 NaN）
点估计    D = mean(fwd_H | mask) − mean(fwd_H | ¬mask)        （pp）
SE        块长 = H 的移动块 bootstrap（B=2000, 固定 seed）的 D* 标准差
t         D / SE
p         2·min(P(D*≤0), P(D*≥0))
MDE       2.80 × SE      （α=0.05 双尾 + 80% 功效；与 paired_stats 口径一致）
placebo   片段匹配重采样（保持片段长度分布，单尾）—— 沿用 audit_regime_hold
```

**不参与判定**的诊断量：朴素日级 Welch t、片段级（每个片段首日一个观测）t。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 块 bootstrap 的默认重复次数与种子（固定 seed ⇒ 可复现）
N_BOOT = 2000
BOOT_SEED = 20261004
POWER_Z = 2.80


# ====================================================================
# 前向收益（与 audit_regime_hold.fwd_ret 逐步一致）
# ====================================================================

def fwd_ret(df: pd.DataFrame, hold: int) -> np.ndarray:
    """次日开盘入场 → 第 ``hold`` 根收盘出场，单位 **%**；不足则 NaN。

    与 [`audit_regime_hold.py`](../scripts/audit_regime_hold.py) 的 ``fwd_ret`` 同口径：
    ``i`` → 入场 ``open[i+1]``、出场 ``close[i+hold]``。
    """
    o = np.asarray(df["open"], dtype=float)
    c = np.asarray(df["close"], dtype=float)
    n = len(df)
    out = np.full(n, np.nan)
    for i in range(n):
        j = i + 1
        k = j + int(hold) - 1
        if j < n and k < n and np.isfinite(o[j]) and o[j] > 0:
            out[i] = (c[k] / o[j] - 1.0) * 100.0
    return out


def _clean(rets, mask) -> tuple:
    r = np.asarray(rets, dtype=float)
    m = np.asarray(mask).astype(bool)
    if len(r) != len(m):
        raise ValueError(f"收益序列与掩码长度不一致: {len(r)} vs {len(m)}")
    ok = np.isfinite(r)
    return r, m, ok


# ====================================================================
# 点估计与朴素诊断
# ====================================================================

def two_sample_diff(rets, mask) -> float:
    """``mean(掩码内) − mean(掩码外)``（pp）；任一侧为空返回 NaN。"""
    r, m, ok = _clean(rets, mask)
    a, b = r[m & ok], r[~m & ok]
    if len(a) == 0 or len(b) == 0:
        return np.nan
    return float(a.mean() - b.mean())


def naive_day_t(rets, mask) -> dict:
    """**朴素日级** Welch t（把每天当独立样本）—— 只作诊断，**不参与判定**。"""
    r, m, ok = _clean(rets, mask)
    a, b = r[m & ok], r[~m & ok]
    if len(a) < 3 or len(b) < 3:
        return {"t": np.nan, "se": np.nan, "n_sig": len(a), "n_base": len(b)}
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = float(np.sqrt(va / len(a) + vb / len(b)))
    if se == 0 or not np.isfinite(se):
        return {"t": np.nan, "se": se, "n_sig": len(a), "n_base": len(b)}
    return {"t": float((a.mean() - b.mean()) / se), "se": se,
            "n_sig": int(len(a)), "n_base": int(len(b))}


# ====================================================================
# 块 bootstrap（主统计量）
# ====================================================================

def block_bootstrap_diff(rets, mask, block: int,
                         n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> dict:
    """块长 = ``block`` 的移动块 bootstrap，返回 ``D / SE / t / p / ci``。

    每次重采样抽 ``ceil(n/block)`` 个**随机起点**的连续 ``block`` 日块（有放回、可重叠），
    块内**同时**携带收益与掩码标记 ⇒ 保留块内自相关与"状态连续为真"的结构。
    """
    r, m, ok = _clean(rets, mask)
    n = len(r)
    valid = ok & np.isfinite(r)
    if valid.sum() < 3 or (~m & valid).sum() < 3 or (m & valid).sum() < 3:
        return {"D": np.nan, "se": np.nan, "t": np.nan, "p": np.nan,
                "ci_lo": np.nan, "ci_hi": np.nan, "mde": np.nan,
                "n_boot": 0, "block": int(block)}
    block = int(max(1, min(block, n)))
    d_point = two_sample_diff(r, m)

    starts_max = n - block
    k = int(np.ceil(n / block))
    rng = np.random.default_rng(seed)
    offs = np.arange(block)
    ds = np.empty(n_boot, dtype=float)
    chunk = 250
    for s0 in range(0, n_boot, chunk):
        s1 = min(s0 + chunk, n_boot)
        starts = rng.integers(0, starts_max + 1, size=(s1 - s0, k))
        idx = (starts[:, :, None] + offs[None, None, :]).reshape(s1 - s0, -1)
        rs = r[idx]
        ms = m[idx]
        good = np.isfinite(rs)
        num_a = np.where(ms & good, rs, 0.0).sum(axis=1)
        cnt_a = (ms & good).sum(axis=1)
        num_b = np.where(~ms & good, rs, 0.0).sum(axis=1)
        cnt_b = ((~ms) & good).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            ds[s0:s1] = np.where((cnt_a > 0) & (cnt_b > 0),
                                 num_a / np.maximum(cnt_a, 1) - num_b / np.maximum(cnt_b, 1),
                                 np.nan)
    ds = ds[np.isfinite(ds)]
    if len(ds) < 20:
        return {"D": d_point, "se": np.nan, "t": np.nan, "p": np.nan,
                "ci_lo": np.nan, "ci_hi": np.nan, "mde": np.nan,
                "n_boot": int(len(ds)), "block": block}
    se = float(ds.std(ddof=1))
    le, ge = float((ds <= 0).mean()), float((ds >= 0).mean())
    p = float(2 * min(le, ge))
    return {
        "D": d_point,
        "se": se,
        "t": float(d_point / se) if se > 0 else np.nan,
        "p": min(p, 1.0),
        "ci_lo": float(np.percentile(ds, 2.5)),
        "ci_hi": float(np.percentile(ds, 97.5)),
        "mde": POWER_Z * se,
        "n_boot": int(len(ds)),
        "block": block,
    }


# ====================================================================
# 片段匹配安慰剂（沿用 audit_regime_hold 的做法）
# ====================================================================

def episode_spans(mask, gap: int = 3) -> list:
    """布尔掩码 → 片段 ``[(start, end, n_days), …]``（相邻 ≤gap 合并）。"""
    pos = np.where(np.asarray(mask, dtype=bool))[0]
    if len(pos) == 0:
        return []
    spans, s, prev = [], int(pos[0]), int(pos[0])
    for p in pos[1:]:
        p = int(p)
        if p - prev <= int(gap):
            prev = p
            continue
        spans.append((s, prev, prev - s + 1))
        s = prev = p
    spans.append((s, prev, prev - s + 1))
    return spans


def placebo_p(rets, mask, rng: np.random.Generator, n: int = 200,
              gap: int = 3) -> tuple:
    """片段匹配重采样（保持片段长度分布）→ ``(零分布 95 分位, p)``。

    沿用 [`audit_regime_hold.py`](../scripts/audit_regime_hold.py) 的做法：
    随机挑同样段数、同样各段长度的日子（只在有效日里挑）。单尾 ``p = P(空 ≥ 观测)``。
    """
    r, m, ok = _clean(rets, mask)
    v = np.where(ok, r, np.nan)
    pos = np.where(m & ok)[0]
    if len(pos) == 0:
        return np.nan, np.nan
    lens = [L for _s, _e, L in episode_spans(m & ok, gap=gap)]
    avail = np.where(ok)[0]
    obs = float(v[pos].mean())
    null = []
    for _ in range(int(n)):
        take: list = []
        for L in lens:
            if len(avail) <= L:
                continue
            st = int(rng.integers(0, len(avail) - L))
            take.extend(avail[st:st + L].tolist())
        if len(take) >= 5:
            null.append(float(v[np.array(take)].mean()))
    if len(null) < 20:
        return np.nan, np.nan
    null_a = np.asarray(null)
    return float(np.percentile(null_a, 95)), float((null_a >= obs).mean())


# ====================================================================
# 片段级诊断（每个片段首日一个观测）
# ====================================================================

def episode_level_stats(rets, mask, gap: int = 3) -> dict:
    """**片段级**诊断：每个片段取首日 → 一个观测（"一次决策"视角）。不参与判定。"""
    r, m, ok = _clean(rets, mask)
    firsts = [s for s, _e, _l in episode_spans(m & ok, gap=gap)]
    a = np.array([r[i] for i in firsts if np.isfinite(r[i])])
    b = r[~m & ok]
    if len(a) < 3 or len(b) < 3:
        return {"n_episodes": len(firsts), "mean_sig": float(a.mean()) if len(a) else np.nan,
                "t": np.nan}
    se = float(np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))
    return {
        "n_episodes": len(firsts),
        "mean_sig": float(a.mean()),
        "mean_base": float(b.mean()),
        "t": float((a.mean() - b.mean()) / se) if se > 0 else np.nan,
    }


# ====================================================================
# 组装
# ====================================================================

def summarize(rets, mask, block: int, *, n_boot: int = N_BOOT,
              seed: int = BOOT_SEED, n_placebo: int = 200,
              placebo_seed: int = BOOT_SEED, gap: int = 3) -> dict:
    """一次算齐：点估计 / 块 bootstrap 的 D·SE·t·p·CI·MDE / placebo / 两个诊断。"""
    r, m, ok = _clean(rets, mask)
    sig_days = int((m & ok).sum())
    spans = episode_spans(m & ok, gap=gap)
    boot = block_bootstrap_diff(r, m, block=block, n_boot=n_boot, seed=seed)
    q, p_pl = placebo_p(r, m, np.random.default_rng(placebo_seed), n=n_placebo, gap=gap)
    out = dict(boot)
    out.update({
        "n_signal_days": sig_days,
        "n_base_days": int((~m & ok).sum()),
        "n_episodes": len(spans),
        "placebo_q95": q,
        "placebo_p": p_pl,
        "naive_day_t": naive_day_t(r, m)["t"],
    })
    out.update({"episode_" + k: v for k, v in episode_level_stats(r, m, gap=gap).items()})
    return out


__all__ = [
    "N_BOOT", "BOOT_SEED", "POWER_Z",
    "fwd_ret", "two_sample_diff", "naive_day_t",
    "block_bootstrap_diff", "episode_spans", "placebo_p",
    "episode_level_stats", "summarize",
]
