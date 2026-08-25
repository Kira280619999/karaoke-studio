# Karaoke Studio v0.1.3

Bản PolarFormer giới hạn bộ nhớ dành cho máy Apple Silicon 24 GB, kèm bản vá kiểm thử phát hành đa nền tảng.

## Thay đổi chính

- Thay cửa sổ inference 20 giây có thể vượt 10 GiB bằng cửa sổ FP32 4 giây.
- Ghép các cửa sổ bằng overlap equal-power 1 giây để không tạo tiếng bật hoặc đường nối.
- Đọc và ghi audio theo luồng; độ dài bài chỉ làm tăng thời gian, không làm RAM tăng dần.
- Tắt ONNX CPU memory arena và memory pattern; chạy graph tuần tự với số thread hữu hạn.
- Stem được ghi vào file tạm rồi thay thế nguyên tử, tránh để lại WAV hỏng khi người dùng hủy.
- Tiến độ từng cửa sổ được đưa vào job để giao diện không đứng im trong lúc xử lý.
- ViperX 1297 vẫn chỉ là fallback nếu PolarFormer thực sự thất bại.
- Test streaming dùng runtime giả lập nhẹ trên CI thường; runtime AI thật vẫn được kiểm tra ở job riêng.

## Xác minh trên audio thật

- Đoạn 12 giây và 30 giây đều có peak RSS khoảng 3,242 GiB; không tăng sau 10 cửa sổ.
- `instrumental + vocals` khôi phục nguồn với SNR khoảng 158,5 dB.
- Đủ frame, stereo 44,1 kHz, không có mẫu không hữu hạn và không có spike ở các điểm nối.
- Toàn bộ lint, TypeScript, 66 frontend tests, 121 backend tests và production build đều đạt local.

Checkpoint 201 MiB vẫn được tải local và không nằm trong GitHub Release hoặc source archive.
