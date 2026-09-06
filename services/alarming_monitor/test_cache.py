"""cache.py 双层缓存降级测试。

重点验证：
    - freshness 校验触发重拉时，若网络失败，返回 history + 旧 latest（仅缺最新日）
    - 失败时不回写、不清缓存文件（旧 latest 文件保留）
    - TTL 过期路径同样保留旧 latest 作为兜底
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cache  # noqa: E402


@pytest.fixture
def tmp_cache_root(tmp_path, monkeypatch):
    """把缓存根目录指向 tmp_path（通过 monkeypatch _project_root）。"""
    monkeypatch.setattr(cache, '_project_root', lambda: tmp_path)
    return tmp_path


def _write(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding='utf-8-sig')


def test_freshness_refetch_failure_keeps_old_latest(tmp_cache_root):
    """freshness 触发重拉 → 网络失败 → 返回 history+旧 latest，不清缓存。"""
    key = 'idx_x'
    hist_path = cache._history_path(key)
    latest_path = cache._latest_path(key)

    # 历史：40 天前 ~ 31 天前
    hist_dates = [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                  for d in range(60, 30, -1)]
    hist_df = pd.DataFrame({'date': hist_dates, 'close': range(len(hist_dates))})

    # 旧 latest：最近 30 日（不含今日）→ 触发 freshness 重拉
    latest_dates = [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                    for d in range(30, 0, -1)]
    latest_df = pd.DataFrame({'date': latest_dates, 'close': range(len(latest_dates))})

    _write(hist_path, hist_df)
    _write(latest_path, latest_df)

    # 拉取函数始终失败
    @cache.cached(key_fn=lambda: key, ttl_hours=20, freshness_check=True,
                  min_refetch_minutes=0)
    def fetch():
        raise RuntimeError('network down')

    result = fetch()

    # 降级返回：history + 旧 latest 合并
    assert result is not None
    merged_dates = set(result['date'].astype(str))
    assert set(hist_dates).issubset(merged_dates)
    assert set(latest_dates).issubset(merged_dates)
    # 近 30 日（旧 latest）仍在
    assert len(result) >= len(latest_df)

    # 缓存文件未被清除/覆盖
    assert latest_path.exists()
    on_disk = pd.read_csv(latest_path)
    assert list(on_disk['date'].astype(str)) == latest_dates


def test_ttl_expired_refetch_failure_keeps_old_latest(tmp_cache_root):
    """TTL 过期触发重拉 → 失败 → 仍返回旧 latest 兜底。"""
    key = 'idx_y'
    hist_path = cache._history_path(key)
    latest_path = cache._latest_path(key)

    hist_df = pd.DataFrame({
        'date': [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                 for d in range(60, 30, -1)],
        'close': range(30),
    })
    latest_dates = [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                    for d in range(30, 0, -1)]
    latest_df = pd.DataFrame({'date': latest_dates, 'close': range(30)})

    _write(hist_path, hist_df)
    _write(latest_path, latest_df)
    # 把 latest mtime 改到 TTL 之前 → 过期
    old_mtime = time.time() - 25 * 3600
    os.utime(latest_path, (old_mtime, old_mtime))

    @cache.cached(key_fn=lambda: key, ttl_hours=20, freshness_check=False)
    def fetch():
        return None

    result = fetch()
    assert result is not None
    assert set(latest_dates).issubset(set(result['date'].astype(str)))
    # 不回写：mtime 不变
    assert abs(latest_path.stat().st_mtime - old_mtime) < 1.0


def test_no_cache_and_failure_returns_none(tmp_cache_root):
    """无任何缓存且拉取失败 → 返回 None。"""
    key = 'idx_z'

    @cache.cached(key_fn=lambda: key, ttl_hours=20)
    def fetch():
        return None

    assert fetch() is None


def test_refetch_success_overwrites_latest(tmp_cache_root):
    """重拉成功 → 覆盖 latest，返回新数据。"""
    key = 'idx_w'
    latest_path = cache._latest_path(key)

    old_dates = [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                 for d in range(30, 0, -1)]
    _write(latest_path, pd.DataFrame({'date': old_dates, 'close': range(30)}))
    old_mtime = time.time() - 25 * 3600
    os.utime(latest_path, (old_mtime, old_mtime))

    new_df = pd.DataFrame({
        'date': [(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
                 for d in range(29, -1, -1)],
        'close': range(30),
    })

    @cache.cached(key_fn=lambda: key, ttl_hours=20, freshness_check=False)
    def fetch():
        return new_df

    result = fetch()
    assert result is not None
    # 新数据包含今日
    today = datetime.now().strftime('%Y-%m-%d')
    assert today in set(result['date'].astype(str))
