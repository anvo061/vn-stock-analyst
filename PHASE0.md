# Giai đoạn 0: kiểm chứng nguồn real time và hạ tầng

Trạng thái ngày 07/11/2026 (thứ Bảy, không có phiên): **chưa xong**. Chưa có số đo độ trễ trong phiên.

## Nguồn giá

Một request mỗi endpoint lúc 02:36 ngày 07/11. Thời gian phản hồi lúc thị trường đóng cửa, chưa đại diện cho giờ cao điểm.

| Nguồn | Kết quả | Dữ liệu | Đánh giá tạm |
|---|---|---|---|
| Vietcap `price/symbols/getList` | 200, 640 ms | Nhiều mã, giá khớp, 3 mức giá mua/bán, khối ngoại, có thời điểm sự kiện | Ứng viên nguồn chính |
| KBS `investment/stock/iss` | 200, 427 ms | Nhiều mã, giá khớp, 3 mức giá, khối ngoại, không có thời điểm khớp lệnh | Ứng viên dự phòng |
| Vietcap `LEData/getAll`, KBS `trade/history` | 200 | Lịch sử từng lệnh khớp | Dùng cho chi tiết một mã |
| SSI `stock/exchange/hose` | 200, 730 KB | Cả sàn HOSE, nặng | Không gọi liên tục |
| CafeF `stockhandler.ashx` | 200, 114 KB | Cả sàn, tên trường viết tắt | Dự phòng chậm |
| DNSE nến 1 phút | 200 | Chỉ nến, không có bảng giá | Dùng cho biểu đồ trong ngày |
| VNDirect `stock_prices` | 200 | Giá theo ngày | Không phải real time |
| TCBS `stock-insight`, `tcanalysis` | 404 | | **Loại** |
| TCBS `openapi` | 401 | Cần được cấp quyền, không thử | **Loại ở giai đoạn này** |

Chạy thử 45 giây ngoài phiên trên 5 mã: cả 4 nguồn đo đều thành công 100% và khớp giá. Lần này chỉ xác nhận công cụ và cách đọc trường dữ liệu, các số độ trễ của nó không có ý nghĩa.

Chưa thử WebSocket của các bảng giá web, vì nhịp 3-5 giây không cần tới.

## Kiểm tra giá lịch sử (07/11/2026, phiên gần nhất 06/11)

Vietcap, 5 mã FPT, VCB, HPG, MWG, SSI. Đây là độ chính xác của dữ liệu quá khứ, chưa nói lên độ trễ trong phiên.

| Kiểm tra | Kết quả |
|---|---|
| Giá ngày 1.500 phiên (từ 02/11/2020) | Không có phiên cuối tuần, trùng ngày, ô trống hay O/H/L/C vô lý. Biến động một phiên lớn nhất 7,00-7,06% |
| So với `adClose` của VNDirect, 501 phiên | Lệch tối đa 0,091% (SSI); không thiếu phiên |
| O/H/L/C và khối lượng phiên 06/11 so với VNDirect và KBS | Trùng khớp cả 5 mã |
| Nến 1 phút phiên 06/11 | 226 nến, 09:15-14:45. Tổng khối lượng bằng khối lượng ngày cả 5 mã |
| Nến 1 phút so với DNSE | Giá đóng cửa và khối lượng khớp 226/226 phút. DNSE tính giá theo nghìn đồng |

Khi làm Giai đoạn 1 cần nhớ:
- Lệnh khớp của Vietcap trả tối đa 100 dòng mỗi lần và cần phân trang theo `truncTime`. KBS phân trang theo `page`, `limit` tới 1.000 dòng. Chưa kiểm tra tổng khối lượng khi tải đủ cả phiên.
- Giá ngày là giá đã điều chỉnh, còn nến 1 phút chỉ được đối chiếu ở phiên 06/11, chưa kiểm tra qua ngày chốt quyền.

## Việc còn lại trong phiên

Đo từ 09:30 đến 10:00 và thêm một lần gần 14:30. Ngoài hai lần đo, để `serve run` chạy suốt phiên rồi xem `serve report` để xác nhận Giai đoạn 1 (xem README). Tránh chạy đo và `serve run` cùng lúc, vì sẽ gọi nguồn gấp đôi.

`.venv\Scripts\python.exe -X utf8 -m stock_analyst.measure_realtime --minutes 30`

Công cụ tự chặn khi chạy ngoài phiên, gọi Vietcap/KBS mỗi 3 giây, CafeF mỗi 15 giây, và ngừng gọi nguồn nào trả 401/403/429. Kết quả nằm trong `data/phase0/`.

Tiêu chí chốt nguồn chính: tỷ lệ thành công từ 99%, độ trễ phản hồi p95 dưới 2 giây, không bị chặn, không lệch giá ở cùng khối lượng tích lũy.

## Tunnel

- **Cloudflare Quick Tunnel không hỗ trợ SSE**, không đảm bảo uptime, đổi URL mỗi lần chạy và ai có URL cũng vào được ([tài liệu Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)). Các giới hạn này không áp dụng cho tunnel có tên gắn tài khoản Cloudflare.
- Đã thử ngày 07/11/2026 với trang mẫu, được bạn cho phép, rồi tắt ngay: hỏi 10 lần mỗi 3 giây qua Quick Tunnel đều thành công, trung vị 365 ms, chậm nhất 1,16 giây. SSE qua tunnel có kết nối nhưng sự kiện bị giữ lại, trễ 23-30 giây, nên đúng là không dùng được. Chỉ là mẫu ngắn với trang nhỏ, chưa thử với API và giao diện thật.

| Phương án | Cần gì | Ưu | Nhược |
|---|---|---|---|
| A. Tunnel có tên (đề xuất) | Tài khoản Cloudflare, một tên miền | URL cố định, có SSE, có lớp đăng nhập | Tốn phí tên miền, bạn tự đăng ký |
| B. Quick Tunnel, giao diện tự hỏi giá mỗi 3-5 giây | Không | Miễn phí, không cần tài khoản | URL đổi mỗi lần bật, không đảm bảo uptime, phải tự làm đăng nhập |

**Đã chọn: phương án B (Quick Tunnel miễn phí).** Hệ quả cho thiết kế:

- **Cập nhật giá:** giao diện hỏi backend mỗi 3-5 giây, backend trả từ bộ nhớ đệm và không gọi nguồn thay cho từng người. Không dùng SSE.
- **URL đổi mỗi lần bật:** không ghi URL vào repo công khai. Backend in ra link mời mỗi khi khởi động, dạng `https://<tên>.github.io/<repo>/#api=<URL tunnel>&token=<mã mời>`. Phần sau `#` không được trình duyệt gửi lên GitHub. URL đổi thì phải gửi lại link.
- **Đăng nhập:** ai có URL cũng tới được backend, nên mọi API phải kiểm tra mã mời của từng người, thu hồi được, và chỉ đọc.
- **Thông báo đẩy:** vì URL đổi, đăng ký thông báo phải được làm mới sau mỗi lần bật. Cần kiểm tra ở Giai đoạn 4.
- **Uptime:** không có cam kết, nên giao diện báo rõ khi mất kết nối.
- **Đổi sau:** chuyển sang tunnel có tên khi cần địa chỉ cố định, không phải làm lại giao diện.

## GitHub Pages

- Gói miễn phí chỉ có Pages trên repo công khai ([GitHub Docs](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)). Repo vì vậy không được chứa dữ liệu, báo cáo hay khóa bí mật; dữ liệu chỉ đi từ backend sau khi đăng nhập.
- Pages không dành cho kinh doanh trực tuyến hoặc SaaS; soft limit 100 GB/tháng ([giới hạn Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)). Phù hợp với nhóm nhỏ, không thu phí.

## Quyền dữ liệu

Đã đọc các trang công khai ngày 07/11/2026. Đây không phải tư vấn pháp lý, và không thấy cấm **không có nghĩa là được phép**.

| Nguồn | Tìm thấy |
|---|---|
| SSI | [Điều khoản dịch vụ](https://ssi.com.vn/dieu-khoan-dich-vu) chỉ cho dùng cá nhân, cấm xuất bản, truyền phát hoặc tái sản xuất cho bên thứ ba nếu không có văn bản đồng ý. **Vì vậy đã bỏ SSI iBoard khỏi công cụ đo và sản phẩm** |
| Vietcap | Bản công bố rủi ro cho biết giá lấy từ sở giao dịch và bên thứ ba. Hợp đồng dịch vụ trực tuyến và bản công bố không có điều khoản về quyền dùng lại dữ liệu. Chưa đọc được điều khoản riêng của trading.vietcap.com.vn |
| KBS | Trang chủ không có liên kết điều khoản sử dụng |
| CafeF | Chỉ có dòng bản quyền của VCCorp và chính sách bảo mật, không nói về dữ liệu |
| HOSE | Không tìm thấy quy định công khai về tái phân phối dữ liệu trong lần tìm này |

Dữ liệu giá có thể có chủ quyền từ sở giao dịch, nên việc chia sẻ cho người khác vẫn có rủi ro chưa xác định được. Nên giữ nhóm nhỏ, không thu phí, và hỏi trực tiếp công ty chứng khoán nếu muốn chắc chắn.

- **SSI FastConnect Data** là API chính thức có streaming ([hướng dẫn SSI](https://guide.ssi.com.vn/ssi-products/tieng-viet/fastconnect-data)). Trang [đăng ký](https://guide.ssi.com.vn/ssi-products/tieng-viet/dang-ky-dich-vu) yêu cầu có tài khoản giao dịch tại SSI, đăng ký tại quầy, qua bưu điện hoặc qua môi giới, và có nhắc đến IP cố định. Các trang đã đọc không nói về phí, giới hạn gọi hay quyền chia sẻ lại, nên cần hỏi SSI, nhất là quyền chia sẻ cho người khác.

## Đã thực hiện

Cài git 2.56, cloudflared 2026.10.7 và Node.js 26.13. Thêm `stock_analyst/measure_realtime.py` và test của nó. Chưa tạo repo GitHub, chưa có dữ liệu của bạn rời khỏi máy.
