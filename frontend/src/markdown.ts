import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, linkify: true, typographer: false })

markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index]!.attrSet('target', '_blank')
  tokens[index]!.attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}

// Release notes are remote content. Keep images as explicit links so rendering
// Markdown cannot make an unexpected third-party request from the client.
markdown.renderer.rules.image = (tokens, index) => {
  const token = tokens[index]!
  const source = String(token.attrGet('src') || '')
  const label = markdown.utils.escapeHtml(String(token.content || '查看图片'))
  if (!markdown.validateLink(source)) return label
  return `<a href="${markdown.utils.escapeHtml(source)}" target="_blank" rel="noopener noreferrer">${label}</a>`
}

export const renderMarkdown = (source: string) => markdown.render(source)
