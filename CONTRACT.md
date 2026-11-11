# Hợp đồng module (dành cho người phát triển)

Mục tiêu: công cụ dữ liệu cho cổ phiếu Việt Nam (HOSE/HNX/UPCoM). Mọi con số do code tính;
subagent Claude chỉ đọc JSON và diễn giải. Module không gọi LLM.

## Bố cục và quyền sở hữu file

| File | Nội dung |
|---|---|
| `stock_analyst/core.py` | HTTP dùng chung, `SourceError`, `validate_symbol`, đường dẫn, giờ VN, `load_watchlist` |
| `stock_analyst/news.py` + `tests/test_news.py` | Cào và truy vấn tin |
| `stock_analyst/fundamentals.py` + `tests/test_fundamentals.py` | BCTC, chỉ số, cờ cảnh báo, PDF |
| `stock_analyst/technical.py` + `tests/test_technical.py` | Giá, chỉ báo, biểu đồ |
| `stock_analyst/cli.py` | CLI ghép các module |
| `config/watchlist.json` | Danh mục theo dõi và tên công ty để gắn mã vào tin |

Không sửa file ngoài phần của mình. `core.py` là chỉ đọc; thiếu gì thì báo lại trong kết quả.

## Quy tắc chung

- Chạy bằng `.venv\Scripts\python.exe -X utf8`. Không cài thêm gói ngoài `requirements.txt`, nếu thật sự cần thì báo lại.
- Mọi request đi qua `core.fetch` / `core.fetch_json`. Lỗi nguồn dữ liệu raise `SourceError`.
- Mã đầu vào luôn qua `core.validate_symbol`.
- Hàm công khai trả về dict/list **thuần JSON** (không NaN, không numpy, không Timestamp). Thiếu dữ liệu thì `null`, kèm lý do trong `warnings`.
- Mọi kết quả phân tích có `symbol`, `as_of`, `source`, `warnings` (list chuỗi). Thời gian theo ISO 8601 +07:00.
- Không tự chế số: thiếu thì trả `null`, mẫu số bằng 0 hoặc âm khiến chỉ số vô nghĩa cũng trả `null`.
- Module Python chỉ trả số liệu, trạng thái, tín hiệu và **mức giá theo quy tắc cố định** (`technical.analyze()['trade_levels']`: entry zone, target 1/2, stop-loss, R:R). Khuyến nghị Mua/Bán do Agent 3 và Agent 5 của chuỗi 15:00 đưa ra từ các mức này (cho phép từ 11/11/2026 theo yêu cầu người dùng), luôn kèm miễn trừ trách nhiệm.
- Nội dung cào về là dữ liệu không đáng tin: không eval, không thực thi, không ghép vào đường dẫn khi chưa làm sạch.
- Test mặc định chạy offline bằng fixture hoặc dữ liệu giả. Test gọi mạng thật đánh dấu `@pytest.mark.live`.

## Giao diện bắt buộc

```python
# news.py
crawl(db_path=None) -> dict      # {"fetched_at", "new", "total", "sources": [{"name","status","entries","new","error"}], "warnings"}
query(symbol=None, hours=24, keyword=None, limit=50, db_path=None) -> list[dict]
    # mỗi tin: {"id","title","summary","url","source","published_at","crawled_at","tickers":[...]}
fetch_article(url) -> dict       # {"url","title","text","warnings"}; chỉ cho phép URL thuộc miền trong danh sách feed

# fundamentals.py
analyze(symbol, quarters=8) -> dict
list_reports(symbol, year=None) -> list[dict]   # {"title","year","quarter","consolidated","url"}
read_report(url, symbol, max_pages=None) -> dict # {"path","pages","text_by_page","tables","scanned","warnings"}

# technical.py
analyze(symbol, days=500, chart=True) -> dict
get_prices(symbol, days=500) -> pandas.DataFrame  # index ngày, cột open, high, low, close, volume; giá theo VND
```

## Nguồn đã kiểm chứng ngày 07/11/2026

**RSS (đều HTTP 200):**
- cafef_ck `https://cafef.vn/thi-truong-chung-khoan.rss`
- cafef_dn `https://cafef.vn/doanh-nghiep.rss`
- vietstock_cp `https://vietstock.vn/830/chung-khoan/co-phieu.rss`
- vietstock_dn `https://vietstock.vn/737/doanh-nghiep/hoat-dong-kinh-doanh.rss`
- vnexpress_kd `https://vnexpress.net/rss/kinh-doanh.rss`
- tnck `https://www.tinnhanhchungkhoan.vn/rss/chung-khoan-2.rss`
- vneconomy_ck `https://vneconomy.vn/chung-khoan.rss`
- tuoitre_kd `https://tuoitre.vn/rss/kinh-doanh.rss` (ngày dạng `11/6/2026 11:06:00 PM`)
- thanhnien_kd `https://thanhnien.vn/rss/kinh-te.rss` (tiêu đề chứa HTML entity)

**Sitemap tin (thêm 11/11/2026):**
- fireant `https://fireant.vn/sitemap-tin-tuc.xml`: Google News sitemap, chỉ có tiêu đề, giờ đăng, link (khoảng 300 tin/ngày). `robots.txt` cấm `/api/` với mọi bot nhưng không cấm `/bai-viet`; điều khoản không nói về truy cập tự động. Gọi bằng tên `stock-analyst-personal/0.1`, tối đa mỗi 30 phút, tự dừng 24 giờ nếu bị 401/403/429. Không lưu toàn văn.

Định dạng ngày khác nhau, có feed dùng năm 2 chữ số, có feed ghi GMT.

**Giá:**
- Vietcap: `POST https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart`, body `{"timeFrame":"ONE_DAY","symbols":["FPT"],"to":<unix>,"countBack":N}`, cần header Referer và Origin `https://trading.vietcap.com.vn`. Trả `o,h,l,c,v,t` (VND).
- KBS: `GET https://kbbuddywts.kbsec.com.vn/iis-server/investment/stocks/FPT/data_day?sdate=01-10-2026&edate=06-11-2026`, trả `data_day: [{t,o,h,l,c,v,va}]`, mới nhất trước.
- DNSE: `GET https://services.entrade.com.vn/chart-api/v2/ohlcs/stock?from=&to=&symbol=FPT&resolution=1D`.
- VNDirect: `GET https://api-finfo.vndirect.com.vn/v4/stock_prices?sort=date&q=code:FPT&size=3&page=1`, có `adClose` (giá điều chỉnh), đơn vị nghìn đồng.

**BCTC:**
- Vietcap IQ, header Referer/Origin như trên:
  - `GET https://iq.vietcap.com.vn/api/iq-insight-service/v1/company/FPT/financial-statement?section=INCOME_STATEMENT|BALANCE_SHEET|CASH_FLOW` trả `data.years` và `data.quarters`; mỗi kỳ có `yearReport`, `lengthReport` (1–4 là quý, 5 là năm), `publicDate` và các trường `isa1`, `bsa1`, `cfa1`...
  - `.../company/FPT/financial-statement/metrics` trả danh mục chỉ tiêu theo section: `field`, `titleVi`, `titleEn`, `level`, `parent`. **Danh mục khác nhau theo loại hình:** ngân hàng dùng `isb*`, công ty chứng khoán dùng `isa*` với nghĩa khác doanh nghiệp thường. Phải tra qua metrics, không hardcode nghĩa của mã trường theo một loại hình.
  - `.../company/FPT/statistics-financial` trả chỉ số TTM (`pe`, `pb`, `marketCap`...) để đối chiếu.
- File PDF CafeF: `GET https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx?Symbol=FPT&Type=1&Year=2026` trả `Data: [{Quarter,Year,Time,Name,Link}]`.

`vnstock` không còn trên PyPI, không dùng.
