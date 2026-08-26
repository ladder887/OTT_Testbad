const fs = require('fs')
const path = require('path')
const { Pool } = require('pg')

const { ensureContentsSchema, listContents } = require('../services/contentService')
const { startLiveLoopTranscode, stopLiveLoopTranscode } = require('../services/hlsTranscoder')


const sourceUploadDir = process.env.UPLOAD_SOURCE_DIR || '/var/www/source'

function resolveSourceFile(sourcePath) {
  const raw = String(sourcePath || '').trim()
  if (!raw) return ''
  if (raw.startsWith('/source/')) return path.join(sourceUploadDir, raw.slice('/source/'.length))
  if (path.isAbsolute(raw)) return raw
  return path.join(sourceUploadDir, raw.replace(/^\/+/, ''))
}

function requestedIds(args, availableIds) {
  if (args.includes('--all')) return new Set(availableIds)
  if (args.includes('--none')) return new Set()
  return new Set(
    args
      .flatMap((value) => String(value).split(','))
      .map((value) => value.trim())
      .filter(Boolean)
  )
}

async function main() {
  const pool = new Pool({
    host: process.env.POSTGRES_HOST || 'postgres',
    port: Number(process.env.POSTGRES_PORT || 5432),
    database: process.env.POSTGRES_DB || 'ott_auth',
    user: process.env.POSTGRES_USER || 'ott_user',
    password: process.env.POSTGRES_PASSWORD,
  })
  try {
    await ensureContentsSchema(pool)
    const contents = await listContents(pool, { type: 'live' })
    const availableIds = contents.map((content) => content.id)
    const activeIds = requestedIds(process.argv.slice(2), availableIds)
    const unknown = [...activeIds].filter((contentId) => !availableIds.includes(contentId))
    if (unknown.length > 0) {
      throw new Error(`Unknown LIVE content IDs: ${unknown.join(',')}`)
    }

    const results = []
    for (const content of contents) {
      await stopLiveLoopTranscode({
        hlsPath: content.hlsPath,
        resolutions: content.availableResolutions || ['1080p', '720p'],
      })
      if (!activeIds.has(content.id)) {
        results.push({ content_id: content.id, status: 'stopped' })
        continue
      }
      const sourceFilePath = resolveSourceFile(content.sourcePath)
      if (!sourceFilePath || !fs.existsSync(sourceFilePath)) {
        throw new Error(`LIVE source is missing for ${content.id}: ${sourceFilePath || '<empty>'}`)
      }
      const started = await startLiveLoopTranscode({
        sourceFilePath,
        hlsPath: content.hlsPath,
        resolutions: content.availableResolutions || ['1080p', '720p'],
      })
      results.push({
        content_id: content.id,
        status: 'started',
        resolutions: started.resolutions,
        processes: started.processes,
      })
    }
    console.log(JSON.stringify({ ok: true, active_content_ids: [...activeIds].sort(), results }))
  } finally {
    await pool.end()
  }
}

main().catch((error) => {
  console.error(error.stack || error.message)
  process.exitCode = 1
})
