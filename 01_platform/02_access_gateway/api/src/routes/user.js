const express = require('express');
const { authenticateToken } = require('./auth');

const router = express.Router();

// 사용자 프로필 조회
router.get('/profile', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;

  try {
    const result = await pgPool.query(
      'SELECT id, email, username, full_name, profile_image, subscription_plan, created_at, last_login FROM users WHERE id = $1',
      [req.user.userId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: '사용자를 찾을 수 없습니다.' });
    }

    res.json({ user: result.rows[0] });
  } catch (error) {
    console.error('Get profile error:', error);
    res.status(500).json({ error: '프로필 조회 실패' });
  }
});

// 시청 기록 조회
router.get('/watch-history', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { limit = 20, offset = 0 } = req.query;

  try {
    const result = await pgPool.query(
      `SELECT * FROM watch_history 
       WHERE user_id = $1 
       ORDER BY updated_at DESC 
       LIMIT $2 OFFSET $3`,
      [req.user.userId, parseInt(limit), parseInt(offset)]
    );

    res.json({ history: result.rows });
  } catch (error) {
    console.error('Get watch history error:', error);
    res.status(500).json({ error: '시청 기록 조회 실패' });
  }
});

// 시청 기록 저장/업데이트
router.post('/watch-history', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { content_id, watch_duration, total_duration, session_token } = req.body;
  const label = String(req.body?.label || req.body?.dataset_label || req.body?.scenario_label || 'normal').trim() || 'normal';

  try {
    const progress_percent = (watch_duration / total_duration) * 100;
    const completed = progress_percent >= 90;

    const result = await pgPool.query(
      `INSERT INTO watch_history (user_id, content_id, session_token, label, watch_duration, total_duration, progress_percent, completed, ip_address)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       ON CONFLICT (user_id, content_id) 
       DO UPDATE SET label = $4, watch_duration = $5, total_duration = $6, progress_percent = $7, completed = $8, ip_address = $9, updated_at = NOW()
       RETURNING *`,
      [req.user.userId, content_id, session_token, label, watch_duration, total_duration, progress_percent, completed, req.ip]
    );

    res.json({ history: result.rows[0] });
  } catch (error) {
    console.error('Save watch history error:', error);
    res.status(500).json({ error: '시청 기록 저장 실패' });
  }
});

// 즐겨찾기 목록
router.get('/favorites', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;

  try {
    const result = await pgPool.query(
      'SELECT * FROM favorites WHERE user_id = $1 ORDER BY created_at DESC',
      [req.user.userId]
    );

    res.json({ favorites: result.rows });
  } catch (error) {
    console.error('Get favorites error:', error);
    res.status(500).json({ error: '즐겨찾기 조회 실패' });
  }
});

// 즐겨찾기 추가
router.post('/favorites', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { content_id } = req.body;

  try {
    const result = await pgPool.query(
      `INSERT INTO favorites (user_id, content_id)
       VALUES ($1, $2)
       ON CONFLICT (user_id, content_id) DO NOTHING
       RETURNING *`,
      [req.user.userId, content_id]
    );

    res.json({ favorite: result.rows[0] });
  } catch (error) {
    console.error('Add favorite error:', error);
    res.status(500).json({ error: '즐겨찾기 추가 실패' });
  }
});

// 즐겨찾기 삭제
router.delete('/favorites/:content_id', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { content_id } = req.params;

  try {
    await pgPool.query(
      'DELETE FROM favorites WHERE user_id = $1 AND content_id = $2',
      [req.user.userId, content_id]
    );

    res.json({ message: '즐겨찾기에서 제거되었습니다.' });
  } catch (error) {
    console.error('Remove favorite error:', error);
    res.status(500).json({ error: '즐겨찾기 삭제 실패' });
  }
});

// 활성 세션 목록
router.get('/sessions', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;

  try {
    const result = await pgPool.query(
      `SELECT id, session_token, ip_address, user_agent, device_type, created_at, last_activity 
       FROM sessions 
       WHERE user_id = $1 AND is_active = true AND expires_at > NOW()
       ORDER BY last_activity DESC`,
      [req.user.userId]
    );

    res.json({ sessions: result.rows });
  } catch (error) {
    console.error('Get sessions error:', error);
    res.status(500).json({ error: '세션 조회 실패' });
  }
});

// 특정 세션 종료
router.delete('/sessions/:session_token', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { session_token } = req.params;

  try {
    await pgPool.query(
      'UPDATE sessions SET is_active = false WHERE user_id = $1 AND session_token = $2',
      [req.user.userId, session_token]
    );

    res.json({ message: '세션이 종료되었습니다.' });
  } catch (error) {
    console.error('End session error:', error);
    res.status(500).json({ error: '세션 종료 실패' });
  }
});

module.exports = router;
