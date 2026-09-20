#!/usr/bin/env python3
"""
============================================================================
activation_server.py - Server kích hoạt cho T-Display S3 Video Player
============================================================================
Khớp với submitActivation() trong file .ino:
  POST /activate  body: {"key": "...", "mac": "AA:BB:CC:DD:EE:FF"}
  - 200                       -> thiết bị coi là kích hoạt thành công
  - bất kỳ mã khác 200        -> thất bại; nếu body có {"error": "..."}
                                  thì thiết bị hiện đúng câu đó lên màn hình

QUY TẮC NGHIỆP VỤ (theo yêu cầu):
  - Tối đa MAX_KEYS (=50) mã kích hoạt tồn tại trong toàn bộ hệ thống
    (=  tối đa 50 người dùng).
  - Mỗi key chỉ dùng được cho ĐÚNG 1 thiết bị (1 địa chỉ MAC):
      * Lần dùng key thành công ĐẦU TIÊN sẽ khoá (gắn) key đó với MAC gửi lên.
      * Cùng key + CÙNG mac gửi lại sau đó (vd sau khi Factory Reset) -> vẫn OK.
      * Cùng key + MAC KHÁC -> bị từ chối (403, "Key đã được dùng cho máy khác").
  - Việc khoá thiết bị sau 5 lần nhập sai đã xử lý HOÀN TOÀN ở phía firmware
    (cfgActivateFails/cfgActivateLocked trong .ino) - server không cần biết
    chuyện đó. Server chỉ trả lời đúng/sai cho từng lần thử.
  - Ngoài ra server tự chặn brute-force ở tầng IP (RATE_LIMIT_* bên dưới) để
    phòng ai đó gọi thẳng /activate bằng curl mà không qua thiết bị.

QUẢN TRỊ (admin) - bảo vệ bằng ADMIN_TOKEN (đặt qua biến môi trường):
  GET  /admin/keys?token=...                 -> liệt kê toàn bộ key + trạng thái
  POST /admin/keys/generate?token=...         body {"count": N} -> sinh thêm N key
  POST /admin/keys/reset?token=...            body {"key": "..."} -> gỡ MAC khỏi
                                               1 key (để cấp lại cho máy khác, vd
                                               khi 1 board bị hỏng phải thay board)
  POST /admin/keys/revoke?token=...           body {"key": "..."} -> xoá hẳn 1 key

LƯU TRỮ:
  Toàn bộ trạng thái nằm trong 1 file JSON (DB_PATH, mặc định keys_db.json)
  cạnh script này, ghi bằng ghi-tạm-rồi-đổi-tên (atomic) để không bao giờ
  hỏng file giữa chừng.

  !!! LƯU Ý QUAN TRỌNG VỀ RENDER.COM FREE TIER !!!
  Ổ đĩa của gói Free trên Render KHÔNG bền vững - mỗi lần server bị redeploy
  hoặc "restart do free tier ngủ/dậy" có thể mất sạch dữ liệu (mất luôn danh
  sách MAC đã gắn -> người dùng phải kích hoạt lại). Nếu cần dữ liệu sống
  lâu dài, hai lựa chọn:
    1) Bật "Persistent Disk" trả phí trên Render, mount vào DB_PATH.
    2) Đổi DB_PATH sang trỏ ra 1 dịch vụ ngoài (Upstash Redis free tier,
       hoặc 1 Google Sheet qua API) - cần sửa hàm _load_db()/_save_db() bên
       dưới, còn lại giữ nguyên.
  Trong lúc mới chạy thử / vài chục người dùng, dùng file JSON là đủ.

CÀI ĐẶT & CHẠY:
  pip install -r requirements.txt
  export ADMIN_TOKEN="chuoi-bi-mat-tu-dat"
  python activation_server.py            # chạy local, cổng 8000
  # deploy lên Render: Start Command =  gunicorn activation_server:app
============================================================================
"""

import json
import os
import random
import re
import string
import tempfile
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, request

app = Flask(__name__)

# ----------------------------------------------------------------------------
# CẤU HÌNH
# ----------------------------------------------------------------------------
MAX_KEYS = 50                      # tổng số key tối đa toàn hệ thống (=50 người)
KEY_LENGTH = 5                     # số ký tự mỗi key - ngắn cho dễ gõ bằng bàn
                                    # phím 2 nút (BOOT di chuyển, Pin14 chọn)
                                    # trên màn hình thiết bị
# Bỏ các ký tự dễ nhầm khi gõ trên bàn phím nhỏ của thiết bị: 0/O, 1/I/L
KEY_ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits) if c not in "0O1IL")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "keys_db.json"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")  # BẮT BUỘC đặt khi deploy thật

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

# Chống brute-force gọi thẳng /activate: tối đa RATE_LIMIT_MAX lần thử sai
# trong RATE_LIMIT_WINDOW giây cho mỗi địa chỉ IP (bộ nhớ RAM, tự quên khi
# server restart - không cần bền vững, đây chỉ là hàng rào phụ, hàng rào
# chính vẫn là khoá 5-lần ở firmware).
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 300  # giây
_fail_hits = defaultdict(deque)
_rate_lock = threading.Lock()

_db_lock = threading.Lock()

# ----------------------------------------------------------------------------
# LƯU TRỮ (đọc/ghi file JSON, atomic write)
# ----------------------------------------------------------------------------
def _empty_db():
    return {"keys": {}}  # keys["ABCDEFGHIJ"] = {"mac": None|"AA:BB:...", "activated_at": None|epoch, "note": ""}


def _load_db():
    if not os.path.exists(DB_PATH):
        return _empty_db()
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("keys", {})
            return data
    except (json.JSONDecodeError, OSError):
        # File hỏng/rỗng bất thường -> đừng làm sập server, coi như DB trống
        return _empty_db()


def _save_db(data):
    dir_ = os.path.dirname(DB_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".keys_db_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DB_PATH)  # atomic trên cùng 1 filesystem
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _gen_key():
    return "".join(random.SystemRandom().choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


# ----------------------------------------------------------------------------
# TIỆN ÍCH
# ----------------------------------------------------------------------------
def _client_ip():
    # Render/hầu hết PaaS đặt sau reverse proxy -> ưu tiên X-Forwarded-For
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


# ----------------------------------------------------------------------------
# ENDPOINT CHO THIẾT BỊ
# ----------------------------------------------------------------------------
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
            # Lần dùng đầu tiên -> gắn key này với đúng thiết bị đang gửi lên
            entry["mac"] = mac
            entry["activated_at"] = int(time.time())
            _save_db(db)
            return jsonify({"status": "activated"}), 200

        if entry["mac"] == mac:
            # Cùng thiết bị gửi lại (vd sau khi reset máy) -> vẫn hợp lệ
            return jsonify({"status": "already_activated"}), 200

        # Key đã bị gắn với 1 thiết bị KHÁC
        _record_fail(ip)
        return jsonify({"error": "Key đã được dùng cho máy khác"}), 403


# ----------------------------------------------------------------------------
# ENDPOINT QUẢN TRỊ
# ----------------------------------------------------------------------------
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
    """Gỡ MAC khỏi 1 key (không xoá key) - dùng khi phải cấp lại key đó cho
    board mới (vd board cũ hỏng), mà không muốn tốn thêm 1 suất trong 20."""
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
    """Xoá hẳn 1 key khỏi hệ thống, trả lại 1 suất trong tổng 20."""
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


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if not ADMIN_TOKEN:
        print("!! CẢNH BÁO: chưa đặt biến môi trường ADMIN_TOKEN - các endpoint "
              "/admin/* sẽ luôn trả lỗi 500 cho tới khi bạn đặt nó.")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
  
