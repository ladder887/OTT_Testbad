const express = require('express')
const fs = require('fs')
const path = require('path')
const multer = require('multer')
const { authenticateToken } = require('./auth')
const {
  listContents,
  getContentById,
  upsertContent,
  ensureContentsSchema,
  deleteContentById,
} = require('../services/contentService')
const {
  transcodeSourceToHls,
  startLiveLoopTranscode,
  stopLiveLoopTranscode,
  removeHlsOutput,
  probeSourceDuration,
} = require('../services/hlsTranscoder')

const router = express.Router()

const sourceUploadDir = process.env.UPLOAD_SOURCE_DIR || '/var/www/source'
const thumbnailUploadDir = process.env.UPLOAD_THUMBNAIL_DIR || '/var/www/thumbnails'
const parsedMaxUploadMb = Number.parseInt(process.env.MAX_UPLOAD_FILE_SIZE_MB || '2048', 10)
const MAX_UPLOAD_FILE_SIZE_MB = Number.isFinite(parsedMaxUploadMb) && parsedMaxUploadMb > 0 ? parsedMaxUploadMb : 2048
const MAX_UPLOAD_FILE_SIZE_BYTES = MAX_UPLOAD_FILE_SIZE_MB * 1024 * 1024

const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    const targetDir = file.fieldname === 'thumbnailImage' ? thumbnailUploadDir : sourceUploadDir
    fs.mkdir(targetDir, { recursive: true }, (error) => cb(error, targetDir))
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname || '').toLowerCase()
    const basename = path
      .basename(file.originalname || 'upload', ext)
      .replace(/[^a-zA-Z0-9_-]/g, '_')
      .slice(0, 40)
    cb(null, `${Date.now()}_${basename || 'upload'}${ext}`)
  },
})

const upload = multer({
  storage,
  limits: {
    fileSize: MAX_UPLOAD_FILE_SIZE_BYTES,
  },
})

function parseBoolean(value, fallback = false) {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'boolean') return value
  const normalized = String(value).trim().toLowerCase()
  return normalized === 'true' || normalized === '1' || normalized === 'yes' || normalized === 'on'
}

function parseList(value, fallback = []) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean)
  }
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  }
  return fallback
}

function parsePositiveInteger(value, fallback = null) {
  if (value === undefined || value === null || value === '') {
    return fallback
  }

  const parsed = Number.parseInt(String(value).trim(), 10)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback
  }

  return parsed
}

function slugify(input) {
  return String(input || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9가-힣]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 60)
}

function requireAdmin(req, res, next) {
  const username = String(req.user?.username || '').toLowerCase()
  const email = String(req.user?.email || '').toLowerCase()

  if (username === 'admin' || email === 'admin@ott.com') {
    return next()
  }

  return res.status(403).json({ error: '관리자 권한이 필요합니다.' })
}

// 전체 콘텐츠 목록
router.get('/list', async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const type = String(req.query.type || '').trim().toLowerCase()

  try {
    const contents = await listContents(pgPool, { type: type || undefined })
    res.json(contents)
  } catch (error) {
    console.error('Get content list error:', error)
    res.status(500).json({ error: '콘텐츠 목록 조회 실패' })
  }
})

// 추천 콘텐츠 (로그인 사용자 대상)
router.get('/recommended/for-you', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool

  try {
    const all = await listContents(pgPool)
    const recommended = all.filter((item) => item.contentType === 'vod').slice(0, 4)
    res.json(recommended)
  } catch (error) {
    console.error('Get recommended error:', error)
    res.status(500).json({ error: '추천 콘텐츠 조회 실패' })
  }
})

// 인기 콘텐츠
router.get('/popular/trending', async (req, res) => {
  const pgPool = req.app.locals.pgPool

  try {
    const trending = await listContents(pgPool)
    res.json(trending.slice(0, 10))
  } catch (error) {
    console.error('Get trending error:', error)
    res.status(500).json({ error: '인기 콘텐츠 조회 실패' })
  }
})

// 장르별 콘텐츠
router.get('/genre/:genre', async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const { genre } = req.params
  const targetGenre = String(genre || '').trim().toLowerCase()

  try {
    const all = await listContents(pgPool)
    const filtered = all.filter((item) => (item.genre || []).some((g) => String(g).toLowerCase() === targetGenre))
    res.json(filtered)
  } catch (error) {
    console.error('Get genre contents error:', error)
    res.status(500).json({ error: '장르별 콘텐츠 조회 실패' })
  }
})

// 관리자용 콘텐츠 목록
router.get('/admin/list', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool

  try {
    const contents = await listContents(pgPool)
    res.json({ count: contents.length, contents })
  } catch (error) {
    console.error('Get admin content list error:', error)
    res.status(500).json({ error: '관리자 콘텐츠 목록 조회 실패' })
  }
})

// 관리자용 콘텐츠 생성/업데이트 + 파일 업로드
router.post(
  '/admin/upload',
  authenticateToken,
  requireAdmin,
  upload.fields([
    { name: 'thumbnailImage', maxCount: 1 },
    { name: 'sourceVideo', maxCount: 1 },
  ]),
  async (req, res) => {
    const pgPool = req.app.locals.pgPool

    try {
      await ensureContentsSchema(pgPool)

      const title = String(req.body.title || '').trim()
      const bodyContentId = String(req.body.content_id || '').trim()
      const generatedId = slugify(title)
      const contentId = bodyContentId || generatedId || `content_${Date.now()}`
      const hlsPath = String(req.body.hls_path || contentId).trim()
      const contentType = String(req.body.content_type || 'vod').trim().toLowerCase()
      const category = contentType === 'live' ? '라이브' : '콘텐츠'

      const thumbnailFile = req.files?.thumbnailImage?.[0]
      const sourceFile = req.files?.sourceVideo?.[0]

      const thumbnailPath = thumbnailFile
        ? `/thumbnails/${thumbnailFile.filename}`
        : String(req.body.thumbnail || '').trim()

      const sourcePath = sourceFile
        ? `/source/${sourceFile.filename}`
        : String(req.body.source_path || '').trim() || null

      let availableResolutions = parseList(req.body.available_resolutions, ['1080p', '720p'])
      const genre = parseList(req.body.genre, contentType === 'live' ? ['라이브'] : ['영상'])
      const existing = await getContentById(pgPool, contentId)
      const targetDurationSecFromBody = parsePositiveInteger(req.body.target_duration_sec, null)
      const targetDurationMinFromBody = parsePositiveInteger(req.body.target_duration_min, null)
      const targetDurationSec =
        targetDurationSecFromBody ||
        (targetDurationMinFromBody ? targetDurationMinFromBody * 60 : null)

      let resolvedDuration =
        contentType === 'live'
          ? 'LIVE'
          : String(req.body.duration || '').trim() || existing?.duration || ''
      let resolvedDurationSec =
        contentType === 'live'
          ? null
          : req.body.duration_sec !== undefined && req.body.duration_sec !== null && req.body.duration_sec !== ''
            ? Number(req.body.duration_sec)
            : existing?.durationSec ?? null

      let generatedHls = null
      if (sourceFile) {
        if (contentType === 'live') {
          generatedHls = await startLiveLoopTranscode({
            sourceFilePath: sourceFile.path,
            hlsPath,
            resolutions: availableResolutions,
          })
        } else {
          const sourceMediaInfo = await probeSourceDuration(sourceFile.path)

          if (targetDurationSec && targetDurationSec < sourceMediaInfo.durationSec) {
            throw new Error(
              `target_duration_sec(${targetDurationSec})는 원본 길이(${sourceMediaInfo.durationSec}) 이상이어야 합니다.`
            )
          }

          generatedHls = await transcodeSourceToHls({
            sourceFilePath: sourceFile.path,
            hlsPath,
            resolutions: availableResolutions,
            targetDurationSec,
            sourceDurationSec: sourceMediaInfo.durationSec,
          })
          resolvedDuration = generatedHls.outputDurationLabel || sourceMediaInfo.durationLabel
          resolvedDurationSec = generatedHls.outputDurationSec || sourceMediaInfo.durationSec
        }

        availableResolutions = generatedHls.resolutions
      }

      const saved = await upsertContent(pgPool, {
        id: contentId,
        hlsPath,
        title,
        description: req.body.description,
        thumbnail: thumbnailPath,
        backdrop: req.body.backdrop || thumbnailPath,
        duration: resolvedDuration,
        durationSec: resolvedDurationSec,
        year: req.body.year,
        rating: req.body.rating,
        genre,
        category,
        contentType,
        featured: parseBoolean(req.body.featured, false),
        availableResolutions,
        sourcePath,
      })

      await pgPool.query(
        `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
         VALUES ($1, 'content_upload', $2, $3, $4)`,
        [
          req.user.userId,
          req.ip,
          req.get('user-agent') || '',
          JSON.stringify({
            content_id: saved.id,
            hls_path: saved.hlsPath,
            content_type: saved.contentType,
            category: saved.category,
            source_path: saved.sourcePath,
            target_duration_sec: targetDurationSec,
            thumbnail: saved.thumbnail,
            hls_generated: Boolean(generatedHls),
            hls_master_path: generatedHls ? `/hls/${generatedHls.hlsPath}/master.m3u8` : null,
          }),
        ]
      )

      res.status(201).json({
        message: generatedHls
          ? contentType === 'live'
            ? '라이브 채널이 등록되고 실시간 루프 스트림이 시작되었습니다.'
            : '콘텐츠가 등록되고 HLS(1080p/720p)가 생성되었습니다.'
          : '콘텐츠가 등록되었습니다.',
        content: saved,
        hls: generatedHls
          ? {
              generated: true,
              mode: generatedHls.mode || (contentType === 'live' ? 'live' : 'vod'),
              hls_path: generatedHls.hlsPath,
              master_playlist: `/hls/${generatedHls.hlsPath}/master.m3u8`,
              resolutions: generatedHls.resolutions,
            }
          : {
              generated: false,
              reason: 'source_video_missing',
            },
      })
    } catch (error) {
      console.error('Content upload error:', error)
      res.status(500).json({
        error: '콘텐츠 등록 중 오류가 발생했습니다.',
        details: error.message,
      })
    }
  }
)

// 관리자용 콘텐츠 삭제
router.delete('/admin/:id', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const contentId = String(req.params.id || '').trim()

  if (!contentId) {
    return res.status(400).json({ error: '삭제할 콘텐츠 ID가 필요합니다.' })
  }

  try {
    const deleted = await deleteContentById(pgPool, contentId)

    if (!deleted) {
      return res.status(404).json({ error: '삭제할 콘텐츠를 찾을 수 없습니다.' })
    }

    try {
      if (deleted.contentType === 'live') {
        await stopLiveLoopTranscode({
          hlsPath: deleted.hlsPath,
          resolutions: deleted.availableResolutions || ['1080p', '720p'],
        })
      }
      await removeHlsOutput(deleted.hlsPath)
    } catch (cleanupError) {
      console.warn('Content delete cleanup warning:', cleanupError.message)
    }

    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'content_delete', $2, $3, $4)`,
      [
        req.user.userId,
        req.ip,
        req.get('user-agent') || '',
        JSON.stringify({
          content_id: deleted.id,
          hls_path: deleted.hlsPath,
          content_type: deleted.contentType,
        }),
      ]
    )

    return res.json({
      message: '콘텐츠가 삭제되었습니다.',
      content: deleted,
    })
  } catch (error) {
    console.error('Content delete error:', error)
    return res.status(500).json({ error: '콘텐츠 삭제 중 오류가 발생했습니다.' })
  }
})

// 특정 콘텐츠 상세 정보
router.get('/:id', async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const { id } = req.params

  try {
    const content = await getContentById(pgPool, id)

    if (!content) {
      return res.status(404).json({ error: '콘텐츠를 찾을 수 없습니다.' })
    }

    res.json(content)
  } catch (error) {
    console.error('Get content error:', error)
    res.status(500).json({ error: '콘텐츠 조회 실패' })
  }
})

router.use((error, req, res, next) => {
  if (error instanceof multer.MulterError) {
    if (error.code === 'LIMIT_FILE_SIZE') {
      return res.status(413).json({
        error: `업로드 파일 크기 제한(${MAX_UPLOAD_FILE_SIZE_MB}MB)을 초과했습니다.`,
        details: '영상 파일을 압축/분할하거나 MAX_UPLOAD_FILE_SIZE_MB 값을 늘려주세요.',
      })
    }

    return res.status(400).json({
      error: '파일 업로드 요청 형식이 올바르지 않습니다.',
      details: error.message,
    })
  }

  if (!error) {
    return next()
  }

  console.error('Content route error:', error)
  return res.status(500).json({
    error: '콘텐츠 요청 처리 중 오류가 발생했습니다.',
    details: error.message,
  })
})

module.exports = router
