# Karaoke Studio v0.1.0

Đây là bản phát hành mã nguồn đầu tiên của workflow Karaoke Studio đã được
kiểm tra trực tiếp trên macOS bằng video tiếng Việt thực tế.

## Có trong bản tải

- Toàn bộ frontend, backend FastAPI, renderer, QA và TimelineV1.
- Launcher macOS/Linux `scripts/dev.sh` và Windows `scripts/dev.ps1`.
- Lockfile Python/Node, font Karaoke tiếng Việt, synthetic fixtures và tài liệu.
- BS-RoFormer ViperX 1297 làm stem Final mặc định; Preview mặc định nghe GỐC.
- Video chỉ được đưa vào danh sách tải sau khi render, full decode và QA hoàn tất;
  bản xuất hợp lệ trước đó không bị ghi đè bởi một lượt render đang chạy hoặc lỗi.

## Không đóng gói trong bản tải

- Video/audio/lyrics/background và project cá nhân.
- Database, stem, export, cache và virtual environment của máy phát triển.
- Checkpoint AI. Chúng được tải về máy người dùng trong lần phân tích đầu tiên
  và giữ trong thư mục dữ liệu local.

## Khởi động

Yêu cầu Python 3.12, Node.js 22+, uv, pnpm 11.19.0, FFmpeg và FFprobe.

macOS/Linux:

```bash
./scripts/dev.sh
```

Windows PowerShell:

```powershell
.\scripts\dev.ps1
```

Sau đó mở `http://127.0.0.1:3000`.

Windows bao gồm launcher và CI runtime, nhưng optional AI workflow vẫn được
ghi rõ là preview cho tới khi có một lượt production hoàn chỉnh trên Windows
x64 thật. Không có media nào được gửi lên cloud; API chỉ bind `127.0.0.1`.
