# Karaoke Studio v0.1.1

Bản cập nhật stem Final chất lượng cao, giữ nguyên kiến trúc phân tích timing đã
được xác nhận ở v0.1.0.

## Thay đổi chính

- BS PolarFormer 62-band FP32 ONNX trở thành stem Karaoke mặc định cho Preview
  và Final; Mel-Band RoFormer, Demucs và căn lời không thay đổi.
- Model MIT 201 MiB được tải local có thể tiếp tục, pin theo revision và chỉ
  được dùng sau khi khớp chính xác kích thước cùng SHA-256.
- ViperX 1297 vẫn được giữ làm fallback; app không chạy đồng thời cả hai model
  export-only nên tránh tốn RAM và thời gian không cần thiết.
- Stem được lưu dưới dạng WAV float32 44,1 kHz trước khi renderer ghép video.
- Viewer vẫn mặc định nghe `GỐC`; tab Audio và Share hiển thị chính xác model
  Final đang được khóa.

## Xác minh

- ONNX Runtime tải đúng graph FP32 và chạy inference thật thành công.
- Smoke test nhạc không vocal đạt correlation `0.99999999999`, SNR khoảng
  `108,3 dB` và duration đầu ra khớp chính xác.
- Toàn bộ lint, TypeScript, 66 frontend tests, backend tests và production build
  đều đạt.

Checkpoint không nằm trong source archive hoặc Git. Lần phân tích đầu tiên sẽ
tải model vào thư mục dữ liệu local của Karaoke Studio.
