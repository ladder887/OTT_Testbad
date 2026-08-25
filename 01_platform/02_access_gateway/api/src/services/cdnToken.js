const crypto = require('crypto')
const { requireEnv } = require('../config/env')

const CDN_SHARED_SECRET = requireEnv('CDN_SHARED_SECRET')
const CDN_TOKEN_TTL_SEC = Number(process.env.CDN_TOKEN_TTL_SEC || 1800)

function toBase64Url(text) {
  return Buffer.from(text, 'utf8').toString('base64url')
}

function fromBase64Url(encoded) {
  return Buffer.from(encoded, 'base64url').toString('utf8')
}

function sanitizeOptionalTag(value, maxLength = 64) {
  if (value === undefined || value === null) {
    return ''
  }

  return String(value)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '')
    .slice(0, maxLength)
}

function buildCanonicalString(payload) {
  return [
    `uid=${payload.uid}`,
    `sid=${payload.sid}`,
    `pid=${payload.pid}`,
    `jti=${payload.jti}`,
    `cid=${payload.cid}`,
    `path=${payload.path}`,
    `edge=${payload.edge}`,
    `iat=${payload.iat}`,
    `exp=${payload.exp}`,
    `ip_bind=${payload.ip_bind ? '1' : '0'}`,
    `client_ip=${payload.client_ip || ''}`,
    `owner_device_id=${sanitizeOptionalTag(payload.odv, 64)}`,
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
  playbackId = crypto.randomUUID(),
  contentId,
  hlsPath,
  edgeId,
  clientIp,
  ipBind = false,
  ownerDeviceId = '',
}) {
  const now = Math.floor(Date.now() / 1000)
  const payload = {
    uid: String(userId),
    sid: String(sessionId),
    pid: sanitizeOptionalTag(playbackId, 64),
    jti: crypto.randomUUID(),
    cid: String(contentId),
    path: String(hlsPath),
    edge: String(edgeId),
    iat: now,
    exp: now + CDN_TOKEN_TTL_SEC,
    ip_bind: Boolean(ipBind),
    client_ip: clientIp || '',
    odv: sanitizeOptionalTag(ownerDeviceId, 64),
  }

  const token = toBase64Url(JSON.stringify(payload))
  const sig = buildSignature(payload)

  return {
    token,
    sig,
    payload,
  }
}

function tokenGraphIdFromJti(jti) {
  if (!jti) return ''
  const digest = crypto.createHash('sha256').update(String(jti)).digest('hex')
  return `cdn_${digest.slice(0, 24)}`
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
  tokenGraphIdFromJti,
}
