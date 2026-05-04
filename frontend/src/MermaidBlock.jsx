import { useEffect, useId, useRef, useState } from 'react'

let configured = false

/** Model bazen <style> veya HTML sızdırır; Mermaid 11 parse hatasını azaltır. */
function sanitizeMermaidChart(src) {
  let s = String(src ?? '')
  s = s.replace(/<style[\s\S]*?<\/style>/gi, '')
  s = s.replace(/<\/?style[^>]*>/gi, '')
  s = s.replace(/%%\{[\s\S]*?\}%%/g, '')
  s = s.replace(/<[^>\n]{1,240}>/g, '')
  return s.trim()
}

export default function MermaidBlock({ chart }) {
  const reactId = useId().replace(/:/g, '')
  const host = useRef(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    const el = host.current
    if (el) el.innerHTML = ''

    ;(async () => {
      try {
        const clean = sanitizeMermaidChart(chart)
        if (!clean) {
          if (!cancelled) setError('Boş Mermaid içeriği')
          return
        }
        const { default: mermaid } = await import('mermaid')
        if (!configured) {
          mermaid.initialize({
            startOnLoad: false,
            theme: 'dark',
            securityLevel: 'strict',
            fontFamily: 'ui-sans-serif, system-ui, sans-serif',
          })
          configured = true
        }
        const renderId = `mmd-${reactId}-${Math.random().toString(36).slice(2, 10)}`
        const { svg } = await mermaid.render(renderId, clean)
        if (!cancelled && host.current) {
          host.current.innerHTML = svg
        }
      } catch (e) {
        if (!cancelled) setError(String(e?.message ?? e))
      }
    })()

    return () => {
      cancelled = true
    }
  }, [chart, reactId])

  if (error) {
    return (
      <pre className="my-3 overflow-x-auto rounded-lg border border-amber-900/50 bg-amber-950/30 p-3 text-xs text-amber-200">
        Mermaid: {error}
      </pre>
    )
  }

  return (
    <div
      ref={host}
      className="mermaid-host my-3 min-h-[2rem] overflow-x-auto rounded-lg border border-slate-700 bg-slate-900/40 p-3 [&_svg]:max-w-full"
      aria-label="Mermaid diyagram"
    />
  )
}
