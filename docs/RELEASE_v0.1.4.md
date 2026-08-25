# Karaoke Studio v0.1.4

Bản tối ưu BS PolarFormer FP32 cho mục tiêu MacBook Pro M5 Pro 24 GB: nhanh hơn rõ rệt, giữ graph FP32 và tránh backend tăng tốc có peak RAM không an toàn.

## Thay đổi chính

- Dùng cửa sổ FP32 cố định sáu giây và overlap equal-power 500 ms.
- Khóa static shape `batch=1` và `time_frames=517` để ONNX Runtime không phải giải lại shape động ở từng đoạn.
- Số thread tự thích nghi theo CPU, tối đa 10; tắt thread spinning để không chiếm CPU khi chờ.
- Tiếp tục tắt CPU memory arena/memory pattern và ghi stem theo luồng để RAM không tăng theo độ dài bài.
- Giữ nguyên model, revision, FP32, STFT/iSTFT và kiến trúc phân tích/căn lời; thay đổi chỉ nằm trong runtime tạo stem PolarFormer cho Preview/Final.
- Không chọn CoreML vì peak gần 19 GiB trong benchmark graph thật; không chọn WebGPU vì đường đọc output native chưa ổn định.

## Xác minh trên audio thật

- Audio stereo 30 giây chạy **22,68 giây**, so với **34,37 giây** ở v0.1.3: nhanh hơn khoảng **34%**.
- Peak model process khoảng **5,35 GB / 4,98 GiB RSS**, phù hợp ngân sách máy đích 24 GB khi chạy một job.
- `instrumental + vocals` khôi phục nguồn ở **151,93 dB SNR**.
- Instrumental mới so với v0.1.3 đạt **110,86 dB SNR** và correlation `0.999999999996`.
- Mép nối tệ nhất chỉ bằng `0,54×` biến thiên p99 cục bộ, không tạo spike mới.

Checkpoint 201 MiB vẫn được tải local, kiểm tra SHA-256 và không nằm trong GitHub Release hoặc source archive.
