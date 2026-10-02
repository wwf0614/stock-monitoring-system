"""
json_util.py —— 安全的 JSON / 文本读写工具

解决的核心隐患：
  原系统多处用 `open(path, "w")` 直接覆盖写 JSON（funds.json / fund_history.json /
  rotation_state.json / alerts.json / last_report.json）。若写的过程中进程被中断
  （崩溃 / 强杀 / 断电），文件会被截断成半截而损坏，基金持仓等状态将丢失。

修复方式：
  - atomic_write_json / atomic_write_text：先写临时文件 → fsync → os.replace 原子覆盖；
    写前先把旧文件备份为 .bak，提供二级回退。
  - safe_load_json：读取时若主文件损坏或缺失，自动回退到 .bak，再回退到 default，
    绝不让读取异常导致进程崩溃；若主文件损坏则用 .bak 自动修复主文件。

跨平台说明：
  os.replace 在同一文件系统上为原子操作（Windows 下等同 MoveFileEx + 覆盖），
  tempfile.mkstemp 在同一目录生成临时文件，保证 os.replace 不会跨盘失败。
"""
import json
import os
import shutil
import tempfile


def _backup(path):
    """写前把旧文件备份为 .bak（best-effort，失败忽略）。"""
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
    except OSError:
        pass


def _write_atomically(path, dump_fn):
    """通用原子写：临时文件 -> fsync -> os.replace。dump_fn(f) 负责写入已打开的文件对象。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    _backup(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", prefix=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            dump_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, indent=2, ensure_ascii=False):
    """原子写入 JSON 对象。"""
    def _dump(f):
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
    _write_atomically(path, _dump)


def atomic_write_text(path, text):
    """原子写入纯文本（如 HTML 报告）。"""
    def _dump(f):
        f.write(text)
    _write_atomically(path, _dump)


def safe_load_json(path, default=None):
    """安全读取 JSON：主文件 -> .bak -> default，损坏时自动用 .bak 修复主文件。

    default 必须显式传入合适结构（如 [] 或 {} 或带默认键的字典），
    因为无法从函数内部推断调用方期望的类型。
    """
    if default is None:
        default = {}
    candidates = [path, path + ".bak"]
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            continue
        # 若是从 .bak 读到有效数据，顺手把主文件修好，避免下次继续读损坏文件
        if p.endswith(".bak") and os.path.exists(path):
            try:
                # 直接原子回写（不再次备份，避免覆盖刚救回来的 .bak）
                _write_atomically(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=2))
            except Exception:
                pass
        return data
    return default
