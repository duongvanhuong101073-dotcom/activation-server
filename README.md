# activation_server.py

Server kích hoạt cho T-Display S3 Video Player - khớp với `submitActivation()`
trong file `.ino` (`ACTIVATION_HOST` + `/activate`).

## Chạy thử ở máy local

```bash
pip install -r requirements.txt
export ADMIN_TOKEN="doi-thanh-chuoi-bi-mat-cua-ban"
python activation_server.py
```

Server chạy ở `http://0.0.0.0:8000`.

## Deploy lên Render.com (free tier, giống RELAY_HOST_AWAY)

1. Đẩy 3 file này (`activation_server.py`, `requirements.txt`) lên 1 repo GitHub.
2. Trên Render: New -> Web Service -> chọn repo đó.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn activation_server:app`
5. Vào tab Environment, thêm biến `ADMIN_TOKEN` = 1 chuỗi bí mật tự đặt.
6. Deploy xong, copy URL Render cấp (dạng `https://xxxx.onrender.com`) và dán
   vào `ACTIVATION_HOST` trong file `.ino`.

⚠️ Free tier của Render không có ổ đĩa bền vững — xem cảnh báo chi tiết ở đầu
`activation_server.py`. Nếu chạy thật cho 20 người dùng lâu dài, nên nâng cấp
lên gói có Persistent Disk (rẻ) và mount vào biến `DB_PATH`.

## Sinh 20 key kích hoạt ban đầu

```bash
curl -X POST "https://xxxx.onrender.com/admin/keys/generate?token=doi-thanh-chuoi-bi-mat-cua-ban" \
     -H "Content-Type: application/json" \
     -d '{"count": 20, "note": "Lô đầu tiên"}'
```

Trả về mảng 20 key (10 ký tự chữ+số, đã bỏ các ký tự dễ nhầm 0/O/1/I/L để gõ
trên bàn phím nhỏ của thiết bị cho dễ). Gửi mỗi key cho đúng 1 người dùng.

## Xem trạng thái toàn bộ key

```bash
curl "https://xxxx.onrender.com/admin/keys?token=doi-thanh-chuoi-bi-mat-cua-ban"
```

## Cấp lại 1 key cho board mới (không tốn thêm suất trong 20)

Dùng khi board cũ của người dùng bị hỏng, cần đổi board mới nhưng vẫn giữ
đúng key đó:

```bash
curl -X POST "https://xxxx.onrender.com/admin/keys/reset?token=..." \
     -H "Content-Type: application/json" \
     -d '{"key": "ABCDEFGHIJ"}'
```

## Test nhanh /activate (giả lập thiết bị gọi lên)

```bash
curl -X POST "https://xxxx.onrender.com/activate" \
     -H "Content-Type: application/json" \
     -d '{"key": "ABCDEFGHIJ", "mac": "AA:BB:CC:DD:EE:FF"}'
```
