import { useCallback, useEffect, useMemo, useState } from 'react'
import { Board } from './Board.jsx'
import { ChartPanel } from './ChartPanel.jsx'
import { MARKET_LABEL, TABS, WATCHLIST_STORAGE_KEY, clearConnection, filterSymbols, formatTime, loadConnection, loadWatchlist, moveSymbol, normalizeApi, pageSlice, parseInviteHash, saveConnection, saveWatchlist, toggleSymbol } from './lib.js'
import { apiGet, useFeed } from './useFeed.js'

const SYMBOLS_RETRY_MS = 5000

/** An invite link's token is taken from the hash, stored, and removed from the address bar at once. */
function consumeInviteHash() {
  let invite = null
  let error = null
  try {
    invite = parseInviteHash(window.location.hash)
  } catch (problem) {
    error = problem.message
  }
  if (window.location.hash) history.replaceState(null, '', window.location.pathname + window.location.search)
  if (invite) saveConnection(invite)
  return { invite, error }
}

function initialConnection() {
  const { invite, error } = consumeInviteHash()
  return { connection: invite || loadConnection(), error }
}

function InviteForm({ reason, onSubmit }) {
  const [api, setApi] = useState('')
  const [token, setToken] = useState('')
  const [error, setError] = useState(reason)
  const submit = (event) => {
    event.preventDefault()
    try {
      onSubmit({ api: normalizeApi(api), token: token.trim() })
    } catch (problem) {
      setError(problem.message)
    }
  }
  return (
    <main className="invite">
      <h1>Bảng điện</h1>
      <p>Mở bằng link mời, hoặc nhập địa chỉ máy chủ và mã mời bạn được gửi.</p>
      {error && <p className="invite-error" role="alert">{error}</p>}
      <form onSubmit={submit}>
        <label>Địa chỉ máy chủ<input value={api} onChange={(e) => setApi(e.target.value)} placeholder="https://…trycloudflare.com" autoComplete="off" spellCheck="false" /></label>
        <label>Mã mời<input type="password" value={token} onChange={(e) => setToken(e.target.value)} required autoComplete="off" /></label>
        <button type="submit">Vào bảng điện</button>
      </form>
    </main>
  )
}

function StatusBanner({ feed, link }) {
  if (link.state === 'lost') {
    return (
      <p className="banner danger" role="alert">
        <strong>Mất kết nối tới máy chủ.</strong> Máy chủ có thể đang tắt. Giá bên dưới là giá cũ, cập nhật lần cuối lúc {formatTime(link.lastOk)}.
      </p>
    )
  }
  if (feed?.status === 'offline') {
    return <p className="banner danger" role="alert"><strong>Nguồn giá đang lỗi.</strong> Giá bên dưới có thể đã cũ. Máy chủ đang tự thử lại.</p>
  }
  if (feed?.mode === 'replay') {
    return <p className="banner replay" role="status"><strong>ĐANG PHÁT LẠI PHIÊN CŨ.</strong> Đây không phải giá đang chạy, và chế độ này chỉ có các mã trong danh mục.</p>
  }
  if (feed && !feed.on_primary) {
    return <p className="banner warn" role="status">Đang dùng nguồn dự phòng {feed.source}, nguồn chính {feed.primary} đang lỗi.</p>
  }
  return null
}

function StatusBar({ feed, link, onLogout }) {
  const healthy = link.state === 'ok' && feed?.status === 'ok'
  return (
    <header className="topbar">
      <h1>Bảng điện</h1>
      <p className={`pill ${healthy ? 'ok' : link.state === 'connecting' ? '' : 'bad'}`} role="status">
        <span aria-hidden="true">{healthy ? '●' : '○'}</span> {link.state === 'connecting' ? 'Đang kết nối' : healthy ? 'Đang cập nhật' : 'Dữ liệu cũ'}
      </p>
      {feed && <p className="meta">{feed.mode === 'replay' ? 'Phát lại' : MARKET_LABEL[feed.market] || feed.market}</p>}
      {feed && <p className="meta">Nguồn: {feed.source}</p>}
      <p className="meta">Cập nhật: {formatTime(link.lastOk)}</p>
      <button type="button" className="ghost" onClick={onLogout}>Thoát</button>
    </header>
  )
}

function Tabs({ tab, counts, searching, onChange }) {
  return (
    <div className="tabs" role="group" aria-label="Chọn sàn">
      {TABS.map(([value, label]) => (
        <button key={value} type="button" aria-pressed={!searching && tab === value} onClick={() => onChange(value)}>
          {label} <span className="count">{counts[value] ?? 0}</span>
        </button>
      ))}
    </div>
  )
}

function Pager({ page, pages, total, onChange }) {
  if (pages <= 1) return <p className="pager-info">{total} mã</p>
  return (
    <nav className="pager" aria-label="Phân trang">
      <button type="button" disabled={page === 0} onClick={() => onChange(page - 1)}>‹ Trước</button>
      <span className="pager-info">Trang {page + 1}/{pages} · {total} mã</span>
      <button type="button" disabled={page >= pages - 1} onClick={() => onChange(page + 1)}>Sau ›</button>
    </nav>
  )
}

function useUniverse(connection, onUnauthorized) {
  const [universe, setUniverse] = useState({ symbols: null, watchlist: [], error: null })
  useEffect(() => {
    const controller = new AbortController()
    let timer
    async function load() {
      try {
        const data = await apiGet(connection, '/api/symbols', controller.signal)
        setUniverse({ symbols: data.symbols, watchlist: data.watchlist, error: null })
      } catch (error) {
        if (controller.signal.aborted) return
        if (error.status === 401) return onUnauthorized()
        const stale = error.status === 404 ? 'Máy chủ đang chạy bản cũ. Hãy tắt và bật lại máy chủ.' : error.message
        setUniverse((previous) => ({ ...previous, error: stale }))
        timer = setTimeout(load, SYMBOLS_RETRY_MS)
      }
    }
    load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [connection, onUnauthorized])
  return universe
}

function Dashboard({ connection, onLogout }) {
  const onUnauthorized = useCallback(() => onLogout('Mã mời không còn hiệu lực. Hãy xin link mời mới.'), [onLogout])
  const universe = useUniverse(connection, onUnauthorized)
  const [tab, setTab] = useState('watch')
  const [term, setTerm] = useState('')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState(null)

  // The viewer's own list lives in this browser; the server's list applies until they change it.
  const [ownList, setOwnList] = useState(() => loadWatchlist())
  const [editing, setEditing] = useState(false)
  const watchlist = ownList ?? universe.watchlist
  const changeList = useCallback((change) => {
    setOwnList((current) => {
      const next = change(current ?? universe.watchlist)
      saveWatchlist(next)
      return next
    })
  }, [universe.watchlist])
  const toggleWatch = useCallback((symbol) => changeList((list) => toggleSymbol(list, symbol)), [changeList])
  const moveWatch = useCallback((symbol, delta) => changeList((list) => moveSymbol(list, symbol, delta)), [changeList])
  const resetList = () => {
    if (window.confirm('Khôi phục danh mục mặc định? Danh mục hiện tại của bạn sẽ mất.')) {
      saveWatchlist(null)
      setOwnList(null)
      setPage(0)
    }
  }
  // Another open tab changed the list: follow it.
  useEffect(() => {
    const onStorage = (event) => { if (event.key === WATCHLIST_STORAGE_KEY) setOwnList(loadWatchlist()) }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const all = universe.symbols || []
  const matches = useMemo(() => filterSymbols(all, { tab, term, watchlist }), [all, tab, term, watchlist])
  const paged = pageSlice(matches, page)
  const counts = useMemo(() => {
    const result = { watch: watchlist.length }
    all.forEach((entry) => { result[entry.exchange] = (result[entry.exchange] || 0) + 1 })
    return result
  }, [all, watchlist])

  const visible = useMemo(() => {
    const symbols = paged.rows.map((entry) => entry.symbol)
    return selected && !symbols.includes(selected) ? [...symbols, selected] : symbols
  }, [paged.rows, selected])
  const { feed, quotes, missing, link } = useFeed(connection, visible, onUnauthorized)

  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const stale = link.state === 'lost' || feed?.status === 'offline'
  const selectedEntry = all.find((entry) => entry.symbol === selected)
  const toggle = useCallback((symbol) => setSelected((current) => (current === symbol ? null : symbol)), [])
  const searching = Boolean(term.trim())
  const onWatchTab = tab === 'watch' && !searching

  return (
    <div className="app">
      <StatusBar feed={feed} link={link} onLogout={() => onLogout()} />
      <StatusBanner feed={feed} link={link} />
      <div className="tools">
        <Tabs tab={tab} counts={counts} searching={searching} onChange={(next) => { setTab(next); setTerm(''); setPage(0) }} />
        <label className="filter"><span>Tìm</span>
          <input value={term} onChange={(e) => { setTerm(e.target.value); setPage(0) }} placeholder="Mã hoặc tên công ty" maxLength={40} spellCheck="false" />
        </label>
        <p className="legend" aria-label="Chú giải màu">
          <span className="tone-ceiling">▲ Trần</span><span className="tone-up">▲ Tăng</span><span className="tone-ref">■ TC</span>
          <span className="tone-down">▼ Giảm</span><span className="tone-floor">▼ Sàn</span>
          <span className="muted">Giá: nghìn đồng</span>
        </p>
        {onWatchTab && universe.symbols && (
          <div className="list-tools">
            <button type="button" aria-pressed={editing} onClick={() => setEditing((value) => !value)}>{editing ? 'Xong' : 'Sắp xếp'}</button>
            {editing && <button type="button" onClick={resetList}>Khôi phục mặc định</button>}
          </div>
        )}
      </div>
      {universe.error && !universe.symbols && <p className="banner danger" role="alert">Không lấy được danh sách mã: {universe.error}</p>}
      {!universe.symbols && !universe.error && <p className="empty">Đang lấy danh sách mã…</p>}
      {universe.symbols && (
        <>
          {!(onWatchTab && matches.length === 0) && (
            <Board rows={paged.rows} quotes={quotes} missing={missing} selected={selected} watchlist={watchlist}
              editing={editing && onWatchTab} onSelect={toggle} onToggleWatch={toggleWatch} onMove={moveWatch} stale={stale} />
          )}
          {matches.length === 0
            ? <p className="empty">{onWatchTab ? 'Danh mục đang trống. Tìm một mã hoặc mở tab sàn rồi bấm ☆ để thêm.' : `Không có mã nào khớp “${term}”.`}</p>
            : <Pager page={paged.page} pages={paged.pages} total={matches.length} onChange={setPage} />}
        </>
      )}
      {selected && (
        <ChartPanel connection={connection} symbol={selected} name={selectedEntry?.name} exchange={selectedEntry?.exchange}
          quote={quotes.get(selected)} watched={watchlist.includes(selected)} onToggleWatch={toggleWatch} onClose={() => setSelected(null)} />
      )}
      <footer className="foot">
        Công cụ hỗ trợ theo dõi, không phải khuyến nghị đầu tư. Dữ liệu lấy từ nguồn không chính thức, có thể chậm hoặc sai.
        Mã đang xem cập nhật vài giây một lần; mã khác được làm mới luân phiên.
        Biểu đồ dùng <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">TradingView Lightweight Charts</a>.
      </footer>
    </div>
  )
}

export default function App() {
  const [{ connection: initial, error: inviteError }] = useState(initialConnection)
  const [connection, setConnection] = useState(initial)
  const [reason, setReason] = useState(inviteError)

  const logout = useCallback((message = null) => {
    clearConnection()
    setConnection(null)
    setReason(message)
  }, [])

  // Opening a new invite link while the app is already open changes only the hash, with no reload.
  useEffect(() => {
    const onHashChange = () => {
      const { invite, error } = consumeInviteHash()
      if (invite) {
        setReason(null)
        setConnection(invite)
      } else if (error) {
        setReason(error)
      }
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  if (!connection) {
    return <InviteForm reason={reason} onSubmit={(next) => { saveConnection(next); setReason(null); setConnection(next) }} />
  }
  return <Dashboard connection={connection} onLogout={logout} />
}
