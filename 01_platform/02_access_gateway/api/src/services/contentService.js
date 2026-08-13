const DEFAULT_CONTENTS = [
  {
    id: 'movie_001',
    hlsPath: 'cat1',
    title: '지구달이1',
    description: '지구달이 시리즈 1편',
    thumbnail: '/thumbnails/cat1.jpg',
    backdrop: '/thumbnails/cat1_backdrop.jpg',
    duration: '3분 29초',
    durationSec: 209,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '콘텐츠',
    contentType: 'vod',
    featured: true,
    availableResolutions: ['1080p', '720p'],
  },
  {
    id: 'movie_002',
    hlsPath: 'cat2',
    title: '지구달이2',
    description: '지구달이 시리즈 2편',
    thumbnail: '/thumbnails/cat2.jpg',
    backdrop: '/thumbnails/cat2_backdrop.jpg',
    duration: '3분 15초',
    durationSec: 195,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '콘텐츠',
    contentType: 'vod',
    featured: false,
    availableResolutions: ['1080p', '720p'],
  },
  {
    id: 'movie_003',
    hlsPath: 'cat3',
    title: '지구달이3',
    description: '지구달이 시리즈 3편',
    thumbnail: '/thumbnails/cat3.jpg',
    backdrop: '/thumbnails/cat3_backdrop.jpg',
    duration: '2분 45초',
    durationSec: 165,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '콘텐츠',
    contentType: 'vod',
    featured: false,
    availableResolutions: ['1080p', '720p'],
  },
  {
    id: 'movie_004',
    hlsPath: 'cat4',
    title: '지구달이4',
    description: '지구달이 시리즈 4편',
    thumbnail: '/thumbnails/cat4.jpg',
    backdrop: '/thumbnails/cat4_backdrop.jpg',
    duration: '4분 10초',
    durationSec: 250,
    year: 2025,
    rating: '전체',
    genre: ['영상', '시리즈'],
    category: '콘텐츠',
    contentType: 'vod',
    featured: false,
    availableResolutions: ['1080p', '720p'],
  },
  {
    id: 'live_001',
    hlsPath: 'live_001',
    title: '라이브 채널 1',
    description: '실증랩 라이브 채널 1',
    thumbnail: '/thumbnails/live_001.jpg',
    backdrop: '/thumbnails/live_001_backdrop.jpg',
    duration: 'LIVE',
    durationSec: null,
    year: 2026,
    rating: '전체',
    genre: ['라이브', '스포츠'],
    category: '라이브',
    contentType: 'live',
    featured: false,
    availableResolutions: ['1080p', '720p'],
  },
  {
    id: 'live_002',
    hlsPath: 'live_002',
    title: '라이브 채널 2',
    description: '실증랩 라이브 채널 2',
    thumbnail: '/thumbnails/live_002.jpg',
    backdrop: '/thumbnails/live_002_backdrop.jpg',
    duration: 'LIVE',
    durationSec: null,
    year: 2026,
    rating: '전체',
    genre: ['라이브', '이벤트'],
    category: '라이브',
    contentType: 'live',
    featured: false,
    availableResolutions: ['1080p', '720p'],
  },
]

let initPromise = null

function normalizeArrayField(value, fallback) {
  if (Array.isArray(value) && value.length > 0) {
    return value
  }
  if (typeof value === 'string' && value.trim()) {
    return value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  }
  return fallback
}

function rowToContent(row) {
  if (!row) return null

  return {
    id: row.content_id,
    hlsPath: row.hls_path,
    title: row.title,
    description: row.description,
    thumbnail: row.thumbnail,
    backdrop: row.backdrop,
    duration: row.duration,
    durationSec: row.duration_sec,
    year: row.year,
    rating: row.rating,
    genre: row.genre || [],
    category: row.category,
    contentType: row.content_type,
    featured: row.featured,
    availableResolutions: row.available_resolutions || ['1080p', '720p'],
    sourcePath: row.source_path || null,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  }
}

async function ensureContentsSchema(pgPool) {
  if (initPromise) {
    return initPromise
  }

  initPromise = (async () => {
    const tableCheckResult = await pgPool.query("SELECT to_regclass('public.contents') IS NOT NULL AS exists")
    const contentsTableAlreadyExists = Boolean(tableCheckResult.rows[0]?.exists)

    await pgPool.query(`
      CREATE TABLE IF NOT EXISTS contents (
        id SERIAL PRIMARY KEY,
        content_id VARCHAR(100) UNIQUE NOT NULL,
        hls_path VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL,
        description TEXT,
        thumbnail VARCHAR(500),
        backdrop VARCHAR(500),
        duration VARCHAR(50),
        duration_sec INTEGER,
        year INTEGER,
        rating VARCHAR(20),
        genre TEXT[] DEFAULT '{}',
        category VARCHAR(100) DEFAULT '콘텐츠',
        content_type VARCHAR(20) DEFAULT 'vod',
        featured BOOLEAN DEFAULT false,
        available_resolutions TEXT[] DEFAULT ARRAY['1080p','720p'],
        source_path VARCHAR(500),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    `)

    await pgPool.query('CREATE INDEX IF NOT EXISTS idx_contents_content_type ON contents(content_type)')
    await pgPool.query('CREATE INDEX IF NOT EXISTS idx_contents_category ON contents(category)')

    // 기본 샘플 데이터는 테이블이 처음 생성된 경우에만 1회 삽입한다.
    if (!contentsTableAlreadyExists) {
      for (const item of DEFAULT_CONTENTS) {
        await pgPool.query(
          `INSERT INTO contents
            (content_id, hls_path, title, description, thumbnail, backdrop, duration, duration_sec, year, rating, genre, category, content_type, featured, available_resolutions)
           VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
           ON CONFLICT (content_id) DO NOTHING`,
          [
            item.id,
            item.hlsPath,
            item.title,
            item.description,
            item.thumbnail,
            item.backdrop,
            item.duration,
            item.durationSec,
            item.year,
            item.rating,
            item.genre,
            item.category,
            item.contentType,
            item.featured,
            item.availableResolutions,
          ]
        )
      }
    }
  })().catch((error) => {
    initPromise = null
    throw error
  })

  return initPromise
}

async function listContents(pgPool, filters = {}) {
  await ensureContentsSchema(pgPool)

  const where = []
  const params = []

  if (filters.type) {
    params.push(filters.type.toLowerCase())
    where.push(`content_type = $${params.length}`)
  }

  const whereClause = where.length > 0 ? `WHERE ${where.join(' AND ')}` : ''
  const result = await pgPool.query(
    `SELECT * FROM contents
     ${whereClause}
     ORDER BY featured DESC, year DESC, title ASC`,
    params
  )

  return result.rows.map(rowToContent)
}

async function getContentById(pgPool, contentId) {
  await ensureContentsSchema(pgPool)
  const result = await pgPool.query('SELECT * FROM contents WHERE content_id = $1 LIMIT 1', [contentId])
  return rowToContent(result.rows[0])
}

async function searchContents(pgPool, query) {
  await ensureContentsSchema(pgPool)

  const trimmed = String(query || '').trim()
  if (!trimmed) {
    return listContents(pgPool)
  }

  const likeQuery = `%${trimmed}%`
  const result = await pgPool.query(
    `SELECT * FROM contents
     WHERE content_id ILIKE $1
        OR title ILIKE $1
        OR description ILIKE $1
        OR category ILIKE $1
        OR array_to_string(genre, ' ') ILIKE $1
     ORDER BY featured DESC, year DESC, title ASC`,
    [likeQuery]
  )

  return result.rows.map(rowToContent)
}

async function upsertContent(pgPool, payload) {
  await ensureContentsSchema(pgPool)

  const normalized = {
    id: String(payload.id || '').trim(),
    hlsPath: String(payload.hlsPath || payload.id || '').trim(),
    title: String(payload.title || '').trim(),
    description: String(payload.description || '').trim(),
    thumbnail: String(payload.thumbnail || '').trim(),
    backdrop: String(payload.backdrop || '').trim(),
    duration: String(payload.duration || '').trim(),
    durationSec:
      payload.durationSec === null || payload.durationSec === undefined || payload.durationSec === ''
        ? null
        : Number(payload.durationSec),
    year: payload.year ? Number(payload.year) : null,
    rating: String(payload.rating || '전체').trim(),
    genre: normalizeArrayField(payload.genre, []),
    category: String(payload.category || '콘텐츠').trim(),
    contentType: String(payload.contentType || 'vod').toLowerCase(),
    featured: Boolean(payload.featured),
    availableResolutions: normalizeArrayField(payload.availableResolutions, ['1080p', '720p']),
    sourcePath: payload.sourcePath || null,
  }

  if (!normalized.id || !normalized.hlsPath || !normalized.title) {
    throw new Error('id, hlsPath, title are required')
  }

  const result = await pgPool.query(
    `INSERT INTO contents
      (content_id, hls_path, title, description, thumbnail, backdrop, duration, duration_sec, year, rating, genre, category, content_type, featured, available_resolutions, source_path)
     VALUES
      ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
     ON CONFLICT (content_id) DO UPDATE SET
      hls_path = EXCLUDED.hls_path,
      title = EXCLUDED.title,
      description = EXCLUDED.description,
      thumbnail = EXCLUDED.thumbnail,
      backdrop = EXCLUDED.backdrop,
      duration = EXCLUDED.duration,
      duration_sec = EXCLUDED.duration_sec,
      year = EXCLUDED.year,
      rating = EXCLUDED.rating,
      genre = EXCLUDED.genre,
      category = EXCLUDED.category,
      content_type = EXCLUDED.content_type,
      featured = EXCLUDED.featured,
      available_resolutions = EXCLUDED.available_resolutions,
      source_path = COALESCE(EXCLUDED.source_path, contents.source_path),
      updated_at = CURRENT_TIMESTAMP
     RETURNING *`,
    [
      normalized.id,
      normalized.hlsPath,
      normalized.title,
      normalized.description,
      normalized.thumbnail,
      normalized.backdrop,
      normalized.duration,
      normalized.durationSec,
      normalized.year,
      normalized.rating,
      normalized.genre,
      normalized.category,
      normalized.contentType,
      normalized.featured,
      normalized.availableResolutions,
      normalized.sourcePath,
    ]
  )

  return rowToContent(result.rows[0])
}

async function deleteContentById(pgPool, contentId) {
  await ensureContentsSchema(pgPool)

  const normalizedId = String(contentId || '').trim()
  if (!normalizedId) {
    throw new Error('contentId is required')
  }

  const client = await pgPool.connect()

  try {
    await client.query('BEGIN')

    const existingResult = await client.query('SELECT * FROM contents WHERE content_id = $1 LIMIT 1', [normalizedId])
    if (existingResult.rows.length === 0) {
      await client.query('ROLLBACK')
      return null
    }

    await client.query('DELETE FROM favorites WHERE content_id = $1', [normalizedId])
    await client.query('DELETE FROM watch_history WHERE content_id = $1', [normalizedId])

    const deletedResult = await client.query('DELETE FROM contents WHERE content_id = $1 RETURNING *', [normalizedId])
    await client.query('COMMIT')

    return rowToContent(deletedResult.rows[0] || existingResult.rows[0])
  } catch (error) {
    await client.query('ROLLBACK')
    throw error
  } finally {
    client.release()
  }
}

module.exports = {
  ensureContentsSchema,
  listContents,
  getContentById,
  searchContents,
  upsertContent,
  deleteContentById,
}
