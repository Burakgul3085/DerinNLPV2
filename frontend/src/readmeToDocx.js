/**
 * README Markdown → Word (.docx). PR / backend akışına dokunmaz; yalnızca istemci indirmesi.
 */
import {
  AlignmentType,
  BorderStyle,
  Document,
  HeadingLevel,
  LevelFormat,
  Packer,
  Paragraph,
  ShadingType,
  Table,
  TableCell,
  TableRow,
  TextRun,
  UnderlineType,
  WidthType,
  convertInchesToTwip,
} from 'docx'
import { marked } from 'marked'

marked.use({ gfm: true, breaks: true })

const BULLET_REF = 'readme-bullets'

function headingLv(depth) {
  const m = {
    1: HeadingLevel.HEADING_1,
    2: HeadingLevel.HEADING_2,
    3: HeadingLevel.HEADING_3,
    4: HeadingLevel.HEADING_4,
    5: HeadingLevel.HEADING_5,
    6: HeadingLevel.HEADING_6,
  }
  return m[Math.min(Math.max(depth, 1), 6)] ?? HeadingLevel.HEADING_6
}

function extractTextFromTokens(tokens) {
  if (!tokens?.length) return ''
  let s = ''
  for (const t of tokens) {
    if (t.type === 'text') s += t.text || ''
    else if (t.type === 'strong' || t.type === 'em' || t.type === 'paragraph') s += extractTextFromTokens(t.tokens)
    else if (t.type === 'codespan') s += t.text || ''
    else if (t.type === 'link') s += t.text || t.href || ''
    else if (t.tokens) s += extractTextFromTokens(t.tokens)
    else if (t.raw) s += t.raw
  }
  return s
}

function inlineToRuns(tokens) {
  const runs = []
  if (!tokens?.length) return [new TextRun({ text: '\u00A0', size: 22 })]
  for (const t of tokens) {
    if (t.type === 'text') runs.push(new TextRun({ text: t.text || '', size: 22 }))
    else if (t.type === 'strong') {
      runs.push(
        new TextRun({
          text: extractTextFromTokens(t.tokens || []) || t.text || '',
          bold: true,
          size: 22,
        }),
      )
    } else if (t.type === 'em') {
      runs.push(
        new TextRun({
          text: extractTextFromTokens(t.tokens || []) || t.text || '',
          italics: true,
          size: 22,
        }),
      )
    } else if (t.type === 'codespan') {
      runs.push(
        new TextRun({
          text: t.text || '',
          font: 'Consolas',
          size: 20,
          shading: { type: ShadingType.CLEAR, fill: 'E2E8F0' },
        }),
      )
    } else if (t.type === 'link') {
      runs.push(
        new TextRun({
          text: t.text || t.href || '',
          color: '2563EB',
          underline: { type: UnderlineType.SINGLE },
          size: 22,
        }),
      )
    } else if (t.type === 'br') {
      runs.push(new TextRun({ text: '\n', size: 22 }))
    } else if (t.tokens?.length) {
      runs.push(...inlineToRuns(t.tokens))
    } else if (t.raw) {
      runs.push(new TextRun({ text: t.raw, size: 22 }))
    }
  }
  return runs.length ? runs : [new TextRun({ text: '\u00A0', size: 22 })]
}

function flushListItems(items, depth, out) {
  for (const item of items || []) {
    const parts = item.tokens || []
    const nestedLists = parts.filter((p) => p.type === 'list')
    const rest = parts.filter((p) => p.type !== 'list')
    const flatRuns = []
    for (const p of rest) {
      if (p.type === 'paragraph') flatRuns.push(...inlineToRuns(p.tokens))
      else if (p.type === 'text') flatRuns.push(...inlineToRuns([p]))
      else if (p.tokens) flatRuns.push(...inlineToRuns(p.tokens))
    }
    out.push(
      new Paragraph({
        children: flatRuns.length ? flatRuns : [new TextRun({ text: '\u00A0', size: 22 })],
        numbering: { reference: BULLET_REF, level: Math.min(depth, 2) },
        spacing: { after: 80 },
      }),
    )
    for (const nl of nestedLists) {
      flushListItems(nl.items, depth + 1, out)
    }
  }
}

function blockquotePlain(token) {
  const lines = []
  for (const inner of token.tokens || []) {
    if (inner.type === 'paragraph') lines.push(extractTextFromTokens(inner.tokens))
    else if (inner.type === 'text') lines.push(extractTextFromTokens([inner]))
    else lines.push(extractTextFromTokens(inner.tokens || []))
  }
  return lines.filter(Boolean).join('\n')
}

function processToken(token) {
  const out = []
  switch (token.type) {
    case 'heading':
      out.push(
        new Paragraph({
          heading: headingLv(token.depth),
          children: inlineToRuns(token.tokens),
          spacing: { before: 280, after: 140 },
        }),
      )
      break
    case 'paragraph':
      out.push(
        new Paragraph({
          children: inlineToRuns(token.tokens),
          spacing: { after: 140 },
        }),
      )
      break
    case 'space':
      out.push(new Paragraph({ text: '', spacing: { after: 80 } }))
      break
    case 'code': {
      const lang = (token.lang || '').toLowerCase()
      const isMermaid = lang === 'mermaid'
      const txt = isMermaid ? '(Mermaid diyagram — GitHub önizlemesinde görüntülenir.)' : token.text || ''
      out.push(
        new Paragraph({
          children: [new TextRun({ text: txt, font: 'Consolas', size: 20 })],
          shading: { type: ShadingType.CLEAR, fill: 'F1F5F9' },
          spacing: { before: 120, after: 120 },
          border: {
            top: { style: BorderStyle.SINGLE, size: 1, color: 'CBD5E1' },
            bottom: { style: BorderStyle.SINGLE, size: 1, color: 'CBD5E1' },
            left: { style: BorderStyle.SINGLE, size: 1, color: 'CBD5E1' },
            right: { style: BorderStyle.SINGLE, size: 1, color: 'CBD5E1' },
          },
        }),
      )
      break
    }
    case 'blockquote': {
      const q = blockquotePlain(token)
      out.push(
        new Paragraph({
          children: [new TextRun({ text: q || '\u00A0', italics: true, size: 22, color: '475569' })],
          indent: { left: convertInchesToTwip(0.28) },
          border: {
            left: { style: BorderStyle.SINGLE, size: 12, color: '64748B' },
          },
          spacing: { before: 120, after: 160 },
        }),
      )
      break
    }
    case 'list':
      flushListItems(token.items, 0, out)
      out.push(new Paragraph({ text: '', spacing: { after: 100 } }))
      break
    case 'hr':
      out.push(
        new Paragraph({
          border: {
            bottom: { style: BorderStyle.SINGLE, size: 8, color: '94A3B8' },
          },
          spacing: { before: 200, after: 200 },
        }),
      )
      break
    case 'table': {
      const rows = []
      const headerCells = (token.header || []).map(
        (cell) =>
          new TableCell({
            children: [
              new Paragraph({
                alignment: AlignmentType.CENTER,
                children: inlineToRuns(cell.tokens),
              }),
            ],
            shading: { type: ShadingType.CLEAR, fill: 'E2E8F0' },
          }),
      )
      rows.push(new TableRow({ children: headerCells }))
      for (const row of token.rows || []) {
        const cells = row.map(
          (cell) =>
            new TableCell({
              children: [new Paragraph({ children: inlineToRuns(cell.tokens) })],
            }),
        )
        rows.push(new TableRow({ children: cells }))
      }
      out.push(
        new Table({
          width: { size: 100, type: WidthType.PERCENTAGE },
          rows,
        }),
      )
      out.push(new Paragraph({ text: '', spacing: { after: 180 } }))
      break
    }
    default:
      if (token.raw?.trim()) {
        out.push(
          new Paragraph({
            children: [new TextRun({ text: token.raw.trim(), size: 22 })],
            spacing: { after: 100 },
          }),
        )
      }
  }
  return out
}

export async function buildReadmeDocxBlob(markdown) {
  const src = markdown || ''
  const tokens = marked.lexer(src)
  const children = []
  for (const t of tokens) {
    children.push(...processToken(t))
  }

  const doc = new Document({
    creator: 'DerinNLP',
    title: 'README',
    description: 'Otonom README dışa aktarımı',
    numbering: {
      config: [
        {
          reference: BULLET_REF,
          levels: [
            {
              level: 0,
              format: LevelFormat.BULLET,
              text: '\u2022',
              alignment: AlignmentType.LEFT,
              style: {
                paragraph: {
                  indent: { left: convertInchesToTwip(0.35), hanging: convertInchesToTwip(0.2) },
                },
              },
            },
            {
              level: 1,
              format: LevelFormat.BULLET,
              text: '\u2013',
              alignment: AlignmentType.LEFT,
              style: {
                paragraph: {
                  indent: { left: convertInchesToTwip(0.65), hanging: convertInchesToTwip(0.2) },
                },
              },
            },
            {
              level: 2,
              format: LevelFormat.BULLET,
              text: '\u25E6',
              alignment: AlignmentType.LEFT,
              style: {
                paragraph: {
                  indent: { left: convertInchesToTwip(0.95), hanging: convertInchesToTwip(0.2) },
                },
              },
            },
          ],
        },
      ],
    },
    sections: [
      {
        properties: {},
        children:
          children.length > 0
            ? children
            : [new Paragraph({ children: [new TextRun({ text: '(Boş içerik)', size: 22 })], spacing: { after: 120 } })],
      },
    ],
  })

  return Packer.toBlob(doc)
}

export function downloadReadmeDocx(blob, filename = 'README-derinnlp.docx') {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename.replace(/[^\w.-]+/g, '_')
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
