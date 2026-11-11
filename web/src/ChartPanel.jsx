import { CandlestickSeries, ColorType, HistogramSeries, createChart } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { DIRECTION_MARK, barLabel, chartTime, direction, formatChange, formatPercent, formatPrice, formatVolume } from './lib.js'
import { FundamentalsTab, NewsTab } from './Research.jsx'
import { apiGet } from './useFeed.js'

const VIEWS = [['chart', 'Biểu đồ'], ['news', 'Tin tức'], ['fin', 'BCTC']]

const UP = '#22c7b8'
const DOWN = '#f0564a'
const INK = '#a5b1c0'
const GRID = '#1a222c'
const REFRESH_MS = { '1m': 30000, '1D': 600000 }

function toSeries(bars) {
  return bars.map((bar) => ({ ...bar, time: chartTime(bar.time) }))
}

function Readout({ bar }) {
  if (!bar) return <p className="readout muted">Di chuột lên biểu đồ để xem từng nến</p>
  return (
    <p className="readout">
      <span>{bar.label}</span>
      <span>M {formatPrice(bar.open)}</span>
      <span>C {formatPrice(bar.high)}</span>
      <span>T {formatPrice(bar.low)}</span>
      <span>Đ {formatPrice(bar.close)}</span>
      <span>KL {formatVolume(bar.volume)}</span>
    </p>
  )
}

function Book({ quote }) {
  const rows = [0, 1, 2]
  if (!quote.bids?.length && !quote.asks?.length) return <p className="note">Không có dữ liệu dư mua/bán cho mã này ở chế độ hiện tại.</p>
  return (
    <table className="mini-book" aria-label="Ba mức giá tốt nhất">
      <thead><tr><th scope="col">KL mua</th><th scope="col">Giá mua</th><th scope="col">Giá bán</th><th scope="col">KL bán</th></tr></thead>
      <tbody>
        {rows.map((i) => {
          const bid = quote.bids?.[i]
          const ask = quote.asks?.[i]
          return (
            <tr key={i}>
              <td className="num">{formatVolume(bid?.volume)}</td>
              <td className={`num tone-${bid ? direction(bid.price, quote) : 'none'}`}>{formatPrice(bid?.price)}</td>
              <td className={`num tone-${ask ? direction(ask.price, quote) : 'none'}`}>{formatPrice(ask?.price)}</td>
              <td className="num">{formatVolume(ask?.volume)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export function ChartPanel({ connection, symbol, name, exchange, quote, watched, onToggleWatch, onClose }) {
  const host = useRef(null)
  const parts = useRef(null)
  const [interval, setIntervalChoice] = useState('1m')
  const [view, setView] = useState('chart')
  const [state, setState] = useState({ loading: true, error: null, warnings: [], count: 0 })
  const [hovered, setHovered] = useState(null)

  useEffect(() => {
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: INK, fontFamily: 'inherit', fontSize: 11,
        panes: { separatorColor: GRID } },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      rightPriceScale: { borderColor: GRID },
      timeScale: { borderColor: GRID, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      localization: { locale: 'vi-VN' },
    })
    // Rising candles are hollow and falling ones solid, so direction survives colour blindness.
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: 'rgba(0,0,0,0)', downColor: DOWN, borderUpColor: UP, borderDownColor: DOWN, wickUpColor: UP, wickDownColor: DOWN,
      priceFormat: { type: 'custom', formatter: formatPrice, minMove: 1 }, // thousands of VND, the same as the board
    })
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 1)
    chart.panes()[0]?.setStretchFactor(4)
    chart.panes()[1]?.setStretchFactor(1)
    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData?.get(candles)
      const vol = param.seriesData?.get(volume)
      if (!bar || param.time === undefined) return setHovered(null)
      setHovered({ ...bar, volume: vol?.value, label: barLabel(param.time) })
    })
    parts.current = { chart, candles, volume }
    return () => {
      parts.current = null
      chart.remove()
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let timer
    let first = true
    setState({ loading: true, error: null, warnings: [], count: 0 })
    parts.current?.chart.applyOptions({ timeScale: { timeVisible: interval === '1m' } })

    async function load() {
      try {
        const query = new URLSearchParams({ symbol, interval })
        const data = await apiGet(connection, `/api/bars?${query}`, controller.signal)
        const current = parts.current
        if (!current) return
        const series = toSeries(data.bars)
        current.candles.setData(series.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })))
        current.volume.setData(series.map(({ time, volume, open, close }) => ({ time, value: volume, color: close >= open ? `${UP}99` : `${DOWN}99` })))
        if (first) current.chart.timeScale().fitContent()
        first = false
        setState({ loading: false, error: null, warnings: data.warnings || [], count: data.bars.length })
      } catch (error) {
        if (controller.signal.aborted) return
        setState((previous) => ({ ...previous, loading: false, error: error.message }))
      }
      timer = setTimeout(load, REFRESH_MS[interval])
    }

    load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [connection, symbol, interval])

  const tone = quote ? direction(quote.price, quote) : 'none'
  return (
    <section className="panel" aria-label={`Chi tiết ${symbol}`}>
      <header className="panel-head">
        <button type="button" className="star big" aria-pressed={watched} aria-label={`${watched ? 'Bỏ' : 'Thêm'} ${symbol} ${watched ? 'khỏi' : 'vào'} danh mục`}
          title={watched ? 'Bỏ khỏi danh mục' : 'Thêm vào danh mục'} onClick={() => onToggleWatch(symbol)}>{watched ? '★' : '☆'}</button>
        <h2>{symbol}</h2>
        {(name || exchange) && <p className="company">{[name, exchange].filter(Boolean).join(' · ')}</p>}
        {quote && (
          <p className={`headline tone-${tone}`}>
            <span aria-hidden="true">{DIRECTION_MARK[tone]}</span> <strong>{formatPrice(quote.price)}</strong>{' '}
            {formatChange(quote.change)} ({formatPercent(quote.change_pct)})
          </p>
        )}
        <div className="segmented views" role="group" aria-label="Nội dung">
          {VIEWS.map(([value, label]) => (
            <button key={value} type="button" aria-pressed={view === value} onClick={() => setView(value)}>{label}</button>
          ))}
        </div>
        {view === 'chart' && (
          <div className="segmented" role="group" aria-label="Khung thời gian">
            {[['1m', '1 phút'], ['1D', '1 ngày']].map(([value, label]) => (
              <button key={value} type="button" aria-pressed={interval === value} onClick={() => setIntervalChoice(value)}>{label}</button>
            ))}
          </div>
        )}
        <button type="button" className="close" onClick={onClose} aria-label="Đóng">×</button>
      </header>
      {/* The chart stays mounted while hidden, so switching back does not rebuild it. */}
      <div hidden={view !== 'chart'}>
        <Readout bar={hovered} />
        <div className="panel-body">
          <div className="chart-box">
            <div className="chart-host" ref={host} />
            {state.loading && <p className="chart-note">Đang tải nến…</p>}
            {!state.loading && !state.error && state.count === 0 && <p className="chart-note">Chưa có nến cho khung này</p>}
            {state.error && <p className="chart-note error">Không tải được nến: {state.error}</p>}
          </div>
          {quote && <Book quote={quote} />}
        </div>
        {state.warnings.map((warning) => <p key={warning} className="note">{warning}</p>)}
      </div>
      {view === 'news' && <NewsTab connection={connection} symbol={symbol} />}
      {view === 'fin' && <FundamentalsTab connection={connection} symbol={symbol} />}
    </section>
  )
}
