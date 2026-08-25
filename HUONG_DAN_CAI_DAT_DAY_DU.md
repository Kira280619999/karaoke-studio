# Hướng dẫn tải và cài đặt Karaoke Studio đầy đủ

Tài liệu này dành cho người nhận link lần đầu và muốn cài Karaoke Studio trên máy riêng. Bản phát hành ổn định hiện tại là **v0.1.5**.

## 1. Link tải chính thức

- Trang Release duy nhất nên dùng: <https://github.com/Kira280619999/karaoke-studio/releases/tag/v0.1.5>
- ZIP cho macOS/Windows: <https://github.com/Kira280619999/karaoke-studio/releases/download/v0.1.5/Karaoke-Studio-v0.1.5-source.zip>
- TAR.GZ cho macOS/Linux: <https://github.com/Kira280619999/karaoke-studio/releases/download/v0.1.5/Karaoke-Studio-v0.1.5-source.tar.gz>
- File checksum: <https://github.com/Kira280619999/karaoke-studio/releases/download/v0.1.5/SHA256SUMS-v0.1.5.txt>

Không tải source từ website lạ hoặc link được upload lại. Repository chính thức là <https://github.com/Kira280619999/karaoke-studio>.

## 2. “Bản đầy đủ” gồm những gì?

ZIP/TAR.GZ chứa toàn bộ phần cần thiết để dựng app từ source:

- frontend React/Vinext;
- backend FastAPI và worker xử lý media;
- renderer Karaoke 30/60/120 fps;
- timeline editor, batch studio và toàn bộ API;
- launcher macOS/Linux `scripts/dev.sh`;
- launcher Windows `scripts/dev.ps1`;
- dependency lockfiles `uv.lock` và `pnpm-lock.yaml`;
- năm font tiếng Việt cùng giấy phép OFL;
- test, tài liệu kiến trúc, bảo mật và thông báo giấy phép.

Các thành phần sau **không nằm trong GitHub Release** và đây là chủ ý an toàn, không phải thiếu file:

- video/audio/lời bài hát và project riêng của chủ repository;
- database, stem, proxy và video đã xuất;
- `.venv`, `node_modules` và cache phụ thuộc theo từng hệ điều hành;
- checkpoint AI có giấy phép riêng.

Launcher tạo lại dependency từ lockfile. Checkpoint AI được app tải trực tiếp từ nguồn đã pin trong lần phân tích đầu, sau đó kiểm tra kích thước hoặc SHA-256 trước khi chạy. Vì vậy người nhận có app trọn vẹn nhưng không nhận nhầm dữ liệu cá nhân hoặc media có bản quyền của người khác.

## 3. Cấu hình máy khuyến nghị

### macOS

- Apple Silicon M1 trở lên; M5 Pro 24 GB là cấu hình mục tiêu đã được tối ưu.
- macOS 13 trở lên.
- RAM tối thiểu 16 GB; khuyến nghị 24 GB.
- Còn trống ít nhất 20 GB; nên có 25–30 GB cho dependency, model, project và export.
- Mạng ổn định trong lần cài và phân tích đầu tiên.

### Windows

- Windows 10/11 x64.
- RAM tối thiểu 16 GB.
- CPU chạy được; NVIDIA CUDA có thể được dùng khi runtime/driver hợp lệ.
- Đường AI trên Windows vẫn được xem là preview cho đến khi có một production run native đầy đủ trên máy đích.

## 4. Kiểm tra file tải xuống

Tải file source và `SHA256SUMS-v0.1.5.txt` vào cùng một thư mục.

### Kiểm tra ZIP trên macOS/Linux

```bash
cd ~/Downloads
grep 'source.zip' SHA256SUMS-v0.1.5.txt | shasum -a 256 -c -
```

Kết quả đúng phải có:

```text
Karaoke-Studio-v0.1.5-source.zip: OK
```

### Kiểm tra TAR.GZ trên macOS/Linux

```bash
cd ~/Downloads
grep 'source.tar.gz' SHA256SUMS-v0.1.5.txt | shasum -a 256 -c -
```

### Kiểm tra ZIP trên Windows PowerShell

```powershell
cd $HOME\Downloads
Get-FileHash .\Karaoke-Studio-v0.1.5-source.zip -Algorithm SHA256
Get-Content .\SHA256SUMS-v0.1.5.txt
```

Hai chuỗi SHA-256 của file ZIP phải giống nhau hoàn toàn. Nếu không giống, xoá file và tải lại từ Release chính thức.

## 5. Cài đặt trên macOS Apple Silicon

### Bước 1 — cài công cụ hệ thống

Mở Terminal và cài Xcode Command Line Tools nếu máy chưa có:

```bash
xcode-select --install
```

Có thể dùng Homebrew để cài các dependency bắt buộc:

```bash
brew install uv ffmpeg node@22
brew link --overwrite --force node@22
npm install --global pnpm@11.19.0
```

Nếu chưa có Homebrew, dùng hướng dẫn chính thức tại <https://docs.brew.sh/Installation>. Có thể cài Node.js 22 bằng installer chính thức tại <https://nodejs.org/en/download> thay cho Homebrew. Hướng dẫn `uv` chính thức ở <https://docs.astral.sh/uv/getting-started/installation/> và pnpm ở <https://pnpm.io/installation>.

Kiểm tra trước khi tiếp tục:

```bash
uv --version
node --version
pnpm --version
ffmpeg -version
ffprobe -version
```

Node phải từ `22.13.0` trở lên. Project khóa pnpm ở `11.19.0`.

### Bước 2 — giải nén và mở đúng thư mục

Giải nén ZIP. Sau đó trong Terminal:

```bash
cd ~/Downloads/Karaoke-Studio-v0.1.5
chmod +x scripts/dev.sh
```

Nếu đặt thư mục ở vị trí khác, kéo thư mục đó từ Finder vào Terminal sau lệnh `cd ` để lấy đúng đường dẫn.

### Bước 3 — cài đúng dependency đã khóa

```bash
uv python install 3.12
uv sync --frozen --dev --extra quality --extra alignment
pnpm install --frozen-lockfile
```

Không cần tự tạo virtual environment; `uv` tạo `.venv` trong project. Không chạy `pip install` hoặc nâng version package thủ công vì sẽ làm môi trường lệch khỏi bản đã kiểm thử.

### Bước 4 — khởi động app

```bash
./scripts/dev.sh
```

Giữ Terminal này mở trong lúc dùng. Khi launcher báo sẵn sàng, mở URL frontend được in trong Terminal, thường là:

```text
http://127.0.0.1:3000
```

Backend thường ở:

```text
http://127.0.0.1:8000/api/health
```

Health đúng cho bản này phải trả về `status: ok` và `version: 0.1.5`.

### Bước 5 — tải model lần đầu

1. Bấm `＋ Import` hoặc `Tạo mới`.
2. Chọn video và timeline LRC/SRT/VTT/TXT, hoặc dán timestamp trực tiếp.
3. Đọc phần giấy phép và chỉ bật Maximum Accuracy khi mục đích sử dụng phù hợp.
4. Bắt đầu phân tích và giữ máy thức, cắm sạc, không đóng Terminal.

Lần đầu lâu hơn vì app phải tải model. Những lần sau dùng cache local. PolarFormer FP32 được tải vào `.karaoke-studio-data/models/`, kiểm tra đúng checkpoint rồi mới tạo stem Karaoke. Các model căn lời có giấy phép riêng; model CTC tiếng Việt là CC BY-NC 4.0 và không dành cho mục đích thương mại.

## 6. Cài đặt trên Windows 10/11 x64

Cài trước:

- Node.js 22+ từ <https://nodejs.org/en/download>;
- `uv` theo <https://docs.astral.sh/uv/getting-started/installation/>;
- pnpm `11.19.0`;
- FFmpeg/FFprobe x64 và thêm vào `PATH`.

Mở PowerShell trong thư mục đã giải nén rồi chạy:

```powershell
npm install --global pnpm@11.19.0
.\scripts\dev.ps1
```

Nếu Execution Policy chặn launcher, chỉ bỏ qua cho lần chạy này:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

Launcher tự đồng bộ dependency đã khóa và chọn cổng local trống. Dữ liệu mặc định nằm ở `%LOCALAPPDATA%\KaraokeStudio`.

## 7. Cách dùng ngắn gọn

1. Import một video và một nguồn timeline.
2. Điền tên bài hát và ca sĩ/nguồn.
3. Chạy phân tích.
4. Viewer mặc định nghe `GỐC` để kiểm timing; chuyển sang `KARAOKE` để nghe stem loại giọng.
5. Sửa và duyệt timing trong Magnetic Lyric Timeline.
6. Chọn preset 1080p60 hoặc 1080p120 CFR.
7. Xuất bản có ca sĩ hoặc bản Karaoke loại giọng.
8. Chỉ tải MP4 sau khi render và QA hoàn tất.

## 8. Dữ liệu được lưu ở đâu?

### macOS/Linux

```text
<thư mục project>/.karaoke-studio-data/
```

### Windows

```text
%LOCALAPPDATA%\KaraokeStudio
```

Thư mục dữ liệu chứa database, nguồn, model, stem, project và export. Không commit hoặc upload thư mục này lên GitHub.

## 9. Dừng và mở lại

Để dừng đúng cách, quay lại Terminal đang chạy và nhấn `Ctrl+C` một lần. Không cần xoá `.venv`, `node_modules` hoặc model.

Lần sau chỉ cần:

```bash
cd /duong/dan/toi/Karaoke-Studio-v0.1.5
./scripts/dev.sh
```

## 10. Xử lý lỗi thường gặp

### `command not found: uv`, `pnpm`, `ffmpeg` hoặc `ffprobe`

Đóng và mở lại Terminal sau khi cài. Chạy lại năm lệnh kiểm tra ở Bước 1. Nếu vẫn thiếu, kiểm tra `PATH` của công cụ đó.

### Node quá cũ

```bash
node --version
```

Nếu thấp hơn 22.13, nâng Node.js rồi chạy lại `pnpm install --frozen-lockfile`.

### Cổng 3000 hoặc 8000 đang được dùng

Không tắt tiến trình lạ một cách tùy tiện. Đọc URL thật mà launcher in ra; launcher có thể chọn cổng kế tiếp. Đóng instance Karaoke Studio cũ bằng `Ctrl+C` nếu chính nó đang giữ cổng.

### Tải model bị gián đoạn

Giữ file tạm và chạy lại phân tích; downloader sẽ tiếp tục khi backend hỗ trợ resume. Kiểm tra mạng và dung lượng ổ đĩa. Không đổi tên hoặc chép checkpoint từ nguồn không rõ ràng.

### Máy 24 GB dùng nhiều RAM

- Chạy một job Maximum Accuracy tại một thời điểm.
- Đóng ứng dụng AI/video nặng khác.
- Cắm sạc và để macOS tự quản lý bộ nhớ.
- Không tự tăng `KARAOKE_POLARFORMER_CHUNK_SECONDS` hoặc số thread.

PolarFormer FP32 v0.1.5 dùng cửa sổ cố định sáu giây, overlap 500 ms, static shape và số thread tự thích nghi tối đa 10. Peak model process đo được khoảng 4,98 GiB; độ dài bài chủ yếu làm tăng thời gian, không làm RAM tăng tuyến tính theo toàn bài.

### Giao diện mở nhưng backend chưa sẵn sàng

Mở URL `/api/health` mà Terminal in ra. Nếu không nhận được JSON `status: ok`, đọc lỗi trong Terminal backend trước khi import lại.

## 11. Kiểm tra dành cho người phát triển

Sau khi cài dependency:

```bash
pnpm run check
```

Lệnh này chạy Python lint, frontend lint, TypeScript, 66 frontend tests, backend tests và production build. CI GitHub cũng chạy trên macOS, Linux và Windows cho mỗi commit vào `main`.

## 12. Cập nhật phiên bản sau này

Người dùng Release ZIP nên tải Release mới vào một thư mục mới, chạy cài dependency theo lockfile rồi mới chép project cá nhân khi thật sự cần. Không ghi đè source mới lên source cũ đang chạy.

Người dùng Git có thể clone đúng bản ổn định:

```bash
git clone --branch v0.1.5 --depth 1 https://github.com/Kira280619999/karaoke-studio.git
cd karaoke-studio
./scripts/dev.sh
```

## 13. Quyền riêng tư và giấy phép

- App bind ở `127.0.0.1`; media không được upload lên cloud bởi Karaoke Studio.
- Chỉ dùng video, audio, lyrics và hình ảnh mà bạn có quyền sử dụng.
- Source code là MIT; font, dependency và model giữ giấy phép riêng.
- Không separator AI nào đảm bảo loại sạch 100% giọng hát; luôn nghe lại trước khi phát hành.
- `Verified` là nhãn chất lượng sau kiểm duyệt, không phải cam kết AI đúng tuyệt đối.

Chi tiết thêm: [README](README.md), [ngân sách RAM](docs/RAM_REQUIREMENTS.md), [kiến trúc](docs/ARCHITECTURE.md), [bảo mật](SECURITY.md) và [giấy phép bên thứ ba](THIRD_PARTY_NOTICES.md).
