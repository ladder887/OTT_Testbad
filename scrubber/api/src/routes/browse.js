const express = require('express')
const { authenticateToken } = require('./auth')
const { searchContents, getContentById } = require('../services/contentService')

const router = express.Router()

router.get('/search', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const query = String(req.query.q || '')
  const results = await searchContents(pgPool, query)

  try {
    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'browse_search', $2, $3, $4)`,
      [
        req.user.userId,
        req.ip,
        req.get('user-agent') || '',
        JSON.stringify({
          query,
          results_count: results.length,
        }),
      ]
    )
  } catch (error) {
    console.error('Browse search audit log error:', error)
  }

  res.json({
    query,
    results_count: results.length,
    items: results,
  })
})

router.get('/content/:contentId', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool
  const { contentId } = req.params
  const content = await getContentById(pgPool, contentId)

  if (!content) {
    return res.status(404).json({ error: '콘텐츠를 찾을 수 없습니다.' })
  }

  try {
    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'browse_content', $2, $3, $4)`,
      [
        req.user.userId,
        req.ip,
        req.get('user-agent') || '',
        JSON.stringify({ content_id: contentId }),
      ]
    )
  } catch (error) {
    console.error('Browse content audit log error:', error)
  }

  res.json({ content })
})

module.exports = router
