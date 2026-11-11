// Pure helpers: connection details, number formats, price direction.

const STORAGE_KEY = 'bang-dien-connection'

export const PAGE_SIZE = 50
export const TABS = [['watch', 'Danh mục'], ['HOSE', 'HOSE'], ['HNX', 'HNX'], ['UPCOM', 'UPCoM']]

const WATCHLIST_KEY = 'bang-dien-watchlist'
const SYMBOL_PATTERN = /^[A-Z0-9]{3}$/

/** The viewer's own list, or null until they first change it (the server's list applies until then). */
export function loadWatchlist(storage = globalThis.localStorage) {
  try {
    const saved = JSON.parse(storage.getItem(WATCHLIST_KEY))
    return Array.isArray(saved) ? [...new Set(saved.filter((s) => typeof s === 'string' && SYMBOL_PATTERN.test(s)))] : null
  } catch {
    return null
  }
}

export function saveWatchlist(list, storage = globalThis.localStorage) {
  try {
    if (list === null) storage.removeItem(WATCHLIST_KEY)
    else storage.setItem(WATCHLIST_KEY, JSON.stringify(list))
  } catch {
    // Private mode: the changes last until the tab closes.
  }
}

export const WATCHLIST_STORAGE_KEY = WATCHLIST_KEY

export function toggleSymbol(list, symbol) {
  return list.includes(symbol) ? list.filter((s) => s !== symbol) : [...list, symbol]
}

export function moveSymbol(list, symbol, delta) {
  const from = list.indexOf(symbol)
  const to = from + delta
  if (from < 0 || to < 0 || to >= list.length) return list
  const next = [...list]
  next.splice(from, 1)
  next.splice(to, 0, symbol)
  return next
}

/** Lowercase without Vietnamese marks, so "hoa phat" finds "Hòa Phát". */
export function plainText(text) {
  return String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase().trim()
}

/** A search covers the whole market whichever tab is open; exact code matches come first. */
export function filterSymbols(universe, { tab, term, watchlist }) {
  const query = plainText(term)
  if (query) {
    const matches = universe.filter((entry) => entry.symbol.toLowerCase().includes(query) || plainText(entry.name).includes(query))
    const rank = (entry) => (entry.symbol.toLowerCase() === query ? 0 : entry.symbol.toLowerCase().startsWith(query) ? 1 : 2)
    return matches.sort((a, b) => rank(a) - rank(b) || a.symbol.localeCompare(b.symbol))
  }
  if (tab === 'watch') {
    const known = new Map(universe.map((entry) => [entry.symbol, entry]))
    return (watchlist || []).map((symbol) => known.get(symbol) || { symbol, exchange: null, name: '' })
  }
  return universe.filter((entry) => entry.exchange === tab)
}

export function pageSlice(rows, page) {
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const current = Math.min(Math.max(page, 0), pages - 1)
  return { rows: rows.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE), page: current, pages }
}

/** Read `#api=...&token=...` from an invite link. Only http(s) API addresses are accepted. */
export function parseInviteHash(hash) {
  const params = new URLSearchParams(String(hash || '').replace(/^#/, ''))
  const token = params.get('token')
  if (!token) return null
  return { api: normalizeApi(params.get('api') || ''), token }
}

export function normalizeApi(value) {
  const text = String(value || '').trim().replace(/\/+$/, '')
  if (!text) return ''
  let url
  try {
    url = new URL(text)
  } catch {
    throw new Error('Địa chỉ máy chủ không hợp lệ')
  }
  if (url.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(url.hostname)) {
    throw new Error('Địa chỉ máy chủ phải dùng https')
  }
  return url.origin
}

export function loadConnection(storage = globalThis.localStorage) {
  try {
    const saved = JSON.parse(storage.getItem(STORAGE_KEY))
    return saved?.token ? saved : null
  } catch {
    return null
  }
}

export function saveConnection(connection, storage = globalThis.localStorage) {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(connection))
  } catch {
    // Private mode: the connection simply lasts until the tab closes.
  }
}

export function clearConnection(storage = globalThis.localStorage) {
  try {
    storage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored, nothing to clear.
  }
}

const price2 = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const whole = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 0 })
const signed = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' })

const missing = (value) => value === null || value === undefined || Number.isNaN(value)

/** Prices arrive in VND and are shown in thousands, as Vietnamese boards do. */
export const formatPrice = (vnd) => (missing(vnd) ? '–' : price2.format(vnd / 1000))
export const formatVolume = (value) => (missing(value) ? '–' : whole.format(value))
export const formatChange = (vnd) => (missing(vnd) ? '–' : signed.format(vnd / 1000))
export const formatPercent = (value) => (missing(value) ? '–' : `${signed.format(value)}%`)
export const formatTime = (date) => (date ? date.toLocaleTimeString('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', hour12: false }) : '–')

/** Board colour class. Ceiling and floor outrank up and down. */
export function direction(price, { ref, ceiling, floor } = {}) {
  if (missing(price) || missing(ref)) return 'none'
  if (ceiling && price >= ceiling) return 'ceiling'
  if (floor && price <= floor) return 'floor'
  if (price > ref) return 'up'
  if (price < ref) return 'down'
  return 'ref'
}

export const DIRECTION_MARK = { ceiling: '▲', up: '▲', ref: '■', down: '▼', floor: '▼', none: '' }

export const MARKET_LABEL = {
  closed: 'Đóng cửa', holiday: 'Nghỉ lễ', pre_open: 'Trước giờ mở cửa', ato: 'Khớp lệnh mở cửa', morning: 'Phiên sáng',
  lunch: 'Nghỉ trưa', afternoon: 'Phiên chiều', atc: 'Khớp lệnh đóng cửa', post: 'Sau đóng cửa',
}

// ---- News and financial statements ------------------------------------------------------

export const SOURCE_LABEL = {
  cafef_ck: 'CafeF', cafef_dn: 'CafeF', vietstock_cp: 'Vietstock', vietstock_dn: 'Vietstock', vnexpress_kd: 'VnExpress',
  tnck: 'Tin nhanh CK', vneconomy_ck: 'VnEconomy', tuoitre_kd: 'Tuổi Trẻ', thanhnien_kd: 'Thanh Niên', fireant: 'FireAnt',
}

/** A link is only ever rendered when it is plain http(s); anything else shows as text. */
export function safeHttpUrl(value) {
  try {
    const url = new URL(String(value))
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}

const dateParts = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Ho_Chi_Minh', hour12: false, hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit',
})

/** "09:30 11/11" in Vietnam time, assembled from parts because locale separators differ between browsers. */
export function formatDateTime(iso) {
  const date = iso ? new Date(iso) : null
  if (!date || Number.isNaN(date.getTime())) return '–'
  const part = Object.fromEntries(dateParts.formatToParts(date).map(({ type, value }) => [type, value]))
  return `${part.hour}:${part.minute} ${part.day}/${part.month}`
}

const oneDecimal = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
const twoDecimals = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const signedOne = new Intl.NumberFormat('vi-VN', { minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'exceptZero' })

/** Statement amounts arrive in tỷ đồng and ratios as decimals (0.12 means 12%), per the API's `units`. */
export function formatMetric(value, kind) {
  if (missing(value)) return '–'
  if (typeof value !== 'number') return String(value)
  switch (kind) {
    case 'bn': return `${oneDecimal.format(value)} tỷ`
    case 'pct': return `${oneDecimal.format(value * 100)}%`
    case 'growth': return `${signedOne.format(value * 100)}%`
    case 'times': return `${twoDecimals.format(value)} lần`
    case 'vnd': return `${formatPrice(value)} nghìn đ`
    default: return twoDecimals.format(value)
  }
}

export const METRIC_GROUPS = [
  ['growth', 'Tăng trưởng', [
    ['revenue', 'Doanh thu quý', 'bn'], ['revenue_yoy', 'so với cùng kỳ', 'growth'], ['revenue_ttm', 'Doanh thu 4 quý', 'bn'],
    ['revenue_ttm_yoy', 'so với cùng kỳ', 'growth'], ['parent_profit', 'LN công ty mẹ quý', 'bn'], ['parent_profit_yoy', 'so với cùng kỳ', 'growth'],
    ['parent_profit_ttm', 'LN công ty mẹ 4 quý', 'bn'], ['parent_profit_ttm_yoy', 'so với cùng kỳ', 'growth']]],
  ['profitability', 'Sinh lời', [
    ['gross_margin_ttm', 'Biên gộp 4 quý', 'pct'], ['operating_margin_ttm', 'Biên hoạt động 4 quý', 'pct'], ['net_margin_ttm', 'Biên ròng 4 quý', 'pct'],
    ['gross_margin_q', 'Biên gộp quý', 'pct'], ['net_margin_q', 'Biên ròng quý', 'pct'], ['roe', 'ROE', 'pct'], ['roa', 'ROA', 'pct'],
    ['nim', 'NIM', 'pct'], ['cir', 'CIR 4 quý', 'pct'], ['cir_q', 'CIR quý', 'pct'], ['ldr', 'LDR', 'pct'], ['npl_ratio', 'Nợ xấu', 'pct'],
    ['coverage', 'Bao phủ nợ xấu', 'pct'], ['casa', 'CASA', 'pct'], ['associates_profit_ttm', 'Lãi liên doanh, liên kết 4 quý', 'bn'],
    ['associates_share_of_pbt_ttm', 'Tỷ trọng lãi liên kết trong LNTT', 'pct']]],
  ['liquidity_leverage', 'Thanh khoản và đòn bẩy', [
    ['current_ratio', 'Thanh toán hiện hành', 'times'], ['quick_ratio', 'Thanh toán nhanh', 'times'], ['debt', 'Nợ vay', 'bn'],
    ['debt_to_equity', 'Nợ vay/Vốn chủ', 'times'], ['net_debt', 'Nợ vay ròng', 'bn'], ['ebitda_ttm', 'EBITDA 4 quý', 'bn'],
    ['net_debt_to_ebitda', 'Nợ ròng/EBITDA', 'times'], ['interest_cover', 'Khả năng trả lãi', 'times'], ['equity_to_assets', 'Vốn chủ/Tổng tài sản', 'pct'],
    ['total_assets', 'Tổng tài sản', 'bn'], ['equity', 'Vốn chủ sở hữu', 'bn'], ['margin_loans_to_equity', 'Cho vay ký quỹ/Vốn chủ', 'times']]],
  ['cash_quality', 'Dòng tiền', [
    ['cfo_ttm', 'Dòng tiền kinh doanh 4 quý', 'bn'], ['net_profit_ttm', 'LNST 4 quý', 'bn'], ['cfo_to_net_profit', 'Dòng tiền/LNST', 'times'],
    ['capex_ttm', 'Chi đầu tư TSCĐ 4 quý', 'bn'], ['fcf_ttm', 'Dòng tiền tự do 4 quý', 'bn']]],
  ['valuation', 'Định giá', [
    ['market_cap', 'Vốn hóa', 'bn'], ['pe', 'P/E (nguồn)', 'times'], ['pb', 'P/B (nguồn)', 'times'], ['own_pe', 'P/E tự tính', 'times'],
    ['own_pb', 'P/B tự tính', 'times'], ['implied_price_vnd', 'Giá dùng để tính', 'vnd']]],
]

/** Known fields in a fixed order, then any other numeric field the API adds, so nothing is silently hidden. */
export function metricRows(section, fields) {
  if (!section || typeof section !== 'object') return []
  const known = new Set(fields.map(([key]) => key))
  const rows = fields.filter(([key]) => key in section).map(([key, label, kind]) => ({ key, label, value: formatMetric(section[key], kind) }))
  Object.entries(section)
    .filter(([key, value]) => !known.has(key) && typeof value === 'number')
    .forEach(([key, value]) => rows.push({ key, label: key, value: formatMetric(value) }))
  return rows
}

export const SEVERITY_LABEL = { high: 'Cao', medium: 'Trung bình', low: 'Thấp' }

/** Each failed check, in words a reader can verify against the statements. */
export function integrityFailures(integrity) {
  if (!integrity) return []
  const failures = []
  for (const row of integrity.balance_identity || []) {
    if (row.ok === false) failures.push(`${row.period}: tài sản lệch nợ phải trả + vốn chủ ${oneDecimal.format(row.diff)} tỷ`)
  }
  for (const row of integrity.annual_reconciliation || []) {
    if (row.ok === false) failures.push(`Năm ${row.year}, ${row.line}: cộng 4 quý lệch số cả năm ${oneDecimal.format(row.diff)} tỷ`)
  }
  if (integrity.profit_attribution_ok === false) failures.push('LNST không khớp phần công ty mẹ + cổ đông thiểu số')
  return failures
}

/** Readable label for a chart time: an already shifted epoch, a date string, or a business-day object. */
export function barLabel(time) {
  if (typeof time === 'number') return new Date(time * 1000).toISOString().slice(11, 16)
  if (typeof time === 'string') return time.split('-').reverse().join('/')
  return time ? `${time.day}/${time.month}/${time.year}` : ''
}

/** Chart time is UTC-based, so intraday epochs shift by +7h to display Vietnamese session times. */
export const VN_OFFSET_S = 7 * 3600
export const chartTime = (time) => (typeof time === 'number' ? time + VN_OFFSET_S : time)
