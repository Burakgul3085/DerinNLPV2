import { useCallback, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import {
  Bot,
  ExternalLink,
  Github,
  Loader2,
  Sparkles,
  TerminalSquare,
} from 'lucide-react'

// Dev + VITE_API_BASE yok: aynı kökten /api → Vite proxy (port 5173/5174 güvenli). Prod veya .env ile tam URL kullan.
const API_BASE =
  import.meta.env.VITE_API_BASE?.trim() ||
  (import.meta.env.DEV ? '' : 'http://127.0.0.1:8000')

function parseSseBlocks(buffer, onPayload) {
  const delimiter = '\n\n'
  let rest = buffer
  let splitIdx = rest.indexOf(delimiter)
  while (splitIdx !== -1) {
    const rawBlock = rest.slice(0, splitIdx)
    rest = rest.slice(splitIdx + delimiter.length)
    for (const line of rawBlock.split(/\r?\n/)) {
      if (line.startsWith('data: ')) {
        const jsonStr = line.slice(6).trim()
        if (!jsonStr) continue
        try {
          onPayload(JSON.parse(jsonStr))
        } catch {
          /* tek satır JSON bekleniyor */
        }
      }
    }
    splitIdx = rest.indexOf(delimiter)
  }
  return rest
}

export default function App() {
  const [repoUrl, setRepoUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [logLines, setLogLines] = useState([])
  const [readme, setReadme] = useState('')
  const [prUrl, setPrUrl] = useState(null)
  const [bannerError, setBannerError] = useState(null)

  const terminalRef = useRef(null)

  useEffect(() => {
    const el = terminalRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [logLines])

  const appendLog = useCallback((text, tone = 'info') => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
    setLogLines((prev) => [...prev, { id, text, tone }])
  }, [])

  const handleSubmit = async (e) => {
    e.preventDefault()
    const trimmed = repoUrl.trim()
    if (!trimmed || loading) return

    setLoading(true)
    setReadme('')
    setPrUrl(null)
    setBannerError(null)
    setLogLines([])
    appendLog('> SSE bağlantısı kuruluyor…')

    try {
      const response = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({ repo_url: trimmed }),
      })

      if (!response.ok) {
        let detail = `İstek başarısız (${response.status}).`
        try {
          const errJson = await response.json()
          detail =
            typeof errJson.detail === 'string'
              ? errJson.detail
              : JSON.stringify(errJson.detail ?? errJson)
        } catch {
          try {
            detail = await response.text()
          } catch {
            /* noop */
          }
        }
        throw new Error(detail)
      }

      const reader = response.body?.getReader()
      if (!reader) throw new Error('Tarayıcı gövde akışını desteklemiyor.')

      const decoder = new TextDecoder()
      let buffer = ''

      const handlePayload = (payload) => {
        if (payload.type === 'log' && payload.message) {
          appendLog(payload.message)
        } else if (payload.type === 'readme' && typeof payload.content === 'string') {
          setReadme(payload.content)
        } else if (payload.type === 'success') {
          if (payload.pr_url) setPrUrl(payload.pr_url)
          appendLog(payload.message || 'Tamamlandı.', 'success')
        } else if (payload.type === 'error') {
          const msg = payload.message || 'Bilinmeyen hata.'
          appendLog(`[HATA] ${msg}`, 'error')
          setBannerError(msg)
        }
      }

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        buffer = parseSseBlocks(buffer, handlePayload)
      }

      if (buffer.trim()) {
        for (const line of buffer.split(/\r?\n/)) {
          if (!line.startsWith('data: ')) continue
          try {
            handlePayload(JSON.parse(line.slice(6).trim()))
          } catch {
            /* eksik çerçeve */
          }
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      appendLog(`[HATA] ${msg}`, 'error')
      setBannerError(msg)
    } finally {
      setLoading(false)
    }
  }

  const toneClass = (tone) => {
    if (tone === 'error') return 'text-red-400'
    if (tone === 'success') return 'text-emerald-300'
    return 'text-emerald-400'
  }

  return (
    <div className="min-h-screen bg-slate-900 text-white">
      <div className="mx-auto flex max-w-7xl flex-col gap-8 px-4 py-10 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-6 border-b border-slate-800 pb-8">
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-emerald-500/15 ring-1 ring-emerald-400/40">
              <Bot className="h-7 w-7 text-emerald-400" aria-hidden />
            </span>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-3xl">
                Otonom GitHub Dokümantasyon Ajanı
              </h1>
              <p className="mt-1 max-w-3xl text-sm text-slate-400 sm:text-base">
                Repo kaynak kodlarını tarar, Gemini ile profesyonel README üretir ve otomatik Pull Request
                açar.
              </p>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4 lg:flex-row lg:items-end">
            <label className="flex flex-1 flex-col gap-2 text-left">
              <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                <Github className="h-4 w-4 text-slate-400" aria-hidden />
                GitHub Repo URL
              </span>
              <input
                type="text"
                name="repo_url"
                autoComplete="off"
                placeholder="https://github.com/sahip/repo veya owner/repo"
                value={repoUrl}
                onChange={(ev) => setRepoUrl(ev.target.value)}
                className="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 shadow-inner outline-none ring-emerald-500/0 transition placeholder:text-slate-600 focus:border-emerald-500/60 focus:ring-4 focus:ring-emerald-500/15"
              />
            </label>
            <button
              type="submit"
              disabled={loading || !repoUrl.trim()}
              className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 px-8 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/30 transition hover:from-emerald-400 hover:to-teal-400 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {loading ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
                  Çalışıyor…
                </>
              ) : (
                <>
                  <Sparkles className="h-5 w-5" aria-hidden />
                  Ajanı Başlat
                </>
              )}
            </button>
          </form>

          {prUrl && (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-emerald-100">
              <span className="text-sm font-medium">Pull Request hazır.</span>
              <a
                href={prUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-semibold text-slate-950 shadow hover:bg-emerald-400"
              >
                PR&apos;yi GitHub&apos;da aç
                <ExternalLink className="h-4 w-4" aria-hidden />
              </a>
            </div>
          )}

          {bannerError && !prUrl && (
            <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {bannerError}
            </div>
          )}
        </header>

        <section className="grid flex-1 grid-cols-1 gap-6 lg:grid-cols-2 lg:gap-8">
          <div className="flex min-h-[420px] flex-col rounded-2xl border border-slate-800 bg-black/80 shadow-xl shadow-black/40">
            <div className="flex items-center gap-2 border-b border-emerald-900/40 px-4 py-3">
              <TerminalSquare className="h-4 w-4 text-emerald-500" aria-hidden />
              <span className="text-xs font-semibold uppercase tracking-wider text-emerald-500/90">
                Ajan Konsolu
              </span>
            </div>
            <div
              ref={terminalRef}
              className="terminal-scanline relative flex-1 overflow-y-auto px-4 py-4 font-mono text-[13px] leading-relaxed sm:text-sm"
            >
              {logLines.length === 0 && (
                <p className="text-slate-600">
                  Hazır. Repo adresini girip &quot;Ajanı Başlat&quot; ile süreci başlatın.
                </p>
              )}
              {logLines.map((line) => (
                <div key={line.id} className={`terminal-line mb-1 ${toneClass(line.tone)}`}>
                  <span className="select-none text-emerald-700">{'>'}</span>{' '}
                  <span>{line.text}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="flex min-h-[420px] flex-col rounded-2xl border border-slate-800 bg-slate-950/60 shadow-xl shadow-black/30">
            <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                README Önizleme
              </span>
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-4">
              {!readme && (
                <p className="text-sm text-slate-500">
                  Üretilen README burada görünecek. Akış tamamlanınca Markdown olarak işlenecek.
                </p>
              )}
              {readme && (
                <article className="readme-preview">
                  <Markdown>{readme}</Markdown>
                </article>
              )}
            </div>
          </div>
        </section>

        <footer className="border-t border-slate-800 pt-6 text-center text-xs text-slate-600">
          Backend: FastAPI + SSE · Model: Gemini API (otomatik/yapılandırılabilir) · GitHub PyGithub ile.
        </footer>
      </div>
    </div>
  )
}
