const crypto = require('crypto')

const CDN_SHARED_SECRET = process.env.CDN_SHARED_SECRET || 'your_cdn_secret_key_change_this_in_production'
const CDN_TOKEN_TTL_SEC = Number(process.env.CDN_TOKEN_TTL_SEC || 1800)

function toBase64Url(text) {
  return Buffer.from(text, 'utf8').toString('base64url')
}

function fromBase64Url(encoded) {
  return Buffer.from(encoded, 'base64url').toString('utf8')
}

function sanitizeOptionalTag(value, maxLength = 48) {
  if (value === undefined || value === null) {
    return ''
  }

  return String(value)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '')
    .slice(0, maxLength)
}

function buildCanonicalString(payload) {
  const runId = sanitizeOptionalTag(payload.rid, 48)
  const scenarioId = sanitizeOptionalTag(payload.scn, 24).toUpperCase()
  const label = sanitizeOptionalTag(payload.lbl, 48)

  return [
    `uid=${payload.uid}`,
    `sid=${payload.sid}`,
    `cid=${payload.cid}`,
    `path=${payload.path}`,
    `edge=${payload.edge}`,
    `exp=${payload.exp}`,
    `rid=${runId}`,
    `scn=${scenarioId}`,
    `lbl=${label}`,
  ].join('&')
}

function buildSignature(payload) {
  const canonical = buildCanonicalString(payload)

  return crypto
    .createHmac('sha256', CDN_SHARED_SECRET)
    .update(canonical)
    .digest('hex')
}

function issuePlaybackToken({
  userId,
  sessionId,
  contentId,
  hlsPath,
  edgeId,
  clientIp,
  ipBind = false,
  runId = '',
  scenarioId = '',
  label = '',
  datasetLabel = '',
}) {
  const resolvedLabel = sanitizeOptionalTag(label || datasetLabel, 48)

  const now = Math.floor(Date.now() / 1000)
  const payload = {
    uid: String(userId),
    sid: String(sessionId),
    cid: String(contentId),
    path: String(hlsPath),
    edge: String(edgeId),
    iat: now,
    exp: now + CDN_TOKEN_TTL_SEC,
    ip_bind: Boolean(ipBind),
    client_ip: clientIp || '',
    rid: sanitizeOptionalTag(runId, 48),
    scn: sanitizeOptionalTag(scenarioId, 24).toUpperCase(),
    lbl: resolvedLabel,
  }

  const token = toBase64Url(JSON.stringify(payload))
  const sig = buildSignature(payload)

  return {
    token,
    sig,
    payload,
  }
}

function extractHlsPathFromUri(uri) {
  if (!uri || typeof uri !== 'string') return ''
  const withoutQuery = uri.split('?')[0]
  const parts = withoutQuery.split('/').filter(Boolean)

  const hlsIndex = parts.indexOf('hls')
  if (hlsIndex === -1 || parts.length < hlsIndex + 2) {
    return ''
  }

  return parts[hlsIndex + 1]
}

function verifyPlaybackToken({ token, sig, edgeId, requestUri, clientIp }) {
  if (!token || !sig) {
    return { valid: false, reason: 'missing_token_or_signature' }
  }

  let payload
  try {
    payload = JSON.parse(fromBase64Url(token))
  } catch (error) {
    return { valid: false, reason: 'invalid_token_encoding' }
  }

  const now = Math.floor(Date.now() / 1000)
  if (!payload.exp || now > Number(payload.exp)) {
    return { valid: false, reason: 'token_expired', payload }
  }

  const expectedSig = buildSignature(payload)
  if (expectedSig !== sig) {
    return { valid: false, reason: 'invalid_signature', payload }
  }

  if (payload.edge !== edgeId) {
    return { valid: false, reason: 'edge_mismatch', payload }
  }

  if (payload.ip_bind && payload.client_ip && payload.client_ip !== clientIp) {
    return { valid: false, reason: 'ip_mismatch', payload }
  }

  const requestedPath = extractHlsPathFromUri(requestUri)
  if (!requestedPath || requestedPath !== payload.path) {
    return { valid: false, reason: 'content_path_mismatch', payload }
  }

  return {
    valid: true,
    payload,
  }
}

module.exports = {
  issuePlaybackToken,
  verifyPlaybackToken,
  extractHlsPathFromUri,
}
