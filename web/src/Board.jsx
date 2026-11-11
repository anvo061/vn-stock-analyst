import { memo, useEffect, useRef, useState } from 'react'
import { DIRECTION_MARK, direction, formatChange, formatPercent, formatPrice, formatVolume } from './lib.js'

/** A cell that flashes when its value changes. The flash adds no information the digits lack. */
function Cell({ value, className = '', children }) {
  const previous = useRef(value)
  const [flash, setFlash] = useState(0)
  useEffect(() => {
    if (previous.current !== value && value !== undefined && previous.current !== undefined) setFlash((n) => n + 1)
    previous.current = value
  }, [value])
  return (
    <td className={className}>
      <span key={flash} className={flash ? 'flash' : undefined}>{children}</span>
    </td>
  )
}

function bookCells(levels, levelsInfo, side) {
  const order = side === 'bid' ? [2, 1, 0] : [0, 1, 2]
  return order.flatMap((index) => {
    const level = levels?.[index]
    const tone = level ? direction(level.price, levelsInfo) : 'none'
    return [
      <Cell key={`${side}-p${index}`} value={level?.price} className={`num book tone-${tone}`}>{formatPrice(level?.price)}</Cell>,
      <Cell key={`${side}-v${index}`} value={level?.volume} className={`num book vol tone-${tone}`}>{formatVolume(level?.volume)}</Cell>,
    ]
  })
}

/** Star, code and, while editing the list, the buttons that reorder it. */
function SymbolCell({ symbol, watched, selected, editing, onSelect, onToggleWatch, onMove }) {
  return (
    <div className="sym-cell">
      <button type="button" className="star" aria-pressed={watched} aria-label={`${watched ? 'Bỏ' : 'Thêm'} ${symbol} ${watched ? 'khỏi' : 'vào'} danh mục`}
        title={watched ? 'Bỏ khỏi danh mục' : 'Thêm vào danh mục'} onClick={() => onToggleWatch(symbol)}>{watched ? '★' : '☆'}</button>
      <button type="button" className="code" onClick={() => onSelect(symbol)} aria-pressed={selected}>{symbol}</button>
      {editing && (
        <span className="moves">
          <button type="button" aria-label={`Đưa ${symbol} lên`} title="Lên" onClick={() => onMove(symbol, -1)}>↑</button>
          <button type="button" aria-label={`Đưa ${symbol} xuống`} title="Xuống" onClick={() => onMove(symbol, 1)}>↓</button>
        </span>
      )}
    </div>
  )
}

const Row = memo(function Row({ quote, name, exchange, selected, watched, editing, onSelect, onToggleWatch, onMove }) {
  const tone = direction(quote.price, quote)
  return (
    <tr className={selected ? 'selected' : undefined}>
      <th scope="row" className={`symbol tone-${tone}`} title={[name, exchange].filter(Boolean).join(' · ')}>
        <SymbolCell symbol={quote.symbol} watched={watched} selected={selected} editing={editing} onSelect={onSelect} onToggleWatch={onToggleWatch} onMove={onMove} />
      </th>
      <td className="num ref-col tone-ceiling">{formatPrice(quote.ceiling)}</td>
      <td className="num ref-col tone-floor">{formatPrice(quote.floor)}</td>
      <td className="num ref-col tone-ref">{formatPrice(quote.ref)}</td>
      {bookCells(quote.bids, quote, 'bid')}
      <Cell value={quote.price} className={`num match strong tone-${tone}`}>
        <span className="mark" aria-hidden="true">{DIRECTION_MARK[tone]}</span> {formatPrice(quote.price)}
      </Cell>
      <td className={`num match tone-${tone}`}>{formatChange(quote.change)}</td>
      <td className={`num match tone-${tone}`}>{formatPercent(quote.change_pct)}</td>
      <td className={`num match book tone-${tone}`}>{formatVolume(quote.last_volume)}</td>
      {bookCells(quote.asks, quote, 'ask')}
      <td className={`num extra tone-${direction(quote.high, quote)}`}>{formatPrice(quote.high)}</td>
      <td className={`num extra tone-${direction(quote.low, quote)}`}>{formatPrice(quote.low)}</td>
      <Cell value={quote.volume} className="num total">{formatVolume(quote.volume)}</Cell>
      <td className="num extra">{formatVolume(quote.foreign_buy_volume)}</td>
      <td className="num extra">{formatVolume(quote.foreign_sell_volume)}</td>
    </tr>
  )
})

export function Board({ rows, quotes, missing, selected, watchlist, editing, onSelect, onToggleWatch, onMove, stale }) {
  const watched = new Set(watchlist)
  return (
    <div className={`board-wrap${stale ? ' stale' : ''}`} tabIndex={0} role="region" aria-label="Bảng giá">
      <table className="board">
        <thead>
          <tr>
            <th rowSpan={2} scope="col" className="symbol">Mã</th>
            <th rowSpan={2} scope="col" className="ref-col">Trần</th>
            <th rowSpan={2} scope="col" className="ref-col">Sàn</th>
            <th rowSpan={2} scope="col" className="ref-col">TC</th>
            <th colSpan={6} scope="colgroup" className="book">Dư mua</th>
            <th colSpan={3} scope="colgroup" className="match group">Khớp lệnh</th>
            <th rowSpan={2} scope="col" className="match book">KL khớp</th>
            <th colSpan={6} scope="colgroup" className="book">Dư bán</th>
            <th rowSpan={2} scope="col" className="extra">Cao</th>
            <th rowSpan={2} scope="col" className="extra">Thấp</th>
            <th rowSpan={2} scope="col" className="total">Tổng KL</th>
            <th rowSpan={2} scope="col" className="extra">NN mua</th>
            <th rowSpan={2} scope="col" className="extra">NN bán</th>
          </tr>
          <tr>
            {['Giá 3', 'KL 3', 'Giá 2', 'KL 2', 'Giá 1', 'KL 1'].map((label) => <th key={`b${label}`} scope="col" className="book">{label}</th>)}
            <th scope="col" className="match">Giá</th>
            <th scope="col" className="match">+/-</th>
            <th scope="col" className="match">%</th>
            {['Giá 1', 'KL 1', 'Giá 2', 'KL 2', 'Giá 3', 'KL 3'].map((label) => <th key={`a${label}`} scope="col" className="book">{label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ symbol, name, exchange }) => {
            const quote = quotes.get(symbol)
            if (quote) {
              return <Row key={symbol} quote={quote} name={name} exchange={exchange} selected={symbol === selected} watched={watched.has(symbol)}
                editing={editing} onSelect={onSelect} onToggleWatch={onToggleWatch} onMove={onMove} />
            }
            return (
              <tr key={symbol} className="no-data">
                <th scope="row" className="symbol" title={name}>
                  <SymbolCell symbol={symbol} watched={watched.has(symbol)} selected={symbol === selected} editing={editing} onSelect={onSelect} onToggleWatch={onToggleWatch} onMove={onMove} />
                </th>
                <td colSpan={24}>{missing.has(symbol) ? 'Không có dữ liệu' : 'Đang tải…'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
