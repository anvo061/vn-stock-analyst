import { useEffect, useState } from 'react'
import { METRIC_GROUPS, SEVERITY_LABEL, SOURCE_LABEL, barLabel, formatDateTime, integrityFailures, metricRows, safeHttpUrl } from './lib.js'
import { apiGet } from './useFeed.js'

// Everything here is text from outside sources: React escapes it, and nothing is ever set as HTML.

function useApi(connection, path) {
  const [state, setState] = useState({ loading: true, error: null, data: null })
  useEffect(() => {
    const controller = new AbortController()
    setState({ loading: true, error: null, data: null })
    apiGet(connection, path, controller.signal)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((error) => { if (!controller.signal.aborted) setState({ loading: false, error: error.message, data: null }) })
    return () => controller.abort()
  }, [connection, path])
  return state
}

export function NewsTab({ connection, symbol }) {
  const { loading, error, data } = useApi(connection, `/api/news?${new URLSearchParams({ symbol, hours: '168', limit: '40' })}`)
  if (loading) return <p className="tab-note">Đang tải tin…</p>
  if (error) return <p className="tab-note error">Không tải được tin: {error}</p>
  return (
    <div className="research">
      <p className="tab-note">Tin 7 ngày gần nhất được gắn mã {symbol}. Tin cập nhật lần cuối: {formatDateTime(data.last_crawl)}. {data.note}</p>
      {data.items.length === 0 && <p className="empty">Chưa có tin nào gắn mã {symbol} trong dữ liệu đã cào.</p>}
      <ul className="news-list">
        {data.items.map((item) => {
          const href = safeHttpUrl(item.url)
          return (
            <li key={item.url}>
              {href
                ? <a href={href} target="_blank" rel="noopener noreferrer nofollow">{item.title}</a>
                : <span>{item.title}</span>}
              <span className="news-meta">{SOURCE_LABEL[item.source] || item.source} · {formatDateTime(item.published_at)}
                {item.tickers.length > 1 && ` · cùng gắn ${item.tickers.filter((t) => t !== symbol).join(', ')}`}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function FundamentalsTab({ connection, symbol }) {
  const { loading, error, data } = useApi(connection, `/api/fundamentals?${new URLSearchParams({ symbol })}`)
  if (loading) return <p className="tab-note">Đang tải BCTC… lần đầu có thể mất vài giây.</p>
  if (error) return <p className="tab-note error">Không tải được BCTC: {error}</p>
  const failures = integrityFailures(data.integrity)
  const revenueLabel = data.growth?.revenue_label
  return (
    <div className="research">
      <p className="tab-note">
        Kỳ gần nhất {data.latest_period || '–'}, công bố {data.public_date ? barLabel(data.public_date) : '–'} · số tiền tính bằng {data.units?.amounts || 'tỷ đồng'}.
        Số lấy từ API Vietcap, chưa đối chiếu với file BCTC gốc.
      </p>
      {data.supported === false && <p className="banner warn">Loại hình doanh nghiệp này chưa được hỗ trợ tính chỉ số.</p>}

      <section aria-label="Cờ cảnh báo">
        <h3>Cờ cảnh báo</h3>
        {(data.red_flags || []).length === 0
          ? <p className="tab-note">Không có cờ nào theo các quy tắc đang dùng. Không có cờ không có nghĩa là không có vấn đề.</p>
          : (
            <ul className="flags">
              {data.red_flags.map((flag) => (
                <li key={`${flag.rule}-${JSON.stringify(flag.values)}`} className={`flag sev-${flag.severity}`}>
                  <strong>{SEVERITY_LABEL[flag.severity] || flag.severity}</strong> · {flag.description}
                  <span className="flag-rule">{flag.rule}</span>
                </li>
              ))}
            </ul>
          )}
      </section>

      <section aria-label="Kiểm tra số liệu">
        <h3>Kiểm tra số liệu</h3>
        {data.integrity?.passed !== false && failures.length === 0
          ? <p className="tab-note">Đạt: bảng cân đối khớp, cộng 4 quý khớp số cả năm.</p>
          : <ul className="flags">{failures.map((text) => <li key={text} className="flag sev-medium">{text}</li>)}</ul>}
      </section>

      {(data.warnings || []).length > 0 && (
        <section aria-label="Lưu ý dữ liệu">
          <h3>Lưu ý dữ liệu</h3>
          <ul className="warnings">{data.warnings.map((text) => <li key={text}>{text}</li>)}</ul>
        </section>
      )}

      <div className="metric-grid">
        {METRIC_GROUPS.map(([key, title, fields]) => {
          const rows = metricRows(data[key], fields)
          if (rows.length === 0) return null
          const note = data[key]?.note
          return (
            <section key={key} className="metric-group" aria-label={title}>
              <h3>{title}</h3>
              <dl>
                {rows.map((row) => (
                  <div key={row.key} className="metric">
                    <dt>{row.key === 'revenue' && revenueLabel ? `${revenueLabel} quý` : row.label}</dt>
                    <dd>{row.value}</dd>
                  </div>
                ))}
              </dl>
              {note && <p className="tab-note">{note}</p>}
            </section>
          )
        })}
      </div>
      <p className="tab-note">Dữ liệu hỗ trợ theo dõi, không phải khuyến nghị đầu tư.</p>
    </div>
  )
}
