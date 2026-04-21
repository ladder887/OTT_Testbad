const express = require('express');
const { authenticateToken } = require('./auth');

const router = express.Router();

// 관리자 권한 체크 미들웨어
function requireAdmin(req, res, next) {
  // 실제로는 user role을 체크해야 함
  // 여기서는 간단히 특정 이메일만 허용
  const adminEmails = ['admin@ott.com'];
  
  if (!adminEmails.includes(req.user.email)) {
    return res.status(403).json({ error: '관리자 권한이 필요합니다.' });
  }
  
  next();
}

// 전체 사용자 목록
router.get('/users', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { limit = 50, offset = 0 } = req.query;

  try {
    const result = await pgPool.query(
      `SELECT id, email, username, full_name, subscription_plan, is_active, created_at, last_login 
       FROM users 
       ORDER BY created_at DESC 
       LIMIT $1 OFFSET $2`,
      [parseInt(limit), parseInt(offset)]
    );

    const countResult = await pgPool.query('SELECT COUNT(*) FROM users');
    const total = parseInt(countResult.rows[0].count);

    res.json({ 
      users: result.rows,
      total,
      limit: parseInt(limit),
      offset: parseInt(offset)
    });
  } catch (error) {
    console.error('Get users error:', error);
    res.status(500).json({ error: '사용자 목록 조회 실패' });
  }
});

// 사용자 상세 정보
router.get('/users/:userId', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { userId } = req.params;

  try {
    const userResult = await pgPool.query(
      'SELECT id, email, username, full_name, subscription_plan, is_active, created_at, last_login FROM users WHERE id = $1',
      [userId]
    );

    if (userResult.rows.length === 0) {
      return res.status(404).json({ error: '사용자를 찾을 수 없습니다.' });
    }

    const sessionsResult = await pgPool.query(
      `SELECT id, session_token, ip_address, user_agent, is_active, created_at, last_activity 
       FROM sessions 
       WHERE user_id = $1 
       ORDER BY created_at DESC 
       LIMIT 10`,
      [userId]
    );

    const auditResult = await pgPool.query(
      `SELECT action, ip_address, success, created_at 
       FROM audit_logs 
       WHERE user_id = $1 
       ORDER BY created_at DESC 
       LIMIT 20`,
      [userId]
    );

    res.json({
      user: userResult.rows[0],
      sessions: sessionsResult.rows,
      audit_logs: auditResult.rows
    });
  } catch (error) {
    console.error('Get user detail error:', error);
    res.status(500).json({ error: '사용자 상세 조회 실패' });
  }
});

// 사용자 비활성화/활성화
router.patch('/users/:userId/status', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { userId } = req.params;
  const { is_active } = req.body;

  try {
    await pgPool.query(
      'UPDATE users SET is_active = $1 WHERE id = $2',
      [is_active, userId]
    );

    // Audit log
    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'admin_status_change', $2, $3, $4)`,
      [req.user.userId, req.ip, req.get('user-agent'), JSON.stringify({ target_user: userId, is_active })]
    );

    res.json({ message: '사용자 상태가 변경되었습니다.' });
  } catch (error) {
    console.error('Update user status error:', error);
    res.status(500).json({ error: '사용자 상태 변경 실패' });
  }
});

// 전체 세션 목록
router.get('/sessions', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { limit = 100, offset = 0, active_only = 'true' } = req.query;

  try {
    let query = `
      SELECT s.*, u.email, u.username 
      FROM sessions s
      JOIN users u ON s.user_id = u.id
    `;

    if (active_only === 'true') {
      query += ' WHERE s.is_active = true AND s.expires_at > NOW()';
    }

    query += ' ORDER BY s.last_activity DESC LIMIT $1 OFFSET $2';

    const result = await pgPool.query(query, [parseInt(limit), parseInt(offset)]);

    res.json({ sessions: result.rows });
  } catch (error) {
    console.error('Get sessions error:', error);
    res.status(500).json({ error: '세션 목록 조회 실패' });
  }
});

// 특정 세션 강제 종료
router.delete('/sessions/:sessionToken', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;
  const { sessionToken } = req.params;

  try {
    await pgPool.query(
      'UPDATE sessions SET is_active = false WHERE session_token = $1',
      [sessionToken]
    );

    // Audit log
    await pgPool.query(
      `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
       VALUES ($1, 'admin_terminate_session', $2, $3, $4)`,
      [req.user.userId, req.ip, req.get('user-agent'), JSON.stringify({ session_token: sessionToken })]
    );

    res.json({ message: '세션이 종료되었습니다.' });
  } catch (error) {
    console.error('Terminate session error:', error);
    res.status(500).json({ error: '세션 종료 실패' });
  }
});

// 통계 대시보드
router.get('/stats/dashboard', authenticateToken, requireAdmin, async (req, res) => {
  const pgPool = req.app.locals.pgPool;

  try {
    // 전체 사용자 수
    const totalUsers = await pgPool.query('SELECT COUNT(*) FROM users WHERE is_active = true');

    // 활성 세션 수
    const activeSessions = await pgPool.query(
      'SELECT COUNT(*) FROM sessions WHERE is_active = true AND expires_at > NOW()'
    );

    // 최근 24시간 로그인 수
    const recentLogins = await pgPool.query(
      `SELECT COUNT(*) FROM audit_logs 
       WHERE action = 'login_success' AND created_at > NOW() - INTERVAL '24 hours'`
    );

    // 구독 플랜별 사용자 수
    const subscriptionStats = await pgPool.query(
      `SELECT subscription_plan, COUNT(*) as count 
       FROM users 
       WHERE is_active = true 
       GROUP BY subscription_plan`
    );

    res.json({
      total_users: parseInt(totalUsers.rows[0].count),
      active_sessions: parseInt(activeSessions.rows[0].count),
      recent_logins_24h: parseInt(recentLogins.rows[0].count),
      subscription_distribution: subscriptionStats.rows
    });
  } catch (error) {
    console.error('Get dashboard stats error:', error);
    res.status(500).json({ error: '통계 조회 실패' });
  }
});

module.exports = router;
