import json
import os
import random
import re
import string
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict, deque

from flask import Flask, jsonify, request

app = Flask(__name__)


MAX_KEYS = 50
KEY_LENGTH = 5


KEY_ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits) if c not in "0O1IL")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "keys_db.json"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

SEED_KEYS = {
    "68MKB": "admin",
    "T73BS": "",
    "R6SW7": "",
    "W8QG8": "",
    "3THRG": "",
    "869EF": "",
    "DTFFS": "",
    "2KFJU": "",
    "7MS5U": "",
    "ZGKNM": "",
    "U477Y": "",
    "HFYY8": "",
    "X6M47": "",
    "ZZEYR": "",
    "RXDFS": "",
    "72HWY": "",
    "EZ9RP": "",
    "BMJST": "",
    "MCR7E": "",
    "SWGHE": "",
}

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 300
_fail_hits = defaultdict(deque)
_rate_lock = threading.Lock()

_db_lock = threading.Lock()


def _empty_db():
    return {"keys": {k: {"mac": None, "activated_at": None, "note": n} for k, n in SEED_KEYS.items()}}


def _load_db():
    if not os.path.exists(DB_PATH):
        return _empty_db()
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("keys", {})
            return data
    except (json.JSONDecodeError, OSError):
        return _empty_db()


def _save_db(data):
    dir_ = os.path.dirname(DB_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".keys_db_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DB_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _gen_key():
    return "".join(random.SystemRandom().choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited(ip):
    now = time.time()
    with _rate_lock:
        hits = _fail_hits[ip]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW:
            hits.popleft()
        return len(hits) >= RATE_LIMIT_MAX


def _record_fail(ip):
    with _rate_lock:
        _fail_hits[ip].append(time.time())


def _require_admin():
    token = request.args.get("token")
    if not token and request.is_json:
        token = (request.get_json(silent=True) or {}).get("token")
    if not ADMIN_TOKEN:
        return jsonify({"error": "ADMIN_TOKEN chưa được cấu hình trên server"}), 500
    if token != ADMIN_TOKEN:
        return jsonify({"error": "Sai token quản trị"}), 401
    return None


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "activation_server"})


@app.route("/activate", methods=["POST"])
def activate():
    ip = _client_ip()
    if _rate_limited(ip):
        return jsonify({"error": "Thử lại quá nhiều, vui lòng chờ ít phút"}), 429
    body = request.get_json(silent=True) or {}
    key = str(body.get("key", "")).strip().upper()
    mac = str(body.get("mac", "")).strip().upper()
    if not key or not mac:
        _record_fail(ip)
        return jsonify({"error": "Thiếu key hoặc mac"}), 400
    if not MAC_RE.match(mac):
        _record_fail(ip)
        return jsonify({"error": "Địa chỉ MAC không hợp lệ"}), 400
    with _db_lock:
        db = _load_db()
        entry = db["keys"].get(key)
        if entry is None:
            _record_fail(ip)
            return jsonify({"error": "Sai mã kích hoạt"}), 403
        if entry["mac"] is None:
            entry["mac"] = mac
            entry["activated_at"] = int(time.time())
            _save_db(db)
            return jsonify({"status": "activated"}), 200
        if entry["mac"] == mac:
            return jsonify({"status": "already_activated"}), 200
        _record_fail(ip)
        return jsonify({"error": "Key đã được dùng cho máy khác"}), 403


@app.route("/admin/keys", methods=["GET"])
def admin_list_keys():
    err = _require_admin()
    if err:
        return err
    db = _load_db()
    keys = db["keys"]
    used = sum(1 for v in keys.values() if v["mac"])
    return jsonify({
        "total": len(keys),
        "max": MAX_KEYS,
        "activated": used,
        "unused": len(keys) - used,
        "keys": keys,
    })


@app.route("/admin/keys/generate", methods=["POST"])
def admin_generate_keys():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 1))
    note = str(body.get("note", ""))
    if count < 1 or count > MAX_KEYS:
        return jsonify({"error": f"count phải trong khoảng 1..{MAX_KEYS}"}), 400
    with _db_lock:
        db = _load_db()
        remaining = MAX_KEYS - len(db["keys"])
        if remaining <= 0:
            return jsonify({"error": f"Đã đạt giới hạn {MAX_KEYS} key, không sinh thêm được"}), 409
        if count > remaining:
            return jsonify({"error": f"Chỉ còn {remaining} suất key trống (giới hạn {MAX_KEYS})"}), 409
        new_keys = []
        for _ in range(count):
            while True:
                k = _gen_key()
                if k not in db["keys"]:
                    break
            db["keys"][k] = {"mac": None, "activated_at": None, "note": note}
            new_keys.append(k)
        _save_db(db)
    return jsonify({"generated": new_keys, "count": len(new_keys)}), 200


@app.route("/admin/keys/reset", methods=["POST"])
def admin_reset_key():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    key = str(body.get("key", "")).strip().upper()
    with _db_lock:
        db = _load_db()
        entry = db["keys"].get(key)
        if entry is None:
            return jsonify({"error": "Key không tồn tại"}), 404
        entry["mac"] = None
        entry["activated_at"] = None
        _save_db(db)
    return jsonify({"status": "reset", "key": key}), 200


@app.route("/admin/keys/revoke", methods=["POST"])
def admin_revoke_key():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    key = str(body.get("key", "")).strip().upper()
    with _db_lock:
        db = _load_db()
        if key not in db["keys"]:
            return jsonify({"error": "Key không tồn tại"}), 404
        del db["keys"][key]
        _save_db(db)
    return jsonify({"status": "revoked", "key": key}), 200


def register_with_worker():
    base = os.environ.get("WORKER_URL", "").strip().rstrip("/")
    secret = os.environ.get("REG_SECRET", "").strip()
    me = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not (base and secret and me):
        return
    body = json.dumps({"url": me, "kind": "activation"}).encode("utf-8")
    for _ in range(6):
        try:
            req = urllib.request.Request(
                base + "/__register", data=body, method="POST",
                headers={"Content-Type": "application/json", "X-Reg-Secret": secret, "User-Agent": "relay-register/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"[register] {me} -> {r.status}", flush=True)
                return
        except Exception as e:
            print(f"[register] loi: {e}", flush=True)
            time.sleep(10)


threading.Thread(target=register_with_worker, daemon=True).start()


if __name__ == "__main__":
    if not ADMIN_TOKEN:
        print("!! CẢNH BÁO: chưa đặt biến môi trường ADMIN_TOKEN - các endpoint "
              "/admin/* sẽ luôn trả lỗi 500 cho tới khi bạn đặt nó.")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
