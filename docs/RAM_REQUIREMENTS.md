# Ngân sách RAM của Karaoke Studio

## Kết luận

Profile `maximum` dùng được trên máy **16 GB RAM trở lên** khi chỉ chạy một job AI tại một thời điểm. Khuyến nghị đóng bớt ứng dụng rất nặng trong lúc tách giọng/căn lời. Máy 24–32 GB có nhiều khoảng trống hơn, nhưng không phải yêu cầu bắt buộc.

Các stage tách giọng và căn lời chạy tuần tự trong worker. Vì vậy không cộng tất cả mức đỉnh bên dưới lại với nhau.

## Số đo thực tế trên Apple Silicon

Đo ngày 2026-08-23 trên MacBook Pro M5 Pro 64 GB, dùng đúng checkpoint và runtime hiện tại của dự án:

| Stage | Dữ liệu đo | Peak memory footprint |
|---|---:|---:|
| Mel-Band RoFormer / Audio Separator | 30 giây mix thật | **5,25 GB** |
| `htdemucs_ft` (4-model bag) | 30 giây mix thật | **2,75 GB** |
| Hai CTC cùng resident + inference | mỗi model chạy 20 giây vocal thật | **2,71 GB** |

Ngân sách vận hành bảo thủ cho cả API, worker, FFmpeg, proxy và audio buffer là **6–7 GB peak**. Không có swap trong ba phép đo. Bài dài hơn có thể thêm vài trăm MB cho audio/buffer, nhưng separator xử lý theo chunk nên peak không tăng tuyến tính theo toàn bộ thời lượng bài.

## Dung lượng model trên ổ đĩa

| Thành phần | Kích thước gần đúng |
|---|---:|
| `nguyenvulebinh/lyric-alignment` | 1,275 GB |
| `nguyenvulebinh/wav2vec2-base-vietnamese-250h` | 0,378 GB |
| Mel-Band RoFormer | 1,008 GB |
| `htdemucs_ft` (4 checkpoint) | 0,336 GB |
| Toàn bộ data/model hiện có của app | khoảng 3,9 GB |

## Cấu hình khuyến nghị

- **16 GB:** chạy được Maximum; một job AI; tránh chạy đồng thời editor video/AI khác quá nặng.
- **24–32 GB:** thoải mái cho Maximum và thao tác editor song song.
- **64 GB:** không làm timing tự nhiên chính xác hơn; chủ yếu tăng khoảng trống và khả năng chạy công việc khác cùng lúc.

Automatic Sweep Critic không có checkpoint riêng. Nó dùng lại kết quả từ hai CTC và các stem đã tạo, nên chỉ thêm các track RMS/onset nhỏ và không tạo một peak model mới đáng kể.

Các con số là phép đo của phiên bản/runtime được pin ở thời điểm trên, không phải cam kết giống hệt trên mọi hệ điều hành hoặc GPU.
