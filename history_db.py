# -*- coding: utf-8 -*-
"""
history_db.py —— 量化历史库（sqlite）
===================================
解决的核心问题：
  系统所有历史数据都是「每次现抓近 60~90 根」，本地不落盘。回测、因子、风险模型
  都需要任意区间的历史，且一旦上游接口改版或被限流，没落的数据就永久缺失。
  本模块把 akshare_provider 取到的数据固化成可查询的表，是回测引擎的地基。

选型：sqlite（标准库自带，零新依赖；单文件、事务、索引齐全，够用几年）。
  - WAL 模式：夜间 ETL 写库时，白天的看板读取不会被阻塞。
  - 全部用 INSERT OR REPLACE：重复跑同一天不产生重复行，ETL 可无限次重跑（幂等）。
  - 窄表(bar_daily / fund_nav)用强类型列——回测每时每刻都在读它们；
    宽表/易变表(财务、横截面)存 JSON payload——上游改列名不会把库搞坏。

对外：
  connect(path) -> conn      打开(自动建表)
  init_schema(conn)          建表建索引
  各 upsert_*()  返回写入行数
  watermark(conn, step) / set_watermark(conn, step, value)
  load_bars(conn, codes, start, end)   回测取数入口
  summary(conn)              各表行数与区间，供自检打印
"""
import os
import json
import sqlite3
import threading

_SCHEMA_VERSION = "1"
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS trade_calendar (
    date TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS instrument (
    code TEXT PRIMARY KEY, name TEXT, kind TEXT, exchange TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS bar_daily (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, turnover REAL,
    adjust TEXT NOT NULL DEFAULT 'qfq',
    PRIMARY KEY (code, date, adjust)
);
CREATE INDEX IF NOT EXISTS ix_bar_date ON bar_daily(date);
CREATE TABLE IF NOT EXISTS fund_nav (
    code TEXT NOT NULL, date TEXT NOT NULL,
    nav REAL, acc_nav REAL, pct REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS ix_nav_date ON fund_nav(date);
CREATE TABLE IF NOT EXISTS board (
    kind TEXT NOT NULL, name TEXT NOT NULL, code TEXT,
    PRIMARY KEY (kind, name)
);
CREATE TABLE IF NOT EXISTS index_weight (
    index_code TEXT NOT NULL, date TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT, weight REAL,
    PRIMARY KEY (index_code, date, code)
);
CREATE TABLE IF NOT EXISTS index_valuation (
    index_code TEXT NOT NULL, date TEXT NOT NULL,
    close REAL, pe REAL, amount REAL, sample_count REAL,
    PRIMARY KEY (index_code, date)
);
CREATE TABLE IF NOT EXISTS fin_indicator (
    code TEXT NOT NULL, date TEXT NOT NULL, payload TEXT,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS fin_abstract (
    code TEXT NOT NULL, period TEXT NOT NULL, payload TEXT,
    PRIMARY KEY (code, period)
);
CREATE TABLE IF NOT EXISTS spot_snapshot (
    date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    price REAL, pct REAL, payload TEXT,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS margin_detail (
    date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    rzye REAL, payload TEXT,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS board_flow_daily (
    date TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
    pct REAL, main_net REAL, xl_net REAL, big_net REAL, mid_net REAL, small_net REAL,
    settled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, kind, name)
);
CREATE INDEX IF NOT EXISTS ix_flow_name ON board_flow_daily(kind, name, date);
"""


def default_path():
    return _DB_PATH


def connect(path=None):
    """打开库并建表（IF NOT EXISTS，重复调用安全）。"""
    conn = sqlite3.connect(path or _DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn):
    with _write_lock:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                     (_SCHEMA_VERSION,))
        conn.commit()


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(row, *names):
    """按候选列名依次取值（上游中文列名会随版本变，这里容错）。"""
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def upsert_bars(conn, code, rows, adjust="qfq", kind=None):
    """写入日线。rows 为 akshare_provider 返回的 list[dict]。返回写入行数。"""
    if not rows:
        return 0
    rec = [(code, r.get("date") or r.get("日期"),
            _num(_pick(r, "open", "开盘")), _num(_pick(r, "high", "最高")),
            _num(_pick(r, "low", "最低")), _num(_pick(r, "close", "收盘", "最新价")),
            _num(_pick(r, "volume", "成交量")), _num(_pick(r, "amount", "成交额")),
            _num(_pick(r, "turnover", "换手率")), adjust)
           for r in rows if (r.get("date") or r.get("日期"))]
    rec = [x for x in rec if x[1] and x[5] is not None]
    if not rec:
        return 0
    if kind:
        register_instrument(conn, code, kind=kind)
    with _write_lock:
        conn.executemany(
            "INSERT OR REPLACE INTO bar_daily"
            "(code,date,open,high,low,close,volume,amount,turnover,adjust)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rec)
        conn.commit()
    return len(rec)


def upsert_fund_nav(conn, code, rows):
    """写入场外基金净值。列名为中文（f10 原样）。"""
    if not rows:
        return 0
    rec = []
    for r in rows:
        d = r.get("净值日期") or r.get("date")
        if not d:
            continue
        rec.append((code, d, _num(r.get("单位净值")), _num(r.get("累计净值")),
                    _num(r.get("日增长率"))))
    if not rec:
        return 0
    register_instrument(conn, code, kind="fund")
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO fund_nav(code,date,nav,acc_nav,pct) VALUES (?,?,?,?,?)", rec)
        conn.commit()
    return len(rec)


def upsert_spot(conn, date_str, rows):
    """写入全市场横截面快照。宽表存 payload，上游改列不破坏库。"""
    if not rows:
        return 0
    rec = []
    for r in rows:
        code = r.get("代码") or r.get("code")
        if not code:
            continue
        rec.append((date_str, code, r.get("名称"), _num(r.get("最新价")),
                    _num(r.get("涨跌幅")), json.dumps(r, ensure_ascii=False)))
    if not rec:
        return 0
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO spot_snapshot(date,code,name,price,pct,payload)"
                         " VALUES (?,?,?,?,?,?)", rec)
        conn.commit()
    return len(rec)


def upsert_index_weight(conn, index_code, rows):
    if not rows:
        return 0
    rec = []
    for r in rows:
        code = r.get("成分券代码") or r.get("code")
        d = r.get("日期") or r.get("date")
        if not code or not d:
            continue
        rec.append((index_code, d, code, r.get("成分券名称"), _num(r.get("权重"))))
    if not rec:
        return 0
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO index_weight(index_code,date,code,name,weight)"
                         " VALUES (?,?,?,?,?)", rec)
        for c, n in {(x[2], x[3]) for x in rec}:
            conn.execute("INSERT OR IGNORE INTO instrument(code,name,kind) VALUES(?,?,?)",
                         (c, n, "stock"))
        conn.commit()
    return len(rec)


def upsert_index_valuation(conn, index_code, rows):
    if not rows:
        return 0
    rec = []
    for r in rows:
        d = r.get("日期") or r.get("date")
        if not d:
            continue
        rec.append((index_code, d, _num(_pick(r, "close", "收盘", "收盘价")),
                    _num(_pick(r, "滚动市盈率", "pe", "市盈率(动态)")),
                    _num(_pick(r, "成交金额", "amount")), _num(r.get("样本数量"))))
    if not rec:
        return 0
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO index_valuation"
                         "(index_code,date,close,pe,amount,sample_count) VALUES (?,?,?,?,?,?)", rec)
        conn.commit()
    return len(rec)


def upsert_json_rows(conn, table, pk_cols, code, rows, date_keys=("日期", "报告期", "date")):
    """通用：把每条记录连同 code + 日期存成 JSON 行。用于财务等宽表。"""
    if not rows:
        return 0
    rec = []
    for r in rows:
        d = None
        for k in date_keys:
            if r.get(k):
                d = r[k]
                break
        if not d:
            continue
        vals = [code, d, json.dumps(r, ensure_ascii=False)]
        rec.append(tuple(vals))
    if not rec:
        return 0
    cols = ",".join(["code", pk_cols[1], "payload"])
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO %s(%s) VALUES (?,?,?)" % (table, cols), rec)
        conn.commit()
    return len(rec)


def upsert_boards(conn, kind, rows):
    if not rows:
        return 0
    rec = [(kind, r.get("name"), r.get("code")) for r in rows if r.get("name")]
    if not rec:
        return 0
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO board(kind,name,code) VALUES (?,?,?)", rec)
        conn.commit()
    return len(rec)


def upsert_calendar(conn, dates):
    if not dates:
        return 0
    with _write_lock:
        conn.executemany("INSERT OR REPLACE INTO trade_calendar(date) VALUES (?)",
                         [(d,) for d in dates])
        conn.commit()
    return len(dates)


def upsert_board_flow(conn, date_str, kind, rows, settled=False):
    """落一条当日板块资金快照，供「连续 N 日净流入」信号使用。

    为什么需要它：rotation_state.json 只保留最后一个快照，做不出多日累计。
    写入规则（自愈式）：收盘后(settled=1)的值一旦落定，就不再被同日更早的盘中值覆盖；
    收盘前的值允许被同日更新的盘中值刷新，所以页面每次访问都可以安全调用。
    """
    if not rows:
        return 0
    rec = []
    for x in rows:
        nm = x.get("name")
        if not nm:
            continue
        rec.append((date_str, kind, nm, _num(x.get("pct")), _num(x.get("main_net")),
                    _num(x.get("inst_net")), _num(x.get("big_net")),
                    _num(x.get("mid_net")), _num(x.get("small_net")), 1 if settled else 0))
    if not rec:
        return 0
    sql = ("INSERT INTO board_flow_daily"
           "(date,kind,name,pct,main_net,xl_net,big_net,mid_net,small_net,settled)"
           " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,kind,name) DO UPDATE SET"
           " pct=excluded.pct, main_net=excluded.main_net, xl_net=excluded.xl_net,"
           " big_net=excluded.big_net, mid_net=excluded.mid_net,"
           " small_net=excluded.small_net, settled=excluded.settled"
           + ("" if settled else " WHERE board_flow_daily.settled=0"))
    with _write_lock:
        conn.executemany(sql, rec)
        conn.commit()
    return len(rec)


def board_flow_days(conn, settled_only=True, exclude_date=None):
    """已积累的交易日数（信号据此判断多日口径能不能用）。

    exclude_date：当日通常由实时快照补齐而非历史序列，若把它算进"已积累"会虚高一天。
    """
    q = "SELECT COUNT(DISTINCT date) n FROM board_flow_daily WHERE 1=1"
    args = []
    if settled_only:
        q += " AND settled=1"
    if exclude_date:
        q += " AND date<>?"
        args.append(exclude_date)
    r = conn.execute(q, args).fetchone()
    return r["n"] if r else 0


def board_flow_series(conn, kind, name, limit=12, settled_only=True):
    """某板块最近 N 个交易日资金流，按日期升序。找不到返回 []。"""
    q = ("SELECT date,pct,main_net,xl_net,big_net,mid_net,small_net FROM board_flow_daily "
         "WHERE kind=? AND name=?")
    args = [kind, name]
    if settled_only:
        q += " AND settled=1"
    q += " ORDER BY date DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in conn.execute(q, args)]
    rows.reverse()
    return rows


def board_flow_recent(conn, limit_days=6, settled_only=True):
    """最近 N 个交易日的**全市场**板块资金，按板块名归组（组内按日期升序）。

    为什么要批量：买入侧要按「多日累计净流入」在全市场里挑趋势板块，逐个板块调
    board_flow_series 就是 500~1000 次查询；这里一次取回再在内存里分组。
    只认 settled=1 —— 盘中半成品不是"一天的收盘值"，混进窗口会把趋势算歪
    （与 board_flow_latest_settled_date 同一理由）。
    """
    q = ("SELECT date,kind,name,pct,main_net,xl_net,big_net,mid_net,small_net "
         "FROM board_flow_daily WHERE date IN (SELECT DISTINCT date FROM board_flow_daily")
    if settled_only:
        q += " WHERE settled=1"
    q += " ORDER BY date DESC LIMIT ?)"
    args = [int(limit_days)]
    if settled_only:
        q += " AND settled=1"
    q += " ORDER BY name, date"
    out = {}
    for r in conn.execute(q, args):
        out.setdefault(r["name"], []).append(dict(r))
    return out


def board_flow_latest_date(conn):
    r = conn.execute("SELECT MAX(date) d FROM board_flow_daily").fetchone()
    return r["d"] if r else None


def board_flow_latest_settled_date(conn):
    """最近一个「已收盘定型」的交易日。

    降级展示只认 settled=1：盘中半成品是不完整的当日快照，拿它当"收盘格局"会误导。
    """
    r = conn.execute("SELECT MAX(date) d FROM board_flow_daily WHERE settled=1").fetchone()
    return r["d"] if r else None


def load_board_flow(conn, date_str, settled_only=True):
    """取某交易日全量板块资金，返回与实时快照同形状的 {"industries": [], "concepts": []}。

    用途：实时源（东方财富 push2）被限流、或盘后拿不到数据时，用最近定型的收盘格局降级展示，
    而不是给用户一片空白（整页提醒会跟着全哑）。

    total_net 表里没有这一列，按「超大单+大单+中单+小单」合成 —— 与 data_fetcher 的
    _fetch_sectors_once（f63+f64+f65+f66）口径**完全一致**，否则同一天的板块在两处会算出不同的量。
    """
    q = ("SELECT kind,name,pct,main_net,xl_net,big_net,mid_net,small_net "
         "FROM board_flow_daily WHERE date=?")
    args = [date_str]
    if settled_only:
        q += " AND settled=1"
    out = {"industries": [], "concepts": []}
    for r in conn.execute(q, args):
        key = {"industry": "industries", "concept": "concepts"}.get(r["kind"])
        if not key or not r["name"]:
            continue
        xl = _num(r["xl_net"]) or 0.0
        big = _num(r["big_net"]) or 0.0
        mid = _num(r["mid_net"]) or 0.0
        small = _num(r["small_net"]) or 0.0
        out[key].append({
            "name": r["name"], "code": None, "pct": _num(r["pct"]) or 0.0,
            "main_net": _num(r["main_net"]) or 0.0, "inst_net": xl,
            "big_net": big, "mid_net": mid, "small_net": small,
            "total_net": xl + big + mid + small, "xl_in": 0.0,
        })
    return out


def last_trade_date(conn, day=None):
    """<= day 的最近交易日（YYYY-MM-DD）。

    用途：周末/节假日/盘前访问时，行情接口返回的是上一交易日的收盘数据，
    归档必须落到那个交易日，不能按自然日写（否则周五数据会被写成周六）。
    日历为空返回 None，由调用方回落自然日。
    """
    q = "SELECT MAX(date) d FROM trade_calendar"
    args = []
    if day:
        q += " WHERE date<=?"
        args.append(day)
    try:
        r = conn.execute(q, args).fetchone()
    except sqlite3.Error:
        return None
    return r["d"] if r and r["d"] else None


def register_instrument(conn, code, name=None, kind=None, exchange=None, note=None):
    with _write_lock:
        conn.execute(
            "INSERT INTO instrument(code,name,kind,exchange,note) VALUES(?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET "
            "name=COALESCE(excluded.name, instrument.name), "
            "kind=COALESCE(excluded.kind, instrument.kind), "
            "exchange=COALESCE(excluded.exchange, instrument.exchange)",
            (code, name, kind, exchange, note))
        conn.commit()


def watermark(conn, step):
    r = conn.execute("SELECT value FROM meta WHERE key=?", ("wm:" + step,)).fetchone()
    return r["value"] if r else None


def set_watermark(conn, step, value):
    with _write_lock:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("wm:" + step, value))
        conn.commit()


def latest_bar_date(conn, code, adjust="qfq"):
    r = conn.execute("SELECT MAX(date) d FROM bar_daily WHERE code=? AND adjust=?",
                     (code, adjust)).fetchone()
    return r["d"] if r else None


def load_bars(conn, codes, start=None, end=None, adjust="qfq"):
    """回测取数入口：{code: [(date, open, high, low, close, volume, amount), ...]} 按日升序。"""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    q = ("SELECT code,date,open,high,low,close,volume,amount FROM bar_daily "
         "WHERE adjust=? AND code IN (%s)" % ",".join("?" * len(codes)))
    args = [adjust] + codes
    if start:
        q += " AND date>=?"
        args.append(start)
    if end:
        q += " AND date<=?"
        args.append(end)
    q += " ORDER BY code,date"
    out = {}
    for r in conn.execute(q, args):
        out.setdefault(r["code"], []).append(
            (r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"]))
    return out


def nav_series(conn, code, start=None, end=None):
    q = "SELECT date,nav,pct FROM fund_nav WHERE code=?"
    args = [code]
    if start:
        q += " AND date>=?"
        args.append(start)
    if end:
        q += " AND date<=?"
        args.append(end)
    return [(r["date"], r["nav"], r["pct"]) for r in conn.execute(q + " ORDER BY date", args)]


def summary(conn):
    """各表行数与日期区间。ETL 自检用。"""
    out = {}
    for t, dc in (("bar_daily", "date"), ("fund_nav", "date"), ("index_weight", "date"),
                  ("index_valuation", "date"), ("spot_snapshot", "date"),
                  ("fin_indicator", "date"), ("fin_abstract", "period"),
                  ("trade_calendar", "date"), ("instrument", None), ("board", None)):
        try:
            if dc:
                r = conn.execute("SELECT COUNT(*) n, MIN(%s) a, MAX(%s) b FROM %s" % (dc, dc, t)).fetchone()
                out[t] = {"rows": r["n"], "from": r["a"], "to": r["b"]}
            else:
                r = conn.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()
                out[t] = {"rows": r["n"]}
        except sqlite3.Error as e:
            out[t] = {"error": str(e)}
    return out
