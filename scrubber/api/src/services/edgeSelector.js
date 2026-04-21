const LEGACY_REGION_PREFIXES = [
  { prefix: '192.168.10.', region: 'KR' },
  { prefix: '192.168.20.', region: 'JP' },
  { prefix: '192.168.30.', region: 'SG' },
  { prefix: '192.168.40.', region: 'US' },
]

const EDGE_ID = process.env.EDGE_ID || 'edge-local'

const REGION_EDGE_ID = {
  KR: process.env.EDGE_KR_ID || EDGE_ID,
  JP: process.env.EDGE_JP_ID || EDGE_ID,
  SG: process.env.EDGE_SG_ID || EDGE_ID,
  US: process.env.EDGE_US_ID || EDGE_ID,
}

const REGION_EDGE_URL = {
  KR: process.env.EDGE_KR_URL,
  JP: process.env.EDGE_JP_URL,
  SG: process.env.EDGE_SG_URL,
  US: process.env.EDGE_US_URL,
}

const DEFAULT_EDGE_ID = process.env.EDGE_DEFAULT_ID || process.env.EDGE_KR_ID || EDGE_ID
const DEFAULT_EDGE_URL = process.env.EDGE_DEFAULT_URL || 'http://192.168.0.111'

function normalizeClientIp(rawIp) {
  if (!rawIp) return ''
  let ip = rawIp

  if (ip.includes(',')) {
    ip = ip.split(',')[0].trim()
  }

  if (ip.startsWith('::ffff:')) {
    ip = ip.slice(7)
  }

  return ip.trim()
}

function parseIpv4Octets(ip) {
  const parts = ip.split('.')
  if (parts.length !== 4) return null

  const nums = parts.map((p) => Number(p))
  if (nums.some((n) => Number.isNaN(n) || n < 0 || n > 255)) {
    return null
  }

  return nums
}

function regionFromSingleSubnet(ip) {
  const octets = parseIpv4Octets(ip)
  if (!octets) return null

  if (octets[0] !== 192 || octets[1] !== 168 || octets[2] !== 0) {
    return null
  }

  const last = octets[3]
  if (last >= 11 && last <= 19) return 'KR'
  if (last >= 21 && last <= 29) return 'JP'
  if (last >= 31 && last <= 39) return 'SG'
  if (last >= 41 && last <= 49) return 'US'
  return null
}

function getRegionFromIp(clientIp) {
  const normalized = normalizeClientIp(clientIp)

  const fromSubnet = regionFromSingleSubnet(normalized)
  if (fromSubnet) {
    return fromSubnet
  }

  const match = LEGACY_REGION_PREFIXES.find((item) => normalized.startsWith(item.prefix))
  return match ? match.region : 'UNKNOWN'
}

function getEdgeForIp(clientIp) {
  const region = getRegionFromIp(clientIp)
  const edgeId = REGION_EDGE_ID[region] || DEFAULT_EDGE_ID
  const edgeUrl = REGION_EDGE_URL[region] || DEFAULT_EDGE_URL

  return {
    id: edgeId,
    region,
    url: edgeUrl,
  }
}

module.exports = {
  normalizeClientIp,
  getRegionFromIp,
  getEdgeForIp,
}
