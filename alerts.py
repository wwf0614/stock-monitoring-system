"""
alerts.py —— A股大盘盯盘系统 · 告警引擎
检测: 指数跌破均线 / 主力资金骤变 / 行业广度崩塌 / 市场格局切换 / 新闻面偏空
输出: 本地落盘(alerts.json) + 返回给仪表盘展示；可选通知渠道:
  - 邮件 SMTP(SSL)
  - 企业微信/钉钉/飞书 机器人 Webhook(通用)
  - Server酱(微信推送)
全部渠道默认关闭，在 config.json 中填写后启用；未配置时仅本地告警。
"""
import os
import json
import time
import smtplib
import re

try:
    import json_util
except Exception:
    json_util = None
from email.mime.text import MIMEText
import urllib.request
import urllib.parse

_BASE = os.path.dirname(os.path.abspath(__file__))
_ALERT_FILE = os.path.join(_BASE, "alerts.json")
_CONFIG_FILE = os.path.join(_BASE, "config.json")
COOLDOWN = 1800  # 同一条告警 30 分钟内不重复外发


def _sig(msg):
    """去掉数字后的稳定签名，作为告警去重键。
    告警文案里嵌有实时点位/金额（每次刷新都会微调），不能直接做键，
    否则每个刷新周期都会被判为"新告警"，导致 fired 无限膨胀且冷却失效。"""
    return re.sub(r"[\d.,]+", "", msg)


def _default_config():
    return {
        "email": {"enabled": False, "smtp_host": "smtp.qq.com", "smtp_port": 465,
                  "user": "", "password": "", "to": ""},
        "webhook": {"enabled": False, "url": ""},
        "serverchan": {"enabled": False, "key": ""},
    }


def load_config():
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 补全缺失键
        d = _default_config()
        for k, v in d.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        cfg = _default_config()
        if json_util is not None:
            json_util.atomic_write_json(_CONFIG_FILE, cfg)
        else:
            try:
                with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return cfg


def load_alerts():
    if json_util is not None:
        return json_util.safe_load_json(
            _ALERT_FILE, default={"items": [], "last_regime": None, "fired": {}})
    try:
        with open(_ALERT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": [], "last_regime": None, "fired": {}}


def save_alerts(d):
    if json_util is not None:
        json_util.atomic_write_json(_ALERT_FILE, d)
        return
    try:
        with open(_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def detect(snap, analysis):
    out = []
    sh = snap.get("sh_index", {})
    close, ma20, ma60 = sh.get("close"), sh.get("ma20"), sh.get("ma60")
    if close and ma60 and close < ma60:
        out.append(("danger", f"上证指数 {close:.0f} 跌破 MA60({ma60:.0f})，中期趋势转弱"))
    elif close and ma20 and close < ma20:
        out.append(("warn", f"上证指数 {close:.0f} 跌破 MA20({ma20:.0f})"))

    net_yi = (snap.get("market_main_net", 0) or 0) / 1e8
    if net_yi < -300:
        out.append(("danger", f"全市场主力净流出 {net_yi:.0f} 亿，资金大幅撤离"))
    elif net_yi < -150:
        out.append(("warn", f"全市场主力净流出 {net_yi:.0f} 亿"))

    b = snap.get("breadth")
    if b is not None and b < 0.25:
        out.append(("warn", f"上涨行业广度仅 {b*100:.0f}%，市场情绪偏弱"))

    if analysis.get("news_label") == "偏利空" and analysis.get("regime") != "risk_off":
        out.append(("warn", "新闻面偏利空，注意追高风险"))

    prev = load_alerts().get("last_regime")
    cur = analysis.get("regime")
    if prev and prev != cur:
        out.append(("info", f"市场格局切换：{prev} → {cur}（{analysis.get('regime_label')}）"))
    return out, cur


def _send_email(cfg, subject, body):
    e = cfg["email"]
    if not e.get("enabled"):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = e["user"]
        msg["To"] = e["to"]
        with smtplib.SMTP_SSL(e["smtp_host"], int(e["smtp_port"]), timeout=10) as s:
            s.login(e["user"], e["password"])
            s.sendmail(e["user"], e["to"].split(","), msg.as_string())
        return True
    except Exception:
        return False


def _send_webhook(cfg, text):
    w = cfg["webhook"]
    if not (w.get("enabled") and w.get("url")):
        return False
    try:
        data = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = urllib.request.Request(w["url"], data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def _send_serverchan(cfg, title, desp):
    s = cfg["serverchan"]
    if not (s.get("enabled") and s.get("key")):
        return False
    try:
        url = f"https://sctapi.ftqq.com/{s['key']}.send"
        data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def run_alerts(snap, analysis):
    """检测+去重+外发+落盘。返回 {active:[{sev,msg}], sent:[...], channels:{email,webhook,serverchan}}"""
    alerts, cur_regime = detect(snap, analysis)
    state = load_alerts()
    fired = state.get("fired", {})
    now = time.time()
    # 清理已过冷却窗口的旧键，避免 fired 随运行时间无限增长（历史修复：曾累积上千个键）
    fired = {k: v for k, v in fired.items() if now - v < COOLDOWN}
    active, sent, ch_ok = [], [], {}
    cfg = load_config()

    for sev, msg in alerts:
        active.append({"sev": sev, "msg": msg})
        key = _sig(msg)  # 用稳定签名去重，而非会随实时数字变化的整句文案
        last = fired.get(key, 0)
        if now - last < COOLDOWN:
            continue  # 冷却中，仅展示不重发
        fired[key] = now
        sent.append({"sev": sev, "msg": msg})

    # 外发
    if sent:
        text = "【A股盯盘预警】\n" + "\n".join(f"[{s['sev']}] {s['msg']}" for s in sent)
        ch_ok["email"] = _send_email(cfg, "A股盯盘预警", text)
        ch_ok["webhook"] = _send_webhook(cfg, text)
        ch_ok["serverchan"] = _send_serverchan(cfg, "A股盯盘预警", text)

    # 落盘(仅保留最近 50 条)
    items = state.get("items", [])
    for a in sent:
        items.insert(0, {"ts": int(now * 1000), "sev": a["sev"], "msg": a["msg"]})
    items = items[:50]
    save_alerts({"items": items, "last_regime": cur_regime, "fired": fired})
    return {"active": active, "sent": sent, "channels": ch_ok,
            "history": items[:10]}


if __name__ == "__main__":
    import data_fetcher as df
    import analysis as an
    snap = df.build_snapshot()
    a = an.compute_analysis(snap)
    res = run_alerts(snap, a)
    print("active:", res["active"])
    print("sent:", res["sent"])
    print("channels:", res["channels"])
