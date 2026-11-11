import { describe, expect, it } from 'vitest'
import { METRIC_GROUPS, formatDateTime, formatMetric, integrityFailures, metricRows, safeHttpUrl } from './lib.js'
import { PAGE_SIZE, barLabel, chartTime, direction, filterSymbols, formatChange, formatPercent, formatPrice, formatVolume, loadConnection, loadWatchlist, moveSymbol, normalizeApi, pageSlice, parseInviteHash, plainText, saveConnection, saveWatchlist, toggleSymbol } from './lib.js'

const memory = () => {
  const data = new Map()
  return { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: (k) => data.delete(k) }
}

describe('news and statements', () => {
  it('only lets plain web links become links', () => {
    expect(safeHttpUrl('https://cafef.vn/a.chn')).toBe('https://cafef.vn/a.chn')
    expect(safeHttpUrl('http://fireant.vn/bai-viet/a/1')).toBe('http://fireant.vn/bai-viet/a/1')
    for (const bad of ['javascript:alert(1)', 'data:text/html,<b>x</b>', 'vbscript:x', '//evil.test', 'not a url', null, undefined]) {
      expect(safeHttpUrl(bad)).toBeNull()
    }
  })

  it('formats amounts, ratios, growth and multiples in Vietnamese', () => {
    expect(formatMetric(13754.16, 'bn')).toBe('13.754,2 tỷ')
    expect(formatMetric(0.2652, 'pct')).toBe('26,5%')
    expect(formatMetric(-0.2005, 'growth')).toBe('-20,1%')
    expect(formatMetric(0.148, 'growth')).toBe('+14,8%')
    expect(formatMetric(1.6546, 'times')).toBe('1,65 lần')
    expect(formatMetric(52900, 'vnd')).toBe('52,90 nghìn đ')
    expect(formatMetric(null, 'pct')).toBe('–')
    expect(formatMetric('ghi chú', 'pct')).toBe('ghi chú')
  })

  it('lists known fields in order and still shows new numeric fields', () => {
    const rows = metricRows({ roe: 0.2, gross_margin_ttm: 0.33, brand_new: 1.5, note: 'text', year_ago: { roe: 1 }, nim: null },
      [['gross_margin_ttm', 'Biên gộp', 'pct'], ['roe', 'ROE', 'pct'], ['nim', 'NIM', 'pct'], ['casa', 'CASA', 'pct']])
    expect(rows).toEqual([
      { key: 'gross_margin_ttm', label: 'Biên gộp', value: '33,0%' },
      { key: 'roe', label: 'ROE', value: '20,0%' },
      { key: 'nim', label: 'NIM', value: '–' },
      { key: 'brand_new', label: 'brand_new', value: '1,50' },
    ])
    expect(metricRows(null, [])).toEqual([])
  })

  it('every metric group names distinct fields', () => {
    for (const [, , fields] of METRIC_GROUPS) {
      expect(new Set(fields.map(([key]) => key)).size).toBe(fields.length)
    }
  })

  it('spells out each failed integrity check', () => {
    expect(integrityFailures({ passed: true, balance_identity: [{ period: 'Q3/2026', diff: 0, ok: true }], annual_reconciliation: [], profit_attribution_ok: true })).toEqual([])
    expect(integrityFailures({
      passed: false,
      balance_identity: [{ period: 'Q1/2026', diff: 12.34, ok: false }],
      annual_reconciliation: [{ year: 2023, line: 'cfo', diff: -991.6, ok: false }, { year: 2024, line: 'cfo', diff: 0, ok: true }],
      profit_attribution_ok: false,
    })).toEqual([
      'Q1/2026: tài sản lệch nợ phải trả + vốn chủ 12,3 tỷ',
      'Năm 2023, cfo: cộng 4 quý lệch số cả năm -991,6 tỷ',
      'LNST không khớp phần công ty mẹ + cổ đông thiểu số',
    ])
    expect(integrityFailures(undefined)).toEqual([])
  })

  it('formats news times in Vietnam time and survives bad input', () => {
    expect(formatDateTime('2026-11-11T09:30:00+07:00')).toBe('09:30 11/11')
    expect(formatDateTime(null)).toBe('–')
    expect(formatDateTime('garbage')).toBe('–')
  })
})

describe('invite link from the server', () => {
  it('reads the exact format stock_analyst.serve builds', () => {
    expect(parseInviteHash('#api=https%3A%2F%2Fabc-def.trycloudflare.com&token=test-token-not-real-0123456789abcdefABCDEFG'))
      .toEqual({ api: 'https://abc-def.trycloudflare.com', token: 'test-token-not-real-0123456789abcdefABCDEFG' })
  })
})

describe('personal watchlist', () => {
  it('stays null until changed, then remembers even an empty list', () => {
    const storage = memory()
    expect(loadWatchlist(storage)).toBeNull()
    saveWatchlist([], storage)
    expect(loadWatchlist(storage)).toEqual([])
    saveWatchlist(['FPT', 'HPG'], storage)
    expect(loadWatchlist(storage)).toEqual(['FPT', 'HPG'])
    saveWatchlist(null, storage)
    expect(loadWatchlist(storage)).toBeNull()
  })

  it('drops damaged or duplicate entries and survives blocked storage', () => {
    const storage = memory()
    storage.setItem('bang-dien-watchlist', JSON.stringify(['FPT', 'FPT', '../x', 5, 'toolong']))
    expect(loadWatchlist(storage)).toEqual(['FPT'])
    storage.setItem('bang-dien-watchlist', '{broken')
    expect(loadWatchlist(storage)).toBeNull()
    storage.setItem('bang-dien-watchlist', '{"a":1}')
    expect(loadWatchlist(storage)).toBeNull()
    const blocked = { getItem() { throw new Error('x') }, setItem() { throw new Error('x') }, removeItem() { throw new Error('x') } }
    expect(loadWatchlist(blocked)).toBeNull()
    expect(() => saveWatchlist(['FPT'], blocked)).not.toThrow()
    expect(() => saveWatchlist(null, blocked)).not.toThrow()
  })

  it('toggles and moves without mutating or leaving the ends', () => {
    const list = ['A11', 'B22', 'C33']
    expect(toggleSymbol(list, 'B22')).toEqual(['A11', 'C33'])
    expect(toggleSymbol(list, 'D44')).toEqual(['A11', 'B22', 'C33', 'D44'])
    expect(moveSymbol(list, 'C33', -1)).toEqual(['A11', 'C33', 'B22'])
    expect(moveSymbol(list, 'A11', 1)).toEqual(['B22', 'A11', 'C33'])
    expect(moveSymbol(list, 'A11', -1)).toBe(list)
    expect(moveSymbol(list, 'C33', 1)).toBe(list)
    expect(moveSymbol(list, 'ZZZ', 1)).toBe(list)
    expect(list).toEqual(['A11', 'B22', 'C33'])
  })
})

describe('symbol lists', () => {
  const universe = [
    { symbol: 'HPG', exchange: 'HOSE', name: 'Tập đoàn Hòa Phát' },
    { symbol: 'FPT', exchange: 'HOSE', name: 'FPT Corp' },
    { symbol: 'SHS', exchange: 'HNX', name: 'Chứng khoán Sài Gòn Hà Nội' },
    { symbol: 'AHP', exchange: 'UPCOM', name: 'Đầu tư A' },
  ]

  it('splits by exchange and keeps the watchlist order', () => {
    expect(filterSymbols(universe, { tab: 'HNX', term: '' }).map((e) => e.symbol)).toEqual(['SHS'])
    expect(filterSymbols(universe, { tab: 'watch', term: '', watchlist: ['FPT', 'HPG', 'XYZ'] }).map((e) => e.symbol)).toEqual(['FPT', 'HPG', 'XYZ'])
  })

  it('searches the whole market by code or unaccented name', () => {
    expect(filterSymbols(universe, { tab: 'HNX', term: 'fpt' }).map((e) => e.symbol)).toEqual(['FPT'])
    expect(filterSymbols(universe, { tab: 'watch', term: 'hoa phat' }).map((e) => e.symbol)).toEqual(['HPG'])
    expect(filterSymbols(universe, { tab: 'HOSE', term: 'dau tu' }).map((e) => e.symbol)).toEqual(['AHP'])
    expect(plainText('Đầu Tư')).toBe('dau tu')
  })

  it('ranks an exact code ahead of partial matches', () => {
    const list = [{ symbol: 'AHP', exchange: 'UPCOM', name: '' }, { symbol: 'HPG', exchange: 'HOSE', name: '' }, { symbol: 'HP', exchange: 'HOSE', name: '' }]
    expect(filterSymbols(list, { tab: 'HOSE', term: 'hp' }).map((e) => e.symbol)).toEqual(['HP', 'HPG', 'AHP'])
  })

  it('pages safely past either end', () => {
    const rows = Array.from({ length: PAGE_SIZE + 1 }, (_, i) => i)
    expect(pageSlice(rows, 0)).toMatchObject({ page: 0, pages: 2 })
    expect(pageSlice(rows, 0).rows).toHaveLength(PAGE_SIZE)
    expect(pageSlice(rows, 9)).toMatchObject({ page: 1, pages: 2, rows: [PAGE_SIZE] })
    expect(pageSlice([], 3)).toMatchObject({ page: 0, pages: 1, rows: [] })
  })
})

describe('invite link', () => {
  it('reads api and token from the hash', () => {
    expect(parseInviteHash('#api=https%3A%2F%2Fa-b.trycloudflare.com%2F&token=abc')).toEqual({ api: 'https://a-b.trycloudflare.com', token: 'abc' })
    expect(parseInviteHash('#token=abc')).toEqual({ api: '', token: 'abc' })
    expect(parseInviteHash('')).toBeNull()
  })

  it('refuses insecure or malformed servers', () => {
    expect(() => normalizeApi('http://evil.test')).toThrow()
    expect(() => normalizeApi('javascript:alert(1)')).toThrow()
    expect(() => normalizeApi('not a url')).toThrow()
    expect(normalizeApi('http://127.0.0.1:8787/')).toBe('http://127.0.0.1:8787')
    expect(normalizeApi('https://x.test/path')).toBe('https://x.test')
  })

  it('survives broken or unavailable storage', () => {
    const storage = memory()
    saveConnection({ api: '', token: 't' }, storage)
    expect(loadConnection(storage).token).toBe('t')
    storage.setItem('bang-dien-connection', '{broken')
    expect(loadConnection(storage)).toBeNull()
    const throwing = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
    expect(loadConnection(throwing)).toBeNull()
    expect(() => saveConnection({ token: 't' }, throwing)).not.toThrow()
  })
})

describe('formats', () => {
  it('shows prices in thousands with Vietnamese separators', () => {
    expect(formatPrice(50800)).toBe('50,80')
    expect(formatVolume(12162200)).toBe('12.162.200')
    expect(formatChange(-2100)).toBe('-2,10')
    expect(formatChange(2100)).toBe('+2,10')
    expect(formatPercent(-3.97)).toBe('-3,97%')
  })

  it('never turns missing data into zero', () => {
    for (const format of [formatPrice, formatVolume, formatChange, formatPercent]) {
      expect(format(null)).toBe('–')
      expect(format(undefined)).toBe('–')
    }
  })
})

describe('direction', () => {
  const levels = { ref: 100, ceiling: 107, floor: 93 }
  it.each([[107, 'ceiling'], [101, 'up'], [100, 'ref'], [99, 'down'], [93, 'floor'], [null, 'none']])('%s is %s', (price, expected) => {
    expect(direction(price, levels)).toBe(expected)
  })
  it('works without ceiling and floor, as in replay', () => {
    expect(direction(101, { ref: 100 })).toBe('up')
    expect(direction(101, {})).toBe('none')
  })
})

describe('chart time', () => {
  it('labels each time shape in Vietnamese order', () => {
    expect(barLabel(9 * 3600 + 15 * 60)).toBe('09:15')
    expect(barLabel('2026-11-05')).toBe('05/11/2026')
    expect(barLabel({ day: 5, month: 11, year: 2026 })).toBe('5/11/2026')
    expect(barLabel(undefined)).toBe('')
  })

  it('shifts intraday epochs and leaves dates alone', () => {
    expect(chartTime(1000)).toBe(1000 + 7 * 3600)
    expect(chartTime('2026-11-06')).toBe('2026-11-06')
  })
})
