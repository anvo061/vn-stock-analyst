import { useEffect, useRef, useState } from 'react'

const POLL_MS = 3000
const MAX_BACKOFF_MS = 15000
const TIMEOUT_MS = 10000

export class ApiError extends Error {
  constructor(status, message, retryAfter) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

export async function apiGet(connection, path, signal) {
  const response = await fetch(`${connection.api}${path}`, {
    headers: { Authorization: `Bearer ${connection.token}` },
    signal: AbortSignal.any([signal, AbortSignal.timeout(TIMEOUT_MS)]),
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.detail || `HTTP ${response.status}`, Number(response.headers.get('Retry-After')) || 0)
  }
  return response.json()
}

/**
 * Polls quotes for the symbols on screen, which also moves them into the server's fast tier.
 * Quotes seen earlier are kept so paging back shows numbers at once. Pauses while the tab is
 * hidden. `link` tells the UI when what it holds may be old.
 */
export function useFeed(connection, symbols, onUnauthorized) {
  const [state, setState] = useState({ feed: null, quotes: new Map(), missing: new Set() })
  const [link, setLink] = useState({ state: 'connecting', lastOk: null, error: null })
  const wanted = useRef(symbols)
  wanted.current = symbols
  const unauthorized = useRef(onUnauthorized)
  unauthorized.current = onUnauthorized
  const pollNow = useRef(() => {})
  const key = symbols.join(',')

  useEffect(() => {
    const controller = new AbortController()
    let timer
    let failures = 0
    let running = false
    let rerun = false

    const schedule = (delay) => {
      clearTimeout(timer)
      if (!document.hidden) timer = setTimeout(poll, delay)
    }

    async function poll() {
      if (document.hidden) return
      if (running) {
        rerun = true
        return
      }
      running = true
      const asked = wanted.current
      try {
        const query = asked.length ? `?symbols=${asked.join(',')}` : ''
        const data = await apiGet(connection, `/api/quotes${query}`, controller.signal)
        failures = 0
        setState((previous) => {
          const quotes = new Map(previous.quotes)
          data.quotes.forEach((quote) => quotes.set(quote.symbol, quote))
          return { feed: data.feed, quotes, missing: new Set(data.missing) }
        })
        setLink({ state: 'ok', lastOk: new Date(), error: null })
        schedule(rerun ? 0 : POLL_MS)
      } catch (error) {
        if (controller.signal.aborted) return
        if (error.status === 401) {
          unauthorized.current()
          return
        }
        failures += 1
        setLink((previous) => ({ ...previous, state: 'lost', error: error.message }))
        schedule(error.retryAfter ? error.retryAfter * 1000 : Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS))
      } finally {
        running = false
        rerun = false
      }
    }

    const onVisibility = () => {
      if (document.hidden) clearTimeout(timer)
      else poll()
    }
    pollNow.current = poll
    document.addEventListener('visibilitychange', onVisibility)
    poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [connection])

  // A new page should not wait for the next tick.
  useEffect(() => {
    pollNow.current()
  }, [key])

  return { ...state, link }
}
