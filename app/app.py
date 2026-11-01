#!/usr/bin/env python3
"""
pingmon - Website giam sat thiet bi mang noi bo.

- Kiem tra: Ping (ICMP), Cong TCP, HTTP(S), DNS
- Khi thiet bi DOI TRANG THAI (mat ket noi / ket noi lai) -> ghi su kien;
  trinh duyet phat am thanh + doc ten thiet bi.
- Chi dung thu vien chuan Python (chay thang tren python:3.12-alpine).
- Tu phuc vu HTTPS (cong 443) va chuyen huong HTTP (cong 80).
"""
import hmac
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "./data")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CERT_FILE = os.environ.get("CERT_FILE", "")
KEY_FILE = os.environ.get("KEY_FILE", "")
HTTPS_LISTEN = int(os.environ.get("HTTPS_LISTEN", "443"))
HTTP_LISTEN = int(os.environ.get("HTTP_LISTEN", "80"))
PLAIN_PORT = int(os.environ.get("PORT", "8080"))          # khi khong co chung chi
PUBLIC_HTTPS_PORT = int(os.environ.get("PUBLIC_HTTPS_PORT", "443"))
PING_CMD = os.environ.get("PING_CMD", "ping")
WORKERS = int(os.environ.get("WORKERS", "64"))
EVENT_KEEP_DAYS = 180
HIST_LEN = 30

DEFAULT_SETTINGS = {
    "title": "Giám sát mạng nội bộ",
    "interval": 10,         # giay giua 2 lan kiem tra
    "timeout": 2,           # giay cho moi lan kiem tra
    "fail_threshold": 3,    # so lan loi lien tiep -> MAT KET NOI
    "ok_threshold": 1,      # so lan thanh cong lien tiep -> KET NOI LAI
}
TYPES = ("ping", "tcp", "http", "dns")
HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,253}$")

lock = threading.RLock()
db = None
devices = {}      # id -> dict (ban sao trong bo nho cua bang devices)
rt = {}           # id -> trang thai chay: fails, oks, rtt, msg, last_check, hist
settings = dict(DEFAULT_SETTINGS)
sessions = {}     # token -> het han
inflight = set()
version = 0


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# =================================================================== CSDL
def db_init():
    global db
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DATA_DIR, "pingmon.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, host TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'ping', port INTEGER, target TEXT DEFAULT '',
            grp TEXT DEFAULT '', note TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
            status TEXT DEFAULT 'unknown', since REAL, created REAL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, device_id INTEGER,
            name TEXT, host TEXT, kind TEXT, detail TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS ev_dev ON events(device_id, id);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    for row in db.execute("SELECT key, value FROM settings"):
        if row["key"] in DEFAULT_SETTINGS:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except Exception:
                pass
    for row in db.execute("SELECT * FROM devices"):
        d = dict(row)
        devices[d["id"]] = d
        rt[d["id"]] = new_runtime()
    log(f"Nap {len(devices)} thiet bi")


def new_runtime():
    return {"fails": 0, "oks": 0, "rtt": None, "msg": "", "last_check": None,
            "hist": deque(maxlen=HIST_LEN),
            "next": time.time() + random.random() * min(5, settings["interval"])}


def add_event(d, kind, detail=""):
    """Ghi su kien. Goi khi dang giu lock."""
    cur = db.execute(
        "INSERT INTO events(ts, device_id, name, host, kind, detail) VALUES (?,?,?,?,?,?)",
        (time.time(), d.get("id"), d.get("name"), d.get("host"), kind, detail or ""))
    db.commit()
    if cur.lastrowid % 500 == 0:
        db.execute("DELETE FROM events WHERE ts < ?", (time.time() - EVENT_KEEP_DAYS * 86400,))
        db.commit()


# =================================================================== Kiem tra
def check_ping(d, timeout):
    t0 = time.monotonic()
    try:
        p = subprocess.run([PING_CMD, "-c", "1", "-W", str(max(1, int(timeout))), d["host"]],
                           capture_output=True, text=True, timeout=timeout + 3)
    except subprocess.TimeoutExpired:
        return False, None, "Hết thời gian chờ"
    except FileNotFoundError:
        return False, None, "Máy chủ thiếu lệnh ping"
    if p.returncode == 0:
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", p.stdout)
        return True, float(m.group(1)) if m else (time.monotonic() - t0) * 1000, ""
    out = (p.stdout + " " + p.stderr).lower()
    if "bad address" in out or "unknown host" in out or "name or service" in out or "not known" in out:
        return False, None, "Không phân giải được tên"
    if "permission" in out or "not permitted" in out:
        return False, None, "Không có quyền gửi ICMP (thiếu NET_RAW)"
    if "unreachable" in out:
        return False, None, "Không tới được đích (unreachable)"
    return False, None, "Không phản hồi ping"


def check_tcp(d, timeout):
    t0 = time.monotonic()
    try:
        with socket.create_connection((d["host"], int(d["port"])), timeout=timeout):
            pass
        return True, (time.monotonic() - t0) * 1000, ""
    except socket.timeout:
        return False, None, f"Cổng {d['port']}: hết thời gian chờ"
    except ConnectionRefusedError:
        return False, None, f"Cổng {d['port']} đóng (từ chối kết nối)"
    except socket.gaierror:
        return False, None, "Không phân giải được tên"
    except OSError as e:
        return False, None, f"Cổng {d['port']}: {e.strerror or e}"


_http_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))


def check_http(d, timeout):
    url = (d.get("target") or "").strip() or f"http://{d['host']}/"
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pingmon/1.0"})
        with _http_opener.open(req, timeout=timeout) as r:
            code = r.status
            r.read(512)
    except urllib.error.HTTPError as e:
        code = e.code
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            return False, None, "HTTP: hết thời gian chờ"
        if isinstance(reason, ConnectionRefusedError):
            return False, None, "HTTP: từ chối kết nối"
        if isinstance(reason, socket.gaierror):
            return False, None, "Không phân giải được tên"
        return False, None, f"HTTP: {reason}"
    except (socket.timeout, TimeoutError):
        return False, None, "HTTP: hết thời gian chờ"
    except Exception as e:
        return False, None, f"HTTP: {e}"
    ms = (time.monotonic() - t0) * 1000
    if code < 500:
        return True, ms, f"HTTP {code}"
    return False, ms, f"HTTP {code} (lỗi máy chủ)"


def check_dns(d, timeout):
    name = (d.get("target") or "").strip().rstrip(".") or "localhost"
    qid = secrets.randbits(16)
    try:
        qname = b"".join(bytes([len(p)]) + p for p in name.encode("idna").split(b".")) + b"\x00"
    except Exception:
        return False, None, "Tên miền cần phân giải không hợp lệ"
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    t0 = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, (d["host"], int(d.get("port") or 53)))
            data, _ = s.recvfrom(4096)
    except socket.timeout:
        return False, None, "DNS: không phản hồi"
    except OSError as e:
        return False, None, f"DNS: {e.strerror or e}"
    ms = (time.monotonic() - t0) * 1000
    if len(data) < 12:
        return False, ms, "DNS: phản hồi lỗi"
    rid, flags, _qd, an = struct.unpack(">HHHH", data[:8])
    rcode = flags & 0xF
    if rid != qid:
        return False, ms, "DNS: phản hồi sai mã"
    if rcode == 0 and an > 0:
        return True, ms, f"{name} OK"
    if rcode == 0:
        return False, ms, f"DNS: {name} không có bản ghi A"
    if rcode == 3:
        return False, ms, f"DNS: {name} không tồn tại (NXDOMAIN)"
    if rcode == 2:
        return False, ms, "DNS: lỗi máy chủ (SERVFAIL)"
    if rcode == 5:
        return False, ms, "DNS: từ chối truy vấn (REFUSED)"
    return False, ms, f"DNS: lỗi rcode={rcode}"


CHECKS = {"ping": check_ping, "tcp": check_tcp, "http": check_http, "dns": check_dns}


def run_check(did, snap, timeout):
    try:
        ok, rtt, msg = CHECKS[snap["type"]](snap, timeout)
    except Exception as e:
        ok, rtt, msg = False, None, f"Lỗi kiểm tra: {e}"
    try:
        apply_result(did, snap, ok, rtt, msg)
    finally:
        with lock:
            inflight.discard(did)


def apply_result(did, snap, ok, rtt, msg):
    global version
    now = time.time()
    with lock:
        d, r = devices.get(did), rt.get(did)
        # thiet bi bi xoa / sua / tam dung trong luc dang kiem tra -> bo ket qua
        if not d or not r or not d["enabled"] or d["host"] != snap["host"] or d["type"] != snap["type"]:
            return
        r["last_check"], r["rtt"], r["msg"] = now, (rtt if ok else None), msg
        r["hist"].append(1 if ok else 0)
        if ok:
            r["oks"], r["fails"] = r["oks"] + 1, 0
        else:
            r["fails"], r["oks"] = r["fails"] + 1, 0
        new = None
        if ok and d["status"] != "up" and r["oks"] >= settings["ok_threshold"]:
            new = "up"
        elif not ok and d["status"] != "down" and r["fails"] >= settings["fail_threshold"]:
            new = "down"
        if new:
            old = d["status"]
            d["status"], d["since"] = new, now
            db.execute("UPDATE devices SET status=?, since=? WHERE id=?", (new, now, did))
            db.commit()
            # unknown -> up (luc moi them / khoi dong) thi khong bao
            if not (old == "unknown" and new == "up"):
                add_event(d, new, msg)
                log(f"{'MAT KET NOI' if new == 'down' else 'KET NOI LAI'}: {d['name']} ({d['host']}) {msg}")
        version += 1


def scheduler():
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    while True:
        now = time.time()
        jobs = []
        with lock:
            for did, d in devices.items():
                r = rt[did]
                if not d["enabled"] or did in inflight or now < r["next"]:
                    continue
                r["next"] = now + settings["interval"]
                inflight.add(did)
                jobs.append((did, dict(d)))
            timeout = settings["timeout"]
        for did, snap in jobs:
            pool.submit(run_check, did, snap, timeout)
        time.sleep(0.5)


# =================================================================== Quan ly thiet bi
def clean_device(body, old=None):
    """Kiem tra du lieu nhap. Tra ve (dict, loi)."""
    d = dict(old or {})
    name = str(body.get("name", d.get("name", ""))).strip()
    host = str(body.get("host", d.get("host", ""))).strip()
    typ = str(body.get("type", d.get("type", "ping"))).strip().lower()
    target = str(body.get("target", d.get("target", "")) or "").strip()
    grp = str(body.get("grp", d.get("grp", "")) or "").strip()[:60]
    note = str(body.get("note", d.get("note", "")) or "").strip()[:300]
    port = body.get("port", d.get("port"))
    enabled = 1 if body.get("enabled", d.get("enabled", 1)) in (1, True, "1", "true", "on") else 0

    if typ not in TYPES:
        return None, "Loại kiểm tra không hợp lệ"
    if typ == "http" and not host and target:
        host = urlparse(target).hostname or ""
    if not host:
        return None, "Chưa nhập địa chỉ IP / tên máy"
    if not HOST_RE.match(host) or host.startswith("-"):
        return None, f"Địa chỉ không hợp lệ: {host}"
    if not name:
        name = host
    name = name[:100]
    if port in ("", None):
        port = 53 if typ == "dns" else None
    if port is not None:
        try:
            port = int(port)
            assert 1 <= port <= 65535
        except Exception:
            return None, "Cổng phải từ 1 đến 65535"
    if typ == "tcp" and not port:
        return None, "Kiểm tra cổng TCP cần nhập số cổng"
    if typ == "http":
        if not target:
            target = f"http://{host}/"
        if not re.match(r"^https?://", target, re.I):
            return None, "URL phải bắt đầu bằng http:// hoặc https://"
    if typ == "dns" and not target:
        return None, "Kiểm tra DNS cần nhập tên miền cần phân giải"
    if typ in ("ping", "tcp"):
        target = ""
    if typ == "ping":
        port = None
    d.update(name=name, host=host, type=typ, port=port, target=target[:500], grp=grp, note=note, enabled=enabled)
    return d, None


def device_insert(d):
    now = time.time()
    cur = db.execute(
        "INSERT INTO devices(name, host, type, port, target, grp, note, enabled, status, since, created)"
        " VALUES (?,?,?,?,?,?,?,?, 'unknown', ?, ?)",
        (d["name"], d["host"], d["type"], d["port"], d["target"], d["grp"], d["note"], d["enabled"], now, now))
    d.update(id=cur.lastrowid, status="unknown", since=now, created=now)
    devices[d["id"]] = d
    rt[d["id"]] = new_runtime()
    rt[d["id"]]["next"] = 0          # kiem tra ngay
    return d


def api_device_save(body):
    global version
    with lock:
        did = body.get("id")
        if did:
            old = devices.get(int(did))
            if not old:
                return 404, {"error": "Không tìm thấy thiết bị"}
            d, err = clean_device(body, old)
            if err:
                return 400, {"error": err}
            changed = any(old[k] != d[k] for k in ("host", "type", "port", "target")) or old["enabled"] != d["enabled"]
            if changed:   # doi muc tieu kiem tra -> bat dau lai tu dau
                d["status"], d["since"] = "unknown", time.time()
                rt[d["id"]] = new_runtime()
                rt[d["id"]]["next"] = 0
            db.execute("UPDATE devices SET name=?, host=?, type=?, port=?, target=?, grp=?, note=?, enabled=?,"
                       " status=?, since=? WHERE id=?",
                       (d["name"], d["host"], d["type"], d["port"], d["target"], d["grp"], d["note"],
                        d["enabled"], d["status"], d["since"], d["id"]))
            if old["enabled"] != d["enabled"]:
                add_event(d, "resume" if d["enabled"] else "pause")
            else:
                add_event(d, "edit")
            devices[d["id"]] = d
        else:
            d, err = clean_device(body)
            if err:
                return 400, {"error": err}
            device_insert(d)
            add_event(d, "add")
        db.commit()
        version += 1
        return 200, {"ok": True, "id": d["id"]}


def _ids(body):
    if body.get("all") is True:
        return list(devices.keys())
    raw = body.get("ids")
    if raw is None and body.get("id") is not None:
        raw = [body.get("id")]
    try:
        return [int(x) for x in (raw or [])]
    except (TypeError, ValueError):
        return []


def delete_devices(ids):
    """Xoa nhieu thiet bi. Goi khi dang giu lock. Tra ve danh sach da xoa."""
    gone = []
    for did in ids:
        d = devices.pop(did, None)
        if d:
            rt.pop(did, None)
            inflight.discard(did)
            gone.append(d)
    if gone:
        db.executemany("DELETE FROM devices WHERE id=?", [(d["id"],) for d in gone])
        if len(gone) == 1:
            add_event(gone[0], "delete")
        else:
            add_event({"name": f"{len(gone)} thiết bị", "host": ""}, "delete")
        db.commit()
    return gone


def api_device_delete(body):
    """{"id": 5} | {"ids": [1,2,3]} | {"all": true}"""
    global version
    with lock:
        gone = delete_devices(_ids(body))
        if not gone:
            return 404, {"error": "Không có thiết bị nào được xoá"}
        version += 1
        return 200, {"ok": True, "deleted": len(gone)}


def api_device_bulk(body):
    """Tam dung / tiep tuc / doi nhom nhieu thiet bi: {"ids": [...], "action": "pause|resume|group", "grp": "..."}"""
    global version
    action = body.get("action")
    if action not in ("pause", "resume", "group"):
        return 400, {"error": "Thao tác không hợp lệ"}
    grp = str(body.get("grp") or "").strip()[:60]
    with lock:
        changed = 0
        for did in _ids(body):
            d = devices.get(did)
            if not d:
                continue
            if action == "group":
                if d["grp"] != grp:
                    d["grp"] = grp
                    db.execute("UPDATE devices SET grp=? WHERE id=?", (grp, did))
                    changed += 1
                continue
            want = 1 if action == "resume" else 0
            if d["enabled"] == want:
                continue
            d["enabled"], d["status"], d["since"] = want, "unknown", time.time()
            rt[did] = new_runtime()
            rt[did]["next"] = 0
            db.execute("UPDATE devices SET enabled=?, status='unknown', since=? WHERE id=?", (want, d["since"], did))
            changed += 1
        if changed:
            label = {"pause": "pause", "resume": "resume", "group": "edit"}[action]
            add_event({"name": f"{changed} thiết bị" + (f" → nhóm {grp or '(trống)'}" if action == "group" else ""),
                       "host": ""}, label)
        db.commit()
        version += 1
    return 200, {"ok": True, "changed": changed}


IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
JUNK_RE = re.compile(r"^(\d+(\.\d+)?\s*ms|<?\d+ms|n/?a|-+|\d+%?|ms|up|down|online|offline|alive|dead|timeout)$", re.I)


def parse_import_line(line, default_grp=""):
    """Tra ve dict thiet bi tu 1 dong. Ho tro 2 kieu:
       1) Ten, IP [, Nhom [, Loai [, Cong/URL/Ten mien]]]
       2) Dan thang ket qua quet mang: IP <tab> 2 ms <tab> HOSTNAME  (Advanced IP Scanner, PingInfoView...)"""
    parts = [p.strip() for p in re.split(r"[,;\t]", line)]
    if len(parts) == 1:
        parts = parts[0].split()          # "192.168.1.1   2 ms   HOST" cach bang khoang trang
        if len(parts) == 1:
            parts = [parts[0], parts[0]]
    # Kieu 2: cot dau la IP, cot 2 khong phai dia chi (vd "2 ms") -> lay hostname o cot sau
    if IPV4_RE.match(parts[0]) and not (len(parts) > 1 and (IPV4_RE.match(parts[1]) or (HOST_RE.match(parts[1]) and not JUNK_RE.match(parts[1]) and "." in parts[1]))):
        names = [p for p in parts[1:] if p and not JUNK_RE.match(p) and not IPV4_RE.match(p)]
        return {"name": names[-1] if names else parts[0], "host": parts[0], "grp": default_grp, "type": "ping"}
    rec = {"name": parts[0], "host": parts[1] if len(parts) > 1 else parts[0],
           "grp": parts[2] if len(parts) > 2 and parts[2] else default_grp,
           "type": (parts[3] if len(parts) > 3 and parts[3] else "ping").lower()}
    extra = parts[4] if len(parts) > 4 else ""
    if extra:
        rec["port" if rec["type"] == "tcp" else "target"] = extra
    return rec


def api_import(body):
    """Nhap hang loat. body: {"text": "...", "grp": "nhom mac dinh", "replace": true/false}
       replace=true: xoa TOAN BO thiet bi cu roi moi nhap (chi khi co it nhat 1 dong hop le)."""
    global version
    errors, valid = [], []
    default_grp = str(body.get("grp") or "").strip()
    replace = body.get("replace") is True
    with lock:
        existing = set() if replace else {(d["host"], d["type"], d.get("port"), d.get("target")) for d in devices.values()}
        for i, line in enumerate(str(body.get("text", "")).splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d, err = clean_device(parse_import_line(line, default_grp))
            if err:
                errors.append(f"Dòng {i}: {err}")
                continue
            key = (d["host"], d["type"], d.get("port"), d.get("target"))
            if key in existing:
                errors.append(f"Dòng {i}: {d['host']} đã có, bỏ qua")
                continue
            existing.add(key)
            valid.append(d)
        if not valid:
            return 400, {"error": "Không có dòng hợp lệ nào để nhập" + (" – danh sách cũ được giữ nguyên" if replace else ""),
                         "errors": errors[:50]}
        replaced = len(delete_devices(list(devices.keys()))) if replace else 0
        for d in valid:
            device_insert(d)
        add_event({"name": f"{len(valid)} thiết bị" + (f" (thay thế {replaced} cũ)" if replace else ""), "host": ""}, "import")
        db.commit()
        version += 1
    return 200, {"ok": True, "added": len(valid), "replaced": replaced, "errors": errors[:50]}


def api_settings(body):
    global version
    limits = {"interval": (5, 3600), "timeout": (1, 30), "fail_threshold": (1, 20), "ok_threshold": (1, 20)}
    with lock:
        for k, (lo, hi) in limits.items():
            if k in body:
                try:
                    v = int(body[k])
                except Exception:
                    return 400, {"error": f"Giá trị {k} không hợp lệ"}
                if not lo <= v <= hi:
                    return 400, {"error": f"{k} phải từ {lo} đến {hi}"}
                settings[k] = v
        if "title" in body:
            settings["title"] = str(body["title"]).strip()[:80] or DEFAULT_SETTINGS["title"]
        if settings["timeout"] >= settings["interval"]:
            settings["timeout"] = max(1, settings["interval"] - 1)
        for k, v in settings.items():
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
        db.commit()
        version += 1
    return 200, {"ok": True, "settings": settings}


def snapshot(since, admin):
    with lock:
        out = []
        for d in devices.values():
            r = rt[d["id"]]
            h = list(r["hist"])
            out.append({
                "id": d["id"], "name": d["name"], "host": d["host"], "type": d["type"],
                "port": d["port"], "target": d["target"], "grp": d["grp"], "note": d["note"],
                "enabled": d["enabled"],
                "status": d["status"] if d["enabled"] else "paused",
                "since": d["since"], "rtt": r["rtt"], "msg": r["msg"], "last_check": r["last_check"],
                "hist": "".join(map(str, h)),
                "loss": round(100 * (1 - sum(h) / len(h))) if h else None,
            })
        if since < 0:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 150").fetchall()[::-1]
        else:
            rows = db.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 300", (since,)).fetchall()
        last = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        return {
            "now": time.time(), "version": version, "settings": settings,
            "admin": admin, "auth": bool(ADMIN_PASSWORD),
            "devices": out, "events": [dict(x) for x in rows], "last_event_id": last,
        }


def device_history(did):
    """Nhat ky + thong ke mat ket noi cua 1 thiet bi (cho o hien khi tro chuot vao ten)."""
    now = time.time()
    with lock:
        d = devices.get(did)
        rows = [dict(r) for r in db.execute(
            "SELECT id, ts, kind, detail FROM events WHERE device_id=? ORDER BY id DESC LIMIT 500", (did,))]
    if not d:
        return None
    rows.reverse()                       # tang dan theo thoi gian de ghep cap mat/ket noi lai
    outages, open_down = [], None
    for e in rows:
        if e["kind"] == "down":
            open_down = e
        elif e["kind"] == "up" and open_down:
            open_down["duration"] = e["ts"] - open_down["ts"]
            outages.append((open_down["ts"], e["ts"]))
            open_down = None
        elif e["kind"] in ("edit", "pause", "resume") and open_down and d["status"] != "down":
            open_down = None             # chuoi bi ngat do sua / tam dung
    if open_down and d["status"] == "down" and d["enabled"]:
        open_down["ongoing"] = True
        outages.append((open_down["ts"], now))

    def stat(window):
        start = now - window
        cnt = sum(1 for a, b in outages if b > start and a >= start)
        total = sum(max(0, b - max(a, start)) for a, b in outages if b > start)
        return {"count": cnt, "downtime": round(total), "uptime_pct": (int(10000 * (1 - total / window)) / 100) if total else 100}

    return {"id": did, "name": d["name"], "host": d["host"], "status": d["status"] if d["enabled"] else "paused",
            "since": d["since"], "created": d.get("created"),
            "stats": {"day": stat(86400), "week": stat(7 * 86400), "month": stat(30 * 86400)},
            "events": rows[::-1][:60]}


def export_csv():
    with lock:
        lines = ["# Ten, IP, Nhom, Loai, Cong/URL/Ten mien"]
        for d in sorted(devices.values(), key=lambda x: (x["grp"], x["name"])):
            extra = d["port"] if d["type"] == "tcp" else (d["target"] or "")
            lines.append(", ".join(str(x).replace(",", " ") for x in
                                   (d["name"], d["host"], d["grp"], d["type"], extra or "")).rstrip(", "))
        return "\n".join(lines) + "\n"


# =================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "pingmon/1.0"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pm_session":
                return v
        return ""

    def is_admin(self):
        if not ADMIN_PASSWORD:
            return True
        tok = self._session()
        with lock:
            exp = sessions.get(tok)
        return bool(exp and exp > time.time())

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("Dữ liệu quá lớn")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            try:
                since = int(parse_qs(u.query).get("since", ["-1"])[0])
            except ValueError:
                since = -1
            return self._send(200, snapshot(since, self.is_admin()))
        if u.path == "/api/history":
            try:
                did = int(parse_qs(u.query).get("id", ["0"])[0])
            except ValueError:
                did = 0
            h = device_history(did)
            return self._send(200, h) if h else self._send(404, {"error": "Không tìm thấy thiết bị"})
        if u.path == "/api/export":
            if not self.is_admin():
                return self._send(403, {"error": "Cần đăng nhập quản trị"})
            return self._send(200, export_csv().encode("utf-8"), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="thiet-bi.csv"'})
        if u.path == "/healthz":
            return self._send(200, {"ok": True, "devices": len(devices)})
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, {"error": "Không tìm thấy"})

    def do_HEAD(self):
        self.send_response(200 if urlparse(self.path).path in ("/", "/index.html", "/healthz") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "Cần JSON"})
        try:
            body = self._json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "Dữ liệu không hợp lệ"})

        if u.path == "/api/login":
            pw = str(body.get("password", ""))
            if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                tok = secrets.token_urlsafe(32)
                with lock:
                    now = time.time()
                    for k in [k for k, v in sessions.items() if v < now]:
                        sessions.pop(k)
                    sessions[tok] = now + 30 * 86400
                secure = "; Secure" if getattr(self.server, "is_tls", False) else ""
                log("Dang nhap quan tri tu", self.client_address[0])
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"pm_session={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"})
            time.sleep(1.5)
            log("Sai mat khau quan tri tu", self.client_address[0])
            return self._send(403, {"error": "Sai mật khẩu"})

        if u.path == "/api/logout":
            with lock:
                sessions.pop(self._session(), None)
            return self._send(200, {"ok": True}, headers={"Set-Cookie": "pm_session=; Path=/; Max-Age=0"})

        if not self.is_admin():
            return self._send(403, {"error": "Cần đăng nhập quản trị"})
        routes = {
            "/api/device/save": api_device_save,
            "/api/device/delete": api_device_delete,
            "/api/device/bulk": api_device_bulk,
            "/api/import": api_import,
            "/api/settings": api_settings,
        }
        fn = routes.get(u.path)
        if not fn:
            return self._send(404, {"error": "Không tìm thấy"})
        try:
            code, res = fn(body)
        except Exception as e:
            log("Loi API", u.path, e)
            code, res = 500, {"error": f"Lỗi máy chủ: {e}"}
        self._send(code, res)


class RedirectHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0] or "localhost"
        port = "" if PUBLIC_HTTPS_PORT == 443 else f":{PUBLIC_HTTPS_PORT}"
        self.send_response(301)
        self.send_header("Location", f"https://{host}{port}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_POST = do_GET


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass   # bo qua loi ket noi cua trinh duyet (dong tab, TLS that bai...)


class TLSServer(QuietServer):
    is_tls = True

    def __init__(self, addr, handler, ctx):
        self.ctx = ctx
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        # Bat tay TLS trong luong xu ly rieng -> 1 client cham khong chan ca server
        request.settimeout(15)
        tls = self.ctx.wrap_socket(request, server_side=True)
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except Exception:
                pass


def main():
    db_init()
    if not ADMIN_PASSWORD:
        log("CANH BAO: ADMIN_PASSWORD rong -> ai cung sua duoc danh sach thiet bi")
    threading.Thread(target=scheduler, daemon=True).start()

    if CERT_FILE and KEY_FILE and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        redirect = QuietServer(("0.0.0.0", HTTP_LISTEN), RedirectHandler)
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        srv = TLSServer(("0.0.0.0", HTTPS_LISTEN), Handler, ctx)
        log(f"pingmon chay HTTPS cong {HTTPS_LISTEN}, chuyen huong HTTP cong {HTTP_LISTEN}")
    else:
        srv = QuietServer(("0.0.0.0", PLAIN_PORT), Handler)
        log(f"pingmon chay HTTP (khong co chung chi) cong {PLAIN_PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
