# VN Stock Analyst

Agent nghiên cứu cổ phiếu Việt Nam (HOSE, HNX, UPCoM): cào tin mỗi ngày, đọc và phân tích báo cáo tài chính, phân tích kỹ thuật.

Phần Python lấy dữ liệu và tính toán mọi con số. Subagent Claude `VN Stock Analyst` đọc kết quả và viết phân tích, nên không cần API key riêng.

Đây là công cụ hỗ trợ nghiên cứu, không phải tư vấn đầu tư. Agent không đưa khuyến nghị mua/bán, giá mục tiêu hay tỷ trọng.

## Cách dùng

Trong Claude Code, chỉ cần nói:

- `Dùng VN Stock Analyst phân tích HPG`
- `Dùng VN Stock Analyst tạo bản tin hôm nay`
- `Dùng VN Stock Analyst đọc BCTC quý 3/2026 của FPT, tập trung vào thuyết minh`

Định nghĩa subagent nằm ở [agent/vn-stock-analyst.md](agent/vn-stock-analyst.md) và được cài vào `~/.claude/agents/`. Sửa file trong dự án thì nhớ chép đè sang đó.

Đổi danh mục theo dõi trong [config/watchlist.json](config/watchlist.json). `aliases` là tên thường gặp trên báo, dùng để gắn mã vào tin.

## Chạy CLI trực tiếp

Từ thư mục `stock-analyst`:

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.cli daily
```

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.cli brief FPT
```

| Lệnh | Việc làm |
|---|---|
| `crawl` | Cào tin từ 9 nguồn RSS và sitemap tin của FireAnt vào `data/news.db` |
| `is-trading-day` | Hôm nay (giờ VN) có phải ngày giao dịch: thứ Hai-thứ Sáu, không có trong `config/holidays.json`, và từ 09:15 nguồn giá có lệnh khớp hôm nay. Không đọc được nguồn giá thì trả `false` |
| `retag [--dry-run]` | Gắn lại mã cho mọi tin đã lưu theo danh sách mã và `STOPLIST` hiện tại; `--dry-run` chỉ xem trước |
| `news --symbol FPT --hours 72` | Truy vấn tin đã cào; có thêm `--keyword`, `--limit` |
| `article URL` | Lấy toàn văn một bài báo |
| `fundamentals FPT` | Phân tích BCTC, tự nhận loại hình doanh nghiệp thường, ngân hàng, chứng khoán |
| `reports FPT --year 2026` | Liệt kê file PDF BCTC |
| `read-report FPT URL` | Tải PDF và trích lớp chữ |
| `render-report FPT URL --pages 3-8` | Render trang PDF thành PNG để đọc bản scan |
| `technical FPT` | Chỉ báo, xu hướng, hỗ trợ/kháng cự, tín hiệu, biểu đồ |
| `brief FPT` | Gói kỹ thuật, BCTC và tin của một mã |
| `daily` | Cào tin và tạo gói dữ liệu cho cả danh mục trong `reports/<ngày>/` |

## Chuỗi 5 agent lúc 15:00

Lịch `vn-trading-pipeline-1500` chạy 15:00 (thực tế 15:03) thứ Hai-thứ Sáu khi app Claude đang mở; nếu app tắt thì chạy ở lần mở sau. Bắt đầu bằng `is-trading-day`, sau đó lần lượt: `vn-news-scout` → `vn-news-classifier` → `vn-fundamental-advisor` → `vn-technical-specialist` → `vn-trade-strategist`. Kết quả nằm trong `reports/<ngày>/` (01-04 JSON, `FINAL_TRADING_REPORT.md`) và `reports/LATEST_TRADING_REPORT.md`; nhật ký ở `reports/schedule_log.md`. Định nghĩa agent ở [agent/](agent/), bản cài ở `~/.claude/agents/`.

`technical` trả thêm 8 nhóm chỉ báo (`ma20`, `ma50`, `rsi`, `macd`, `bollinger_bands`, `volume_analysis`, `ichimoku`, `fibonacci`) và `trade_levels` (vùng mua, mục tiêu 1/2, cắt lỗ, R:R) tính theo quy tắc cố định ghi trong `trade_levels.rules`. Khuyến nghị trong báo cáo là tín hiệu theo quy tắc để bạn tự tham khảo, không phải tư vấn đầu tư cá nhân.

## Backend giá real time

Một bộ thu thập lấy giá từ nguồn trong [config/live.json](config/live.json), chuyển nguồn dự phòng khi lỗi, và phục vụ mọi người xem từ bộ nhớ. Server chỉ nghe trên `127.0.0.1`.

Phủ toàn bộ cổ phiếu đang niêm yết trên HOSE, HNX, UPCoM (1.516 mã ngày 07/11/2026; không gồm chứng quyền, trái phiếu, ETF và mã đã hủy niêm yết). Trong giờ giao dịch, mỗi 3 giây gọi nguồn một lần, gồm danh mục theo dõi và các mã đang có người xem (tối đa 100 mã, cập nhật mỗi 3 giây) cùng 250 mã kế tiếp của phần còn lại, nên cả thị trường quay hết một vòng dưới 20 giây. Danh sách mã được làm mới mỗi ngày và lưu trong `data/live/universe.json`. Chế độ phát lại chỉ có các mã trong danh mục.

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.serve run
```

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.serve run --replay 2026-11-06 --speed 60
```

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.serve invite add "Tên" --web https://ten.github.io/bang-dien/ --api https://abc.trycloudflare.com
```

Lệnh trên in ra cả mã mời lẫn link hoàn chỉnh `<web>/#api=<backend>&token=<mã>`. Bỏ `--web` thì chỉ in mã. Đặt `"web_url"` trong `config/live.json` để khỏi gõ `--web` mỗi lần. Khi địa chỉ tunnel đổi, ghép lại link cho mã đang dùng (mã phải còn hiệu lực):

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.serve invite link --token <mã> --api https://xyz.trycloudflare.com
```

```bash
.venv\Scripts\python.exe -X utf8 -m stock_analyst.serve report
```

| API | Quyền | Nội dung |
|---|---|---|
| `GET /api/health` | Công khai | Chỉ báo server còn sống |
| `GET /api/symbols` | Mã mời | Toàn bộ mã kèm sàn và tên công ty, cùng danh mục theo dõi |
| `GET /api/quotes?symbols=FPT,VCB` | Mã mời | Tối đa 100 mã: giá, 3 mức mua/bán, khối ngoại, tuổi dữ liệu, trạng thái nguồn. `price` là `null` nếu mã chưa có lệnh khớp |
| `GET /api/bars?symbol=FPT&interval=1D` | Mã mời | Nến `1D` hoặc `1m` của một mã đang niêm yết |
| `GET /api/news?symbol=FPT&hours=72&limit=30` | Mã mời | Tin đã cào: chỉ tiêu đề, nguồn, giờ, link và mã được gắn; không trả nội dung bài. Bỏ `symbol` để lấy tin chung |
| `GET /api/fundamentals?symbol=FPT` | Mã mời | Kết quả phân tích BCTC đầy đủ, gồm `warnings`, `red_flags`, `integrity`. Lưu cache 6 giờ; nguồn lỗi thì trả bản đã lưu kèm cảnh báo |
| `GET /api/status` | Mã mời | Thống kê nguồn, lỗi gần nhất, nhật ký chuyển nguồn, hạn của lịch nghỉ lễ |

Mã mời gửi qua header `Authorization: Bearer <mã>`. Mã chỉ hiện một lần khi tạo; đĩa chỉ lưu bản băm. Thu hồi bằng `invite revoke "Tên"`, có hiệu lực ngay.

Giao diện phải kiểm tra `feed.status` và `feed.mode`: `offline` nghĩa là giá có thể đã cũ, `replay` nghĩa là dữ liệu phiên cũ, không phải giá đang chạy.

Ngày nghỉ lễ nằm trong [config/holidays.json](config/holidays.json): 71 ngày từ 2020 đến 2026 suy ra từ dữ liệu giao dịch thật, cộng 01/01/2027. Vào ngày lễ, server gọi nguồn 60 giây một lần và không coi dữ liệu là "đứng yên". Lịch Tết 2027 **chưa có**: khi HOSE công bố, thêm vào mục `announced`, sửa `covers_until`, rồi khởi động lại server. Từ 45 ngày trước khi lịch hết hạn, server in cảnh báo lúc khởi động và trong `/api/status`.

## Giao diện bảng điện

Mã nguồn trong [web/](web/), dùng React, Vite và TradingView Lightweight Charts. Cần Node.js. Trong `web/`, cài gói một lần rồi chạy bản phát triển, và mở `http://localhost:5173/#token=<mã mời>` khi backend đang chạy:

```bash
npm install
```

```bash
npm run dev
```

Test và đóng gói:

```bash
npm test
```

```bash
npm run build
```

Mỗi người tự chỉnh danh mục của mình bằng ngôi sao ☆ và nút **Sắp xếp** trong tab Danh mục. Danh mục này lưu trong trình duyệt, không đồng bộ giữa các thiết bị và mất nếu xóa dữ liệu trình duyệt. Khác với `config/watchlist.json`, nơi quy định danh mục mặc định, các mã luôn được lưu lịch sử và bản tin hằng ngày.

Link mời có dạng `<địa chỉ web>/#api=<địa chỉ backend>&token=<mã mời>`. Mã được đọc rồi xóa khỏi thanh địa chỉ và lưu trong trình duyệt của người dùng. Khi đưa lên web, phải thêm địa chỉ web vào `allowed_origins` trong `config/live.json`.

`web/public/sw.js` chỉ lưu file giao diện để mở offline, không bao giờ lưu giá. Chưa kiểm chứng được phần này trên trình duyệt thật.

## Giới hạn cần biết

- **Tin FireAnt chỉ có tiêu đề, giờ và link:** đọc từ sitemap công khai, mỗi 30 phút tối đa một lần, tự dừng 24 giờ nếu bị từ chối. Toàn văn chỉ lấy khi phân tích một bài và không được lưu. Điều khoản của FireAnt không nói về truy cập tự động, nên khi chia sẻ chỉ hiện tiêu đề kèm link.

- **Nguồn dữ liệu là API công khai không chính thức** của Vietcap, KBS, VNDirect và CafeF, không có cam kết ổn định và có thể đổi bất cứ lúc nào. Khi đó test `-m live` sẽ báo lỗi.
- **Giá lịch sử đã điều chỉnh** cổ tức và chia tách, nên đỉnh, đáy, hỗ trợ và kháng cự không phải giá khớp thực tế.
- **Phần lớn PDF BCTC là bản scan.** `read-report` chỉ báo `scanned=true`; agent đọc ảnh qua `render-report`. Không có OCR, nên đây là việc đọc từng trang chứ không phải trích số hàng loạt.
- **So sánh cùng kỳ có thể sai lệch** khi đổi phạm vi hợp nhất. Module chỉ cảnh báo, số so sánh được phải đọc trong file BCTC. API cũng có thể chưa cập nhật số liệu điều chỉnh sau kiểm toán.
- **Gắn mã vào tin ưu tiên độ chính xác**, nên bỏ sót nhiều: khoảng 13% tin được gắn mã. Danh sách từ loại trừ soạn tay từ dữ liệu một ngày và chưa đo độ chính xác. Tên công ty ngoài danh mục theo dõi không được nhận diện.
- **Ngưỡng cờ cảnh báo và tín hiệu là quy tắc tự đặt**, chưa hiệu chỉnh trên mẫu rộng. Công ty bảo hiểm chưa được hỗ trợ tính chỉ số. `signal_history` là thống kê mô tả trên mẫu nhỏ, không phải bằng chứng dự báo.

## Phát triển

```bash
.venv\Scripts\python.exe -X utf8 -m pytest -q
```

```bash
.venv\Scripts\python.exe -X utf8 -m pytest -q -m live
```

Cài lại môi trường:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Giao diện module và nguồn dữ liệu nằm trong [CONTRACT.md](CONTRACT.md).
