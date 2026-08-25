# Karaoke Studio v0.1.2

Bản vá PolarFormer giới hạn bộ nhớ dành cho máy Apple Silicon 24 GB.

## Thay đổi chính

- Thay cửa sổ inference 20 giây có thể vượt 10 GiB bằng cửa sổ FP32 4 giây.
- Ghép các cửa sổ bằng overlap equal-power 1 giây để không tạo tiếng bật hoặc đường nối.
- Đọc và ghi audio theo luồng; độ dài bài chỉ làm tăng thời gian, không làm RAM tăng dần.
- Tắt ONNX CPU memory arena và memory pattern; chạy graph tuần tự với số thread hữu hạn.
- Stem được ghi vào file tạm rồi thay thế nguyên tử, tránh để lại WAV hỏng khi người dùng hủy.
- Tiến độ từng cửa sổ được đưa vào job để giao diện không đứng im trong lúc xử lý.
- ViperX 1297 vẫn chỉ là fallback nếu PolarFormer thực sự thất bại.

## Xác minh trên audio thật

- Đoạn 12 giây và 30 giây đều có peak RSS khoảng 3,242 GiB; không tăng sau 10 cửa sổ.
- `instrumental + vocals` khôi phục nguồn với SNR khoảng 158,5 dB.
- Đủ frame, stereo 44,1 kHz, không có mẫu không hữu hạn và không có spike ở các điểm nối.
- Toàn bộ lint, TypeScript, frontend tests, backend tests và production build đều đạt.

Checkpoint 201 MiB vẫn được tải local và không nằm trong GitHub Release hoặc source archive.
