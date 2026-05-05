import { Children, isValidElement, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import remarkGfm from 'remark-gfm'
import MermaidBlock from './MermaidBlock.jsx'
import {
  Bot,
  Check,
  Copy,
  ExternalLink,
  FileDown,
  FolderGit2,
  Github,
  Loader2,
  MessageSquare,
  Sparkles,
  TerminalSquare,
  Users,
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

/**
 * CSV alanı — ayırıcıya göre tırnak + kaçış (RFC 4180).
 * Türkiye / birçok Avrupa Excel sürümünde varsayılan liste ayırıcı `;` olduğu için
 * virgüllü CSV tek sütunda kalır; bu yüzden dışa aktarımda `;` kullanıyoruz.
 */
function csvEscapeCell(value, delimiter) {
  if (value == null || value === '') return ''
  const s = String(value)
  const needsQuote = /["\r\n]/.test(s) || s.includes(delimiter)
  if (needsQuote) return `"${s.replace(/"/g, '""')}"`
  return s
}

/** Profil depo listesi: UTF-8 BOM + noktalı virgül ayırıcı (TR Excel ve çoğu Avrupa yerel ayarı) */
function buildProfileReposCsv(login, repos) {
  const d = ';'
  const header = [
    'kullanici',
    'tam_ad',
    'ad',
    'html_url',
    'readme_var',
    'aciklama',
    'ozel',
    'fork',
    'arsiv',
    'varsayilan_dal',
    'dil',
    'son_push',
  ]
  const bodyRows = [header.join(d)]
  for (const r of repos) {
    bodyRows.push(
      [
        csvEscapeCell(login, d),
        csvEscapeCell(r.full_name, d),
        csvEscapeCell(r.name, d),
        csvEscapeCell(r.html_url, d),
        csvEscapeCell(r.readme_present ? 'evet' : 'hayir', d),
        csvEscapeCell(r.description ?? '', d),
        csvEscapeCell(r.private ? 'evet' : 'hayir', d),
        csvEscapeCell(r.fork ? 'evet' : 'hayir', d),
        csvEscapeCell(r.archived ? 'evet' : 'hayir', d),
        csvEscapeCell(r.default_branch ?? '', d),
        csvEscapeCell(r.language ?? '', d),
        csvEscapeCell(r.pushed_at ?? '', d),
      ].join(d),
    )
  }
  return `\ufeff${bodyRows.join('\r\n')}`
}

function markdownComponents() {
  return {
    pre({ children }) {
      const arr = Children.toArray(children)
      const first = arr[0]
      if (
        isValidElement(first) &&
        typeof first.props.className === 'string' &&
        first.props.className.includes('language-mermaid')
      ) {
        const raw = first.props.children
        const chart = Array.isArray(raw) ? raw.join('') : String(raw ?? '')
        return <MermaidBlock chart={chart.replace(/\n$/, '')} />
      }
      return <pre className="readme-pre-wrap">{children}</pre>
    },
  }
}

export default function App() {
  const mdComponents = useMemo(markdownComponents, [])
  const [repoUrl, setRepoUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [logLines, setLogLines] = useState([])
  const [readme, setReadme] = useState('')
  const [prUrl, setPrUrl] = useState(null)
  const [bannerError, setBannerError] = useState(null)
  const [exportBusy, setExportBusy] = useState(false)
  const [extraInstruction, setExtraInstruction] = useState('')
  const [instructionMode, setInstructionMode] = useState('tam_ve_vurgu')

  const [profileUrl, setProfileUrl] = useState('')
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileError, setProfileError] = useState(null)
  const [profileResult, setProfileResult] = useState(null)
  const [profileCopyHint, setProfileCopyHint] = useState(null)

  const terminalRef = useRef(null)
  const repoSectionRef = useRef(null)
  const profileCopyHintTimerRef = useRef(null)

  useEffect(() => {
    const el = terminalRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [logLines])

  useEffect(() => {
    return () => {
      if (profileCopyHintTimerRef.current) clearTimeout(profileCopyHintTimerRef.current)
    }
  }, [])

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
      const payload = { repo_url: trimmed }
      const ek = extraInstruction.trim()
      if (ek) {
        payload.ek_talimat = ek
        payload.talimat_modu = instructionMode
      }

      const response = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify(payload),
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

  const handleProfileAnalyze = async (e) => {
    e.preventDefault()
    const trimmed = profileUrl.trim()
    if (!trimmed || profileLoading) return

    setProfileLoading(true)
    setProfileError(null)
    setProfileResult(null)
    setProfileCopyHint(null)

    try {
      const response = await fetch(`${API_BASE}/api/profile/repos`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_url: trimmed }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        const detail =
          typeof data.detail === 'string'
            ? data.detail
            : Array.isArray(data.detail)
              ? data.detail.map((d) => d.msg || d).join('; ')
              : response.statusText || 'İstek başarısız.'
        throw new Error(detail)
      }
      setProfileResult(data)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setProfileError(msg)
    } finally {
      setProfileLoading(false)
    }
  }

  const fillRepoFromProfile = (htmlUrl) => {
    setRepoUrl(htmlUrl)
    setBannerError(null)
    repoSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const showProfileCopyHint = useCallback((hint) => {
    if (profileCopyHintTimerRef.current) clearTimeout(profileCopyHintTimerRef.current)
    setProfileCopyHint(hint)
    profileCopyHintTimerRef.current = setTimeout(() => {
      setProfileCopyHint(null)
      profileCopyHintTimerRef.current = null
    }, 2500)
  }, [])

  const handleDownloadProfileCsv = useCallback(() => {
    if (!profileResult?.login) return
    const repos = profileResult.repos || []
    const csv = buildProfileReposCsv(profileResult.login, repos)
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const safe = String(profileResult.login).replace(/[^\w.-]+/g, '_').slice(0, 64)
    a.href = url
    a.download = `github-depolar-${safe}.csv`
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    showProfileCopyHint({ tone: 'success', text: 'CSV dosyası indirildi.' })
  }, [profileResult, showProfileCopyHint])

  const handleCopyProfileCsv = useCallback(async () => {
    if (!profileResult?.login) return
    const repos = profileResult.repos || []
    const csv = buildProfileReposCsv(profileResult.login, repos)
    try {
      await navigator.clipboard.writeText(csv)
      showProfileCopyHint({ tone: 'success', text: 'Tablo panoya (CSV metni) kopyalandı.' })
    } catch {
      showProfileCopyHint({
        tone: 'error',
        text: 'Panoya kopyalanamadı; tarayıcı izni veya güvenli bağlam (HTTPS) gerekli olabilir.',
      })
    }
  }, [profileResult, showProfileCopyHint])

  const toneClass = (tone) => {
    if (tone === 'error') return 'text-red-400'
    if (tone === 'success') return 'text-emerald-300'
    return 'text-emerald-400'
  }

  const handleExportWord = async () => {
    if (!readme.trim() || exportBusy) return
    setExportBusy(true)
    try {
      const { buildReadmeDocxBlob, downloadReadmeDocx } = await import('./readmeToDocx.js')
      const blob = await buildReadmeDocxBlob(readme)
      const raw = repoUrl.trim().replace(/\.git$/i, '')
      const parts = raw.split('/').filter(Boolean)
      const slug = (parts.length >= 2 ? `${parts[parts.length - 2]}_${parts[parts.length - 1]}` : 'README')
        .replace(/[^\w.-]+/g, '_')
        .slice(0, 80)
      downloadReadmeDocx(blob, `README-${slug}.docx`)
      appendLog('> README Word (.docx) olarak indirildi.', 'success')
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      appendLog(`[HATA] Word dışa aktarma: ${msg}`, 'error')
    } finally {
      setExportBusy(false)
    }
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

          <div className="rounded-2xl border border-slate-800 bg-slate-950/40 p-5 shadow-inner shadow-black/20">
            <div className="mb-4 flex flex-wrap items-start gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-teal-500/15 ring-1 ring-teal-400/30">
                <Users className="h-5 w-5 text-teal-400" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="text-sm font-semibold text-slate-200 sm:text-base">
                  Kullanıcı profilinden depo listesi
                </h2>
                <p className="mt-1 text-xs text-slate-500 sm:text-sm">
                  Sadece profil bağlantısı girin; tüm herkese açık depolar listelenir. README varlığı kök
                  dosyaya göre kontrol edilir. Satırdaki bağlantıyı aşağıdaki «Repo URL» alanına aktarıp mevcut
                  ajan akışını kullanın.
                </p>
              </div>
            </div>

            <form
              onSubmit={handleProfileAnalyze}
              className="flex flex-col gap-3 sm:flex-row sm:items-end"
            >
              <label className="flex min-w-0 flex-1 flex-col gap-2 text-left">
                <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                  <FolderGit2 className="h-4 w-4 text-slate-400" aria-hidden />
                  GitHub profil URL
                </span>
                <input
                  type="text"
                  name="profile_url"
                  autoComplete="off"
                  placeholder="https://github.com/kullaniciadi"
                  value={profileUrl}
                  onChange={(ev) => setProfileUrl(ev.target.value)}
                  className="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 shadow-inner outline-none transition placeholder:text-slate-600 focus:border-teal-500/60 focus:ring-4 focus:ring-teal-500/15"
                />
              </label>
              <button
                type="submit"
                disabled={profileLoading || !profileUrl.trim()}
                className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl border border-teal-500/50 bg-teal-500/15 px-6 py-3 text-sm font-semibold text-teal-100 shadow transition hover:bg-teal-500/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {profileLoading ? (
                  <>
                    <Loader2 className="h-5 w-5 animate-spin" aria-hidden />
                    Analiz…
                  </>
                ) : (
                  <>
                    <Sparkles className="h-5 w-5 text-teal-300" aria-hidden />
                    Analiz
                  </>
                )}
              </button>
            </form>

            {profileError && (
              <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                {profileError}
              </div>
            )}

            {profileResult && (
              <div className="mt-4 overflow-hidden rounded-xl border border-slate-800">
                <div className="flex flex-col gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-2.5 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
                  <span className="text-xs text-slate-400">
                    <span className="font-semibold text-slate-200">@{profileResult.login}</span>
                    <span className="mx-2 text-slate-600">·</span>
                    {profileResult.total} depo
                  </span>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={handleDownloadProfileCsv}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-800/90 px-3 py-1.5 text-xs font-semibold text-slate-100 shadow-sm transition hover:border-teal-500/50 hover:bg-slate-800"
                    >
                      <FileDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      CSV indir
                    </button>
                    <button
                      type="button"
                      onClick={handleCopyProfileCsv}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-800/90 px-3 py-1.5 text-xs font-semibold text-slate-100 shadow-sm transition hover:border-teal-500/50 hover:bg-slate-800"
                    >
                      <Copy className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      Panoya kopyala
                    </button>
                  </div>
                </div>
                {profileCopyHint && (
                  <div
                    className={`flex items-center gap-2 border-b border-slate-800/80 px-4 py-2 text-xs ${
                      profileCopyHint.tone === 'error'
                        ? 'bg-red-500/10 text-red-200'
                        : 'bg-emerald-500/10 text-emerald-200'
                    }`}
                  >
                    {profileCopyHint.tone === 'success' && (
                      <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" aria-hidden />
                    )}
                    {profileCopyHint.text}
                  </div>
                )}
                <div className="max-h-[min(420px,55vh)] overflow-auto">
                  <table className="w-full min-w-[640px] border-collapse text-left text-sm">
                    <thead className="sticky top-0 z-[1] bg-slate-900/95 backdrop-blur-sm">
                      <tr className="border-b border-slate-800 text-xs uppercase tracking-wide text-slate-500">
                        <th className="px-3 py-2.5 font-semibold">Depo</th>
                        <th className="px-3 py-2.5 font-semibold">README</th>
                        <th className="hidden px-3 py-2.5 font-semibold sm:table-cell">Dal</th>
                        <th className="hidden px-3 py-2.5 font-semibold md:table-cell">Dil</th>
                        <th className="px-3 py-2.5 font-semibold">Bağlantı</th>
                        <th className="px-3 py-2.5 font-semibold">Ajan</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(profileResult.repos || []).map((r) => (
                        <tr
                          key={r.full_name}
                          className="border-b border-slate-800/80 transition hover:bg-slate-800/30"
                        >
                          <td className="px-3 py-2.5 align-top">
                            <div className="font-medium text-slate-100">{r.name}</div>
                            {r.description && (
                              <div className="mt-0.5 line-clamp-2 text-xs text-slate-500">{r.description}</div>
                            )}
                            <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-slate-500">
                              {r.private && (
                                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">özel</span>
                              )}
                              {r.fork && (
                                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">fork</span>
                              )}
                              {r.archived && (
                                <span className="rounded bg-amber-900/40 px-1.5 py-0.5 text-amber-200/90">
                                  arşiv
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2.5 align-top whitespace-nowrap">
                            {r.readme_present ? (
                              <span className="rounded-lg bg-emerald-500/20 px-2 py-1 text-xs font-medium text-emerald-300">
                                var
                              </span>
                            ) : (
                              <span className="rounded-lg bg-slate-800 px-2 py-1 text-xs text-slate-400">
                                yok
                              </span>
                            )}
                          </td>
                          <td className="hidden px-3 py-2.5 align-top text-slate-400 sm:table-cell">
                            <code className="text-xs">{r.default_branch}</code>
                          </td>
                          <td className="hidden px-3 py-2.5 align-top text-slate-400 md:table-cell">
                            {r.language || '—'}
                          </td>
                          <td className="max-w-[200px] px-3 py-2.5 align-top">
                            <a
                              href={r.html_url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-1 break-all text-xs text-teal-400 underline-offset-2 hover:text-teal-300 hover:underline"
                            >
                              GitHub
                              <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
                            </a>
                          </td>
                          <td className="px-3 py-2.5 align-top">
                            <button
                              type="button"
                              onClick={() => fillRepoFromProfile(r.html_url)}
                              className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1.5 text-xs font-semibold text-emerald-200 transition hover:bg-emerald-500/20"
                            >
                              Repo alanına aktar
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>

          <div ref={repoSectionRef} className="flex flex-col gap-4">
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
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
                  className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 px-8 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/30 transition hover:from-emerald-400 hover:to-teal-400 disabled:cursor-not-allowed disabled:opacity-40"
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
              </div>

              <label className="flex flex-col gap-2 text-left">
                <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                  <MessageSquare className="h-4 w-4 text-slate-400" aria-hidden />
                  Ek talimatlar (isteğe bağlı)
                </span>
                <textarea
                  name="ek_talimat"
                  rows={3}
                  maxLength={4000}
                  autoComplete="off"
                  placeholder="Boş bırak: tam kapsamlı README. Örnek dolu: «Sadece sistem gereksinimleri ve kurulum adımlarını yaz» veya «Kullanılan teknolojileri tabloda özetle»."
                  value={extraInstruction}
                  onChange={(ev) => setExtraInstruction(ev.target.value)}
                  className="min-h-[88px] w-full resize-y rounded-xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 shadow-inner outline-none transition placeholder:text-slate-600 focus:border-emerald-500/60 focus:ring-4 focus:ring-emerald-500/15"
                />
                <span className="text-xs text-slate-600">
                  Çıktı dili Türkçe kalır. Ek talimat yazdığınızda aşağıdaki mod geçerlidir.
                </span>
              </label>

              <div
                className={`flex flex-col gap-2 rounded-xl border px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-6 ${
                  extraInstruction.trim()
                    ? 'border-emerald-500/30 bg-emerald-500/5'
                    : 'border-slate-800 bg-slate-950/40'
                }`}
              >
                <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Talimat modu
                </span>
                <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
                  <input
                    type="radio"
                    name="talimat_modu"
                    value="odakli"
                    checked={instructionMode === 'odakli'}
                    onChange={() => setInstructionMode('odakli')}
                    disabled={!extraInstruction.trim()}
                    className="h-4 w-4 accent-emerald-500"
                  />
                  Odaklı çıktı (yalnız talep edilen kısım)
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
                  <input
                    type="radio"
                    name="talimat_modu"
                    value="tam_ve_vurgu"
                    checked={instructionMode === 'tam_ve_vurgu'}
                    onChange={() => setInstructionMode('tam_ve_vurgu')}
                    disabled={!extraInstruction.trim()}
                    className="h-4 w-4 accent-emerald-500"
                  />
                  Tam README + talimat vurgusu
                </label>
              </div>
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
          </div>
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
                  Hazır. Repo adresini girip &quot;Ajanı Başlat&quot; ile süreci başlatın. İsterseniz ek
                  talimat alanına odaklı istek yazın; boşsa mevcut tam README akışı çalışır.
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
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                README Önizleme
              </span>
              {readme.trim() && (
                <button
                  type="button"
                  onClick={handleExportWord}
                  disabled={exportBusy}
                  className="inline-flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-800/80 px-3 py-1.5 text-xs font-semibold text-slate-100 shadow-sm transition hover:border-emerald-500/50 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {exportBusy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                  ) : (
                    <FileDown className="h-3.5 w-3.5" aria-hidden />
                  )}
                  Word&apos;e aktar
                </button>
              )}
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-4">
              {!readme && (
                <p className="text-sm text-slate-500">
                  Üretilen README burada görünecek. Akış tamamlanınca Markdown olarak işlenecek.
                </p>
              )}
              {readme && (
                <article className="readme-preview">
                  <Markdown
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={[rehypeRaw]}
                    components={mdComponents}
                  >
                    {readme}
                  </Markdown>
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
