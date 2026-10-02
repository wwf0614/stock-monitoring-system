# -*- coding: utf-8 -*-
"""forecast_track.py —— 预测命中率记录与校准

解决的核心问题：
  forecast.py 输出的「置信度」目前是统计口径的启发式打分，并不是真实概率。
  没有事后回填，谁也不知道它到底准不准——用户看到的只是一个好看的数字。

本模块做三件事：
  1) 记录(record_forecast)：每次量化研判落盘到 forecast_history.json，
     同时记下「做预测那一刻」的核心指数锚点价格，作为事后比对的基准。
  2) 回填(settle)：下一个交易日数据出来后，用锚点 vs 次日收盘算实际涨跌幅，
     判定该次预测是「命中 / 部分 / 落空」。
  3) 统计(build_stats)：滚动命中率、按方向分类命中率、置信度分档校准曲线、
     Brier score、仓位建议有效性——用来校准模型，而不是盲信它。

设计要点：
  - 按 (预测所属交易日, lookback) 去重：前端会反复调用 forecast_market，
    不能每次都写一条。同一天同一窗口只保留最新研判，另存当日首次快照(open)，
    可用于对比「早盘研判 vs 收盘前研判」哪个更准。
  - 锚点用「预测时的最新 K 线收盘价」，回填时找该日期之后的第一根 K 线。
    这样即使预测是在盘中发出的(锚点是当日实时价)，算出的也是
    「从预测发出到次日收盘」的真实收益——正是我们想检验的东西。
  - 全部读写走 json_util 的原子写 + 安全读，进程被杀也不会损坏历史。
  - 任何异常都不允许影响 forecast 主流程（记录失败只是少一条样本）。

对外：
  record_forecast(result)          -> bool   落盘一条/更新一条预测
  settle_pending(force=False)      -> dict   回填所有已可判定的预测
  build_stats()                    -> dict   命中率与校准统计
  load_records()                   -> list   原始记录(供调试/导出)

命令行：
  python forecast_track.py --settle   回填
  python forecast_track.py --stats    打印统计
"""
import os
import time
import threading
import datetime

try:
    import data_fetcher as df
except Exception:
    df = None

try:
    import json_util
except Exception:
    json_util = None

_BASE = os.path.dirname(os.path.abspath(__file__))
_HISTORY_PATH = os.path.join(_BASE, "forecast_history.json")

_LOCK = threading.Lock()

# 同一 key 的最小写盘间隔(秒)；前端高频刷新时不重复落盘
_WRITE_THROTTLE = 120
# 最多保留多少条记录(超出丢弃最旧的)，避免文件无限膨胀
_MAX_RECORDS = 800
# 回填时最多处理多少条未回填记录
_SETTLE_BATCH = 30

# 判定阈值(%)：A股主要指数日波动标准差约 1%，用 ±0.3% 作为"平盘带"，
# 避免把 0.01% 的噪声涨跌当成一次成功/失败的方向预测。
_FLAT_BAND = 0.30
# side(震荡)判定的容差：比方向判定更宽，因为"预测震荡"本身就是预测小波动
_SIDE_FULL = 0.50
_SIDE_HALF = 1.20

# 用于事后检验的"主指数"：与 forecast._build_view 的 core 选择保持一致
_CORE_ORDER = ("沪深300", "上证指数", "创业板指", "深证成指", "中证500")

# 置信度分档：检验"我说 70% 把握的时候，是不是真的约 70% 命中"
_CONF_BUCKETS = ((35, 50), (50, 58), (58, 66), (66, 100))


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _load_raw():
    if json_util:
        data = json_util.safe_load_json(_HISTORY_PATH, default={})
    else:
        try:
            import json
            with open(_HISTORY_PATH, "r", encoding="utf-8") as f:
                import json as _j
                data = _j.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    recs = data.get("records")
    if not isinstance(recs, list):
        recs = []
    return recs


def _save_raw(recs):
    payload = {
        "version": 2,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "records": recs,
    }
    if json_util:
        json_util.atomic_write_json(_HISTORY_PATH, payload)
    else:
        import json
        tmp = _HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _HISTORY_PATH)


def load_records():
    """读取全部预测记录(最新在最后)。"""
    with _LOCK:
        return _load_raw()


def reset_history():
    """清空全部预测历史。

    注意：json_util.safe_load_json 在主文件缺失时会回退到 .bak，
    所以只删主文件是清不掉的——必须把 .bak 一起删，否则历史会"复活"。
    """
    removed = []
    for p in (_HISTORY_PATH, _HISTORY_PATH + ".bak"):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# 1) 记录
# ---------------------------------------------------------------------------
def _pick_core_index(result):
    """从研判结果里挑一个主指数作为事后检验基准，返回 (name, secid, last, date)。"""
    best = None
    for it in (result.get("indices") or []):
        nm = it.get("name")
        try:
            rank = _CORE_ORDER.index(nm)
        except (ValueError, AttributeError):
            continue
        if best is None or rank < best[0]:
            best = (rank, it)
    if not best:
        return None
    it = best[1]
    last = it.get("last")
    date = it.get("last_date")
    if not last or not date:
        return None
    return (it.get("name"), it.get("secid"), float(last), str(date))


def _snapshot(market, core):
    """一条研判快照：方向/置信度/仓位 + 锚点价格。"""
    snap = {
        "direction": market.get("direction"),
        "confidence": market.get("confidence"),
        "composite": market.get("composite_score"),
        "position": market.get("suggested_position"),
        "regime_label": market.get("regime_label"),
        "regime_type": market.get("regime_type"),
        "vol_annual": market.get("vol_annual"),
        "eff_lookback": market.get("eff_lookback"),
    }
    if core:
        snap["core_name"] = core[0]
        snap["core_secid"] = core[1]
        snap["anchor_price"] = round(core[2], 4)
        snap["anchor_date"] = core[3]
    return snap


def record_forecast(result):
    """把一次 forecast_market 的结果落盘。失败返回 False，绝不抛异常给调用方。"""
    try:
        if not result or not result.get("ok"):
            return False
        market = result.get("market") or {}
        direction = market.get("direction")
        if direction not in ("up", "down", "side"):
            return False
        core = _pick_core_index(result)
        if not core:
            return False           # 没有可用锚点，这条无法事后检验，不记录

        lookback = int(result.get("lookback") or 20)
        anchor_date = str(core[3])
        key = "%s|%d" % (anchor_date, lookback)
        now_txt = time.strftime("%Y-%m-%d %H:%M")

        with _LOCK:
            recs = _load_raw()
            idx = None
            for i, r in enumerate(recs):
                if r.get("key") == key:
                    idx = i
                    break
            snap = _snapshot(market, core)

            if idx is not None:
                rec = recs[idx]
                rec["samples"] = int(rec.get("samples") or 0) + 1
                # 值没实质变化时做节流，避免高频刷新反复写盘
                same = (rec.get("direction") == snap["direction"]
                        and rec.get("confidence") == snap["confidence"]
                        and rec.get("position") == snap["position"])
                if same and not _throttle_ok(rec, now_txt):
                    return False
                rec.update(snap)
                rec["updated_at"] = now_txt
                # 已回填过的记录若被新数据更新(理论不该发生)，保留原回填结果
            else:
                rec = {
                    "key": key,
                    "date": anchor_date,
                    "lookback": lookback,
                    "first_at": now_txt,
                    "updated_at": now_txt,
                    "samples": 1,
                    "actual": None,
                    "hit": None,
                    "hit_open": None,
                    "grade": None,
                }
                rec.update(snap)
                rec["open"] = {
                    "at": now_txt,
                    "direction": snap["direction"],
                    "confidence": snap["confidence"],
                    "position": snap["position"],
                }
                recs.append(rec)

            recs.sort(key=lambda r: str(r.get("date") or ""))
            if len(recs) > _MAX_RECORDS:
                recs = recs[-_MAX_RECORDS:]
            _save_raw(recs)
            return True
    except Exception:
        return False


def _throttle_ok(rec, now_txt):
    """距上次写盘超过 _WRITE_THROTTLE 秒才允许再次写。"""
    prev = rec.get("updated_at") or rec.get("first_at")
    if not prev:
        return True
    try:
        p = datetime.datetime.strptime(str(prev), "%Y-%m-%d %H:%M")
        n = datetime.datetime.strptime(now_txt, "%Y-%m-%d %H:%M")
        return abs((n - p).total_seconds()) >= _WRITE_THROTTLE
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 1b) 基金级预测记录（kind='fund'）：与大盘研判同一套回填/统计口径，
#     使「宽基指数命中率」之外，还能单独检验「基金级预测」准不准。
# ---------------------------------------------------------------------------
def record_fund_forecast(result):
    """落盘一条基金级预测(kind='fund')。失败返回 False，绝不抛异常给调用方。"""
    try:
        if not result or not result.get("ok"):
            return False
        code = str(result.get("code") or "")
        direction = result.get("direction")
        if not code or direction not in ("up", "down", "side"):
            return False
        anchor_date = str(result.get("as_of") or "")
        anchor_price = result.get("last_nav")
        if not anchor_date or not anchor_price:
            return False
        key = "fund:%s|%s" % (code, anchor_date)      # 同基金同锚点日只留一条
        now_txt = time.strftime("%Y-%m-%d %H:%M")
        with _LOCK:
            recs = _load_raw()
            idx = None
            for i, r in enumerate(recs):
                if r.get("key") == key:
                    idx = i
                    break
            snap = {
                "kind": "fund", "code": code, "name": result.get("name"),
                "direction": direction, "confidence": result.get("confidence"),
                "composite": result.get("score"), "position": result.get("suggest_position"),
                "regime_label": result.get("regime"), "regime_type": result.get("regime"),
                "vol_annual": result.get("vol_annual"),
                "exp_pct": result.get("exp_pct"), "low_pct": result.get("low_pct"),
                "high_pct": result.get("high_pct"), "prob_up": result.get("prob_up"),
                "horizon": result.get("horizon"),
                "core_name": result.get("name"), "core_secid": None,
                "anchor_price": round(float(anchor_price), 4), "anchor_date": anchor_date,
            }
            if idx is not None:
                rec = recs[idx]
                rec["samples"] = int(rec.get("samples") or 0) + 1
                same = (rec.get("direction") == direction
                        and rec.get("confidence") == snap["confidence"])
                if same and not _throttle_ok(rec, now_txt):
                    return False
                rec.update(snap)
                rec["updated_at"] = now_txt
            else:
                rec = {"key": key, "date": anchor_date,
                       "lookback": int(result.get("horizon") or 5),
                       "first_at": now_txt, "updated_at": now_txt, "samples": 1,
                       "actual": None, "hit": None, "hit_open": None, "grade": None}
                rec.update(snap)
                rec["open"] = {"at": now_txt, "direction": direction,
                               "confidence": snap["confidence"], "position": snap["position"]}
                recs.append(rec)
            recs.sort(key=lambda r: str(r.get("date") or ""))
            if len(recs) > _MAX_RECORDS:
                recs = recs[-_MAX_RECORDS:]
            _save_raw(recs)
            return True
    except Exception:
        return False


def _next_nav_after(code, anchor_date, anchor_price):
    """基金净值口径：取锚点日期之后第一条净值的收益(%)，返回 (date, ret%) 或 None。"""
    try:
        import trend as _tr
    except Exception:
        return None
    try:
        hist = _tr.fetch_nav_history(code, days=120) or []   # 升序
    except Exception:
        return None
    for d, v in hist:
        try:
            d = str(d)
            v = float(v)
        except (TypeError, ValueError):
            continue
        if d > anchor_date and v > 0 and anchor_price > 0:
            return (d, round((v / anchor_price - 1.0) * 100.0, 2))
    return None


# ---------------------------------------------------------------------------
# 2) 回填：用事后真实走势给预测打分
# ---------------------------------------------------------------------------
def grade(direction, ret):
    """把预测方向 + 实际涨跌幅 -> (hit, grade)。

    hit ∈ {1.0, 0.5, 0.0}：完全命中 / 部分命中(方向对但幅度不足，或预测震荡但波动略大) / 落空。
    半命中的存在是为了避免样本少时被阈值边界的噪声主导。
    """
    if ret is None:
        return None, None
    try:
        ret = float(ret)
    except (TypeError, ValueError):
        return None, None
    if direction == "up":
        if ret >= _FLAT_BAND:
            h = 1.0
        elif ret > -_FLAT_BAND:
            h = 0.5
        else:
            h = 0.0
    elif direction == "down":
        if ret <= -_FLAT_BAND:
            h = 1.0
        elif ret < _FLAT_BAND:
            h = 0.5
        else:
            h = 0.0
    else:  # side
        a = abs(ret)
        if a <= _SIDE_FULL:
            h = 1.0
        elif a <= _SIDE_HALF:
            h = 0.5
        else:
            h = 0.0
    if h == 1.0:
        g = "命中"
    elif h == 0.5:
        g = "部分"
    else:
        g = "反向" if ((direction == "up" and ret <= -1.0)
                       or (direction == "down" and ret >= 1.0)) else "落空"
    return h, g


def _next_bar_after(secid, anchor_date, anchor_price):
    """取锚点日期之后的第一根日K收盘，返回 (date, ret%) 或 None。"""
    if not df or not secid or not anchor_date:
        return None
    try:
        series = df.fetch_kline(secid, lmt=12) or []
    except Exception:
        return None
    for d, c in series:
        try:
            d = str(d)
            c = float(c)
        except (TypeError, ValueError):
            continue
        if d > anchor_date and c > 0 and anchor_price > 0:
            return (d, round((c / anchor_price - 1.0) * 100.0, 2))
    return None


def settle_pending(force=False):
    """回填所有已可判定的预测。

    force=True 时连今天刚记录(尚未有次日K线)的也尝试一次(用于手动调试)。
    返回 {'checked': n, 'settled': n, 'pending': n}。
    """
    out = {"checked": 0, "settled": 0, "pending": 0}
    if df is None:
        out["reason"] = "data_fetcher 不可用"
        return out
    today = time.strftime("%Y-%m-%d")
    with _LOCK:
        recs = _load_raw()
        todo = [r for r in recs if r.get("actual") is None and r.get("anchor_date")]
        if not force:
            todo = [r for r in todo if str(r.get("anchor_date")) < today]
        todo = todo[-_SETTLE_BATCH:]
        if not todo:
            out["pending"] = len([r for r in recs if r.get("actual") is None])
            return out

        cache = {}   # secid+anchor_date -> (next_date, ret)
        changed = False
        for rec in recs:
            if rec.get("actual") is not None:
                continue
            ad = rec.get("anchor_date")
            if not ad:
                continue
            if not force and str(ad) >= today:
                continue
            out["checked"] += 1
            if rec.get("kind") == "fund":
                ck = ("fund", rec.get("code"), str(ad), rec.get("anchor_price"))
                if ck not in cache:
                    cache[ck] = _next_nav_after(rec.get("code"), str(ad),
                                                rec.get("anchor_price") or 0)
            else:
                ck = (rec.get("core_secid"), str(ad), rec.get("anchor_price"))
                if ck not in cache:
                    cache[ck] = _next_bar_after(rec.get("core_secid"), str(ad),
                                                rec.get("anchor_price") or 0)
            got = cache[ck]
            if not got:
                continue
            d, ret = got
            rec["actual"] = {
                "date": d,
                "ret": ret,
                "core": rec.get("core_name"),
            }
            h, g = grade(rec.get("direction"), ret)
            rec["hit"] = h
            rec["grade"] = g
            od = (rec.get("open") or {}).get("direction")
            if od:
                ho, _go = grade(od, ret)
                rec["hit_open"] = ho
            changed = True
            out["settled"] += 1

        if changed:
            _save_raw(recs)
        out["pending"] = len([r for r in recs if r.get("actual") is None])
    return out


# ---------------------------------------------------------------------------
# 3) 统计与校准
# ---------------------------------------------------------------------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _round1(x):
    return None if x is None else round(x, 1)


def _pearson(xs, ys):
    """皮尔逊相关系数；样本 <5 或方差为 0 时返回 None。"""
    n = len(xs)
    if n < 5 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def build_stats():
    """命中率 / 校准 / Brier / 仓位有效性统计。样本不足时给出明确文案而非编数字。"""
    recs = load_records()
    done = [r for r in recs if r.get("actual") and r.get("hit") is not None]
    total = len(recs)
    settled = len(done)
    pending = total - settled

    res = {
        "ok": True,
        "total": total,
        "settled": settled,
        "pending": pending,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    if settled < 3:
        res["ready"] = False
        res["note"] = ("已记录 %d 次研判，其中 %d 次已有事后结果。"
                       "样本达到 20 次以上，命中率与校准曲线才有统计意义。"
                       % (total, settled))
        res["recent"] = _recent_list(recs, 8)
        return res

    hits = [float(r["hit"]) for r in done]
    res["ready"] = True
    res["hit_rate"] = _round1(sum(hits) / len(hits) * 100.0)
    res["strict_hit_rate"] = _round1(
        sum(1 for h in hits if h >= 1.0) / len(hits) * 100.0)
    res["partial_rate"] = _round1(
        sum(1 for h in hits if h == 0.5) / len(hits) * 100.0)

    # —— 按方向分类：多数模型是"看多容易、看空难"，分类后能看出偏误 ——
    by_dir = {}
    for d in ("up", "down", "side"):
        sub = [r for r in done if r.get("direction") == d]
        if not sub:
            continue
        by_dir[d] = {
            "n": len(sub),
            "hit_rate": _round1(sum(float(r["hit"]) for r in sub) / len(sub) * 100.0),
            "avg_ret": _round1(_avg([(r.get("actual") or {}).get("ret") for r in sub])),
        }
    res["by_direction"] = by_dir

    # —— Brier score：把置信度直接当方向概率，与真实涨跌二元结果比对 ——
    # 0.25 = "永远猜 50%" 的基准线；低于 0.25 说明置信度含有真实信息。
    bs = []
    for r in done:
        conf = r.get("confidence")
        ret = (r.get("actual") or {}).get("ret")
        if conf is None or ret is None:
            continue
        p = float(conf) / 100.0
        if r.get("direction") == "down":
            p = 1.0 - p
        elif r.get("direction") == "side":
            p = 0.5
        y = 1.0 if float(ret) > 0 else 0.0
        bs.append((p - y) ** 2)
    res["brier"] = round(_avg(bs), 4) if bs else None
    res["brier_n"] = len(bs)

    # —— 置信度校准曲线：说 70% 的时候，真的有 70% 命中吗 ——
    cal = []
    for lo, hi in _CONF_BUCKETS:
        sub = [r for r in done
               if r.get("confidence") is not None and lo <= float(r["confidence"]) < hi]
        if not sub:
            cal.append({"bucket": "%d-%d" % (lo, hi), "n": 0,
                        "avg_conf": None, "real": None})
            continue
        cal.append({
            "bucket": "%d-%d" % (lo, hi),
            "n": len(sub),
            "avg_conf": _round1(_avg([float(r["confidence"]) for r in sub])),
            "real": _round1(sum(float(r["hit"]) for r in sub) / len(sub) * 100.0),
        })
    res["calibration"] = cal

    # —— 仓位建议有效性：把样本按建议仓位中位数分成高/低两组，
    #    比较次日实际收益。低仓位组收益明显更差 → 降仓建议确实躲过了下跌。
    pos_ret = [(float(r.get("position") or 0),
                (r.get("actual") or {}).get("ret"))
               for r in done
               if r.get("position") is not None
               and (r.get("actual") or {}).get("ret") is not None]
    if len(pos_ret) >= 8:
        pos_ret.sort(key=lambda x: x[0])
        half = len(pos_ret) // 2
        lo_g, hi_g = pos_ret[:half], pos_ret[half:]
        res["position_edge"] = {
            "n": len(pos_ret),
            "low_pos_avg": _round1(_avg([p for p, _ in lo_g])),
            "low_next_ret": _round1(_avg([r for _, r in lo_g])),
            "high_pos_avg": _round1(_avg([p for p, _ in hi_g])),
            "high_next_ret": _round1(_avg([r for _, r in hi_g])),
        }
        res["position_ic"] = (round(_pearson([p for p, _ in pos_ret],
                                             [r for _, r in pos_ret]), 3)
                              if _pearson([p for p, _ in pos_ret],
                                          [r for _, r in pos_ret]) is not None else None)
    else:
        res["position_edge"] = None

    # —— 早盘 vs 收盘研判：同样的模型，哪个时点更可信 ——
    op = [r for r in done if r.get("hit_open") is not None]
    if len(op) >= 5:
        res["timing"] = {
            "n": len(op),
            "open_hit": _round1(sum(float(r["hit_open"]) for r in op) / len(op) * 100.0),
            "close_hit": _round1(sum(float(r["hit"]) for r in op) / len(op) * 100.0),
        }

    res["recent"] = _recent_list(recs, 10)
    res["note"] = ("命中=方向对且幅度超%.2f%%，部分=方向对但幅度不足；"
                   "Brier<0.25 说明置信度含真实信息。"
                   % _FLAT_BAND)
    return res


# ---------------------------------------------------------------------------
# 3b) 反向重校准：用实测命中率反过来调模型参数（自学习，有界、保守）
# ---------------------------------------------------------------------------
_TUNE_CACHE = {"ts": 0.0, "res": None}
_TUNE_TTL = 1800.0
_TUNE_MIN_N = 20


def tuned_params(use_cache=True):
    """历史反向重校准：用实测命中率对模型关键参数做小幅、有界调整。

    只调 3 个旋钮(不改模型结构)，且有样本下限与边界保护，避免过拟合噪声：
      base_position : 整体命中率 >58% 上调至 60；<45% 下调至 50   (默认 55)
      target_vol    : 高波动样本(年化>=20%)命中率<50% -> 上调至 18(更保守降仓)
      conf_scale    : 高置信档(>=66)实测命中率显著低于宣称 -> 压缩至 0.9
    样本 <20 或数据不足时 applied=False，返回默认参数(等同不调整)。
    返回 {applied, n, params:{base_position,target_vol,conf_scale}, note}
    """
    now = time.time()
    if use_cache and _TUNE_CACHE["res"] and (now - _TUNE_CACHE["ts"]) < _TUNE_TTL:
        return _TUNE_CACHE["res"]
    params = {"base_position": 55, "target_vol": 15.0, "conf_scale": 1.0}
    out = {"applied": False, "n": 0, "params": params,
           "note": "样本不足(需≥%d)，沿用默认参数" % _TUNE_MIN_N}
    try:
        recs = load_records()
        done = [r for r in recs if r.get("actual") and r.get("hit") is not None]
        out["n"] = len(done)
        if len(done) >= _TUNE_MIN_N:
            hits = [float(r["hit"]) for r in done]
            hr = sum(hits) / len(hits) * 100.0
            base = 60 if hr >= 58 else (50 if hr <= 45 else 55)
            tvol = 15.0
            hv = [r for r in done if isinstance(r.get("vol_annual"), (int, float))
                  and r["vol_annual"] >= 20]
            if len(hv) >= 8:
                hv_hr = sum(float(r["hit"]) for r in hv) / len(hv) * 100.0
                if hv_hr < 50:
                    tvol = 18.0
            cscale = 1.0
            hi = [r for r in done if r.get("confidence") is not None
                  and float(r["confidence"]) >= 66]
            if len(hi) >= 8:
                hi_hr = sum(float(r["hit"]) for r in hi) / len(hi) * 100.0
                hi_conf = sum(float(r["confidence"]) for r in hi) / len(hi)
                if hi_hr < hi_conf * 0.85:
                    cscale = 0.9
            params.update({"base_position": base, "target_vol": tvol, "conf_scale": cscale})
            out["applied"] = (base != 55 or tvol != 15.0 or cscale != 1.0)
            out["hit_rate"] = _round1(hr)
            out["note"] = ("基于 %d 条实测(命中率 %.1f%%)：基准仓位 %d、目标波动 %.0f%%、置信度系数 %.2f"
                           % (len(done), hr, base, tvol, cscale))
    except Exception:
        pass
    _TUNE_CACHE["ts"] = now
    _TUNE_CACHE["res"] = out
    return out


def _recent_list(recs, n=10):
    out = []
    for r in [x for x in recs if x.get("actual")][-n:][::-1]:
        out.append({
            "date": r.get("date"),
            "actual_date": (r.get("actual") or {}).get("date"),
            "direction": r.get("direction"),
            "confidence": r.get("confidence"),
            "position": r.get("position"),
            "ret": (r.get("actual") or {}).get("ret"),
            "hit": r.get("hit"),
            "grade": r.get("grade"),
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli():
    import sys
    args = sys.argv[1:]
    if "--settle" in args:
        r = settle_pending(force=("--force" in args))
        print("[回填] 检查 %d 条，新回填 %d 条，仍待定 %d 条"
              % (r.get("checked", 0), r.get("settled", 0), r.get("pending", 0)))
        if r.get("reason"):
            print("  " + r["reason"])
        for rec in load_records()[-8:]:
            a = rec.get("actual")
            if not a:
                print("  %s %s 待定" % (rec.get("date"), rec.get("direction")))
            else:
                print("  %s 预测%s 实际%+.2f%% -> %s"
                      % (rec.get("date"), rec.get("direction"),
                         a.get("ret") or 0, rec.get("grade")))
        return
    if "--stats" in args:
        import json
        st = build_stats()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    if "--reset" in args:
        rm = reset_history()
        print("[清空] 已删除: %s" % (", ".join(rm) if rm else "无(本来就没有历史文件)"))
        print("  注：预测历史是命中率的唯一依据，清空后需重新积累，无法恢复。")
        return
    print(__doc__)


if __name__ == "__main__":
    _cli()
