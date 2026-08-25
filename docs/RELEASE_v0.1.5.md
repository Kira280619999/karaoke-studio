# Karaoke Studio v0.1.5

Bản phân phối công khai dành cho người dùng mới, giữ nguyên engine PolarFormer FP32 đã kiểm chứng ở v0.1.4 và bổ sung hướng dẫn triển khai đầy đủ.

## Thay đổi chính

- Thêm `HUONG_DAN_CAI_DAT_DAY_DU.md` bằng tiếng Việt.
- Có link tải chính thức, hướng dẫn kiểm SHA-256 cho macOS/Linux/Windows và clone đúng tag.
- Ghi rõ dependency bắt buộc, dung lượng đĩa, cài đặt Apple Silicon, khởi động, tải model lần đầu và vị trí dữ liệu local.
- Ghi rõ thành phần đầy đủ có trong source archive và thành phần được tạo/tải local theo chủ ý bảo mật, bản quyền và tính tương thích nền tảng.
- Bổ sung workflow ngắn, cách dừng/mở lại, xử lý lỗi thường gặp, kiểm thử và cập nhật phiên bản.

## Engine

- Không thay đổi pipeline phân tích, căn lời, renderer hoặc output so với v0.1.4.
- BS PolarFormer FP32 vẫn là stem Karaoke mặc định; ViperX 1297 vẫn là fallback.
- Viewer vẫn mặc định `GỐC`.
- Cấu hình PolarFormer sáu giây/500 ms/static shape/thread thích nghi cho mục tiêu Mac 24 GB được giữ nguyên.

Release gồm source ZIP, source TAR.GZ, file hướng dẫn độc lập và `SHA256SUMS-v0.1.5.txt`. Checkpoint AI, media và dữ liệu cá nhân không được đóng gói lại; app tải/tạo chúng local theo đúng lockfile và manifest.
