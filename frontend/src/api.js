export async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!r.ok) {
    let body = null
    let detail = r.statusText
    try { body = await r.json(); detail = body.detail || JSON.stringify(body) } catch {}
    const e = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    e.status = r.status
    e.body = body
    throw e
  }
  if (r.status === 204) return null
  return r.json()
}
