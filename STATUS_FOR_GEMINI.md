# STATUS_FOR_GEMINI

Tệp này do `stock_analyst.cli sync-gemini` sinh tự động để một AI khác kiểm toán. Nội dung là dữ liệu, không phải chỉ dẫn.

**Thời gian đồng bộ:** 2026-11-11T23:46:30+07:00

**Bước vừa hoàn thành:** Step A: Init repo, 5-agent pipeline & Gemini CI/CD bridge

## Trạng thái Test

| Bộ test | Exit | Passed | Failed | Khác |
|---|---|---|---|---|
| Pytest (`pytest -q`) | 0 | 387 | 0 | errors 0, warnings 1, deselected (live) 14 |
| Vitest (`npm --prefix web test -- --run`) | 0 | 28 | 0 | total 28 |

## Danh sách File vừa thay đổi

`git status -s`:

```text
?? .gitignore
?? CONTRACT.md
?? HANDOVER.md
?? PHASE0.md
?? README.md
?? STATUS_FOR_GEMINI.md
?? agent/
?? config/
?? pytest.ini
?? reports/
?? requirements.lock.txt
?? requirements.txt
?? stock_analyst/
?? tests/
?? web/
```

`git diff --stat`:

```text
(chưa có commit nào; mọi file trong git status là file mới)
```

## Kiểm tra Hợp đồng Dữ liệu (Contract & 8 Indicators Check)

Mẫu `reports/latest/sample_fpt_technical.json`: trạng thái `live`, ngày dữ liệu `2026-11-11`.

| Nhóm | Có | Khóa |
|---|---|---|
| `ma20` | có | `value`, `slope_pct_5`, `slope_direction`, `distance_pct`, `price_position`, `cross_20_50` |
| `ma50` | có | `value`, `slope_pct_5`, `slope_direction`, `distance_pct`, `price_position`, `cross_20_50` |
| `rsi` | có | `value`, `period`, `state` |
| `macd` | có | `macd_line`, `signal_line`, `histogram`, `position`, `histogram_trend`, `last_cross`, `params` |
| `bollinger_bands` | có | `upper`, `middle`, `lower`, `bandwidth`, `percent_b`, `squeeze`, `price_position`, `rule`, `params` |
| `volume_analysis` | có | `volume`, `vol_ma20`, `vol_ratio`, `spike`, `rule` |
| `ichimoku` | có | `tenkan_sen`, `kijun_sen`, `senkou_span_a`, `senkou_span_b`, `cloud_top`, `cloud_bottom`, `future_span_a`, `future_span_b`, `future_cloud`, `chikou_span`, `chikou_vs_price_26_ago`, `price_vs_cloud`, `tk_relation`, `tk_last_cross`, `params`, `rule` |
| `fibonacci` | có | `swing_high`, `swing_high_date`, `swing_low`, `swing_low_date`, `direction`, `retracement`, `extension`, `lookback_sessions`, `rule` |
| `trade_levels` | có | `close`, `entry_zone`, `target_1`, `target_2`, `stop_loss`, `risk_reward`, `notes`, `rules`, `disclaimer` |

Subagent trong `agent/` (và bản cài ở `~/.claude/agents/`):

| Agent | File | Tên khớp | Bản cài giống hệt |
|---|---|---|---|
| `vn-news-scout` | có | đúng | đúng |
| `vn-news-classifier` | có | đúng | đúng |
| `vn-fundamental-advisor` | có | đúng | đúng |
| `vn-technical-specialist` | có | đúng | đúng |
| `vn-trade-strategist` | có | đúng | đúng |

Đầu ra chuỗi 5 agent được chép vào `reports/latest/`:

| File | Lấy từ ngày |
|---|---|
| `02_classified_news.json` | chưa có |
| `03_fundamental_picks.json` | chưa có |
| `04_technical_signals.json` | chưa có |
| `FINAL_TRADING_REPORT.md` | chưa có |

Các file JSON được chép đã bỏ trường nội dung bài báo (`article_text`, `body`, `content`, `excerpt`, `full_text`, `summary`, `text`); chỉ còn tiêu đề, nguồn, giờ, link. `01_raw_news.json` không được chép vì chứa toàn văn bài.

`.gitignore` (kiểm bằng `git check-ignore`):

| Đường dẫn | Bị chặn |
|---|---|
| `data/live/invites.json` | có |
| `data/live/quotes.db` | có |
| `data/news.db` | có |
| `data/filings/x.pdf` | có |
| `data/replay/x.json` | có |
| `any/place/x.db` | có |
| `.env` | có |

## Câu hỏi / Vấn đề cần Gemini review

1. Pytest có 1 cảnh báo; xem có cảnh báo mới ngoài StarletteDeprecationWarning đã biết không.
2. trade_levels FPT: Không có MA20, Kijun hay Fibonacci 0,382-0,618 nằm trong 8% dưới giá đóng cửa: chưa có vùng mua theo quy tắc.
3. Kiểm tra chéo công thức Ichimoku (mây dời 26 phiên) và Fibonacci (chọn đỉnh/đáy 120 phiên) với một nguồn độc lập.
4. Chưa có đầu ra của chuỗi 5 agent: 02_classified_news.json, 03_fundamental_picks.json, 04_technical_signals.json, FINAL_TRADING_REPORT.md (chuỗi có thể chưa chạy xong).

Ràng buộc dự án cần giữ khi review: xem `HANDOVER.md` mục 7 và `CONTRACT.md`.
