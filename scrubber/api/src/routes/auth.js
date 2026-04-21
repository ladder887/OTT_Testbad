const express = require('express');
const bcrypt = require('bcrypt');
const jwt = require('jsonwebtoken');
const { body, validationResult } = require('express-validator');
const rateLimit = require('express-rate-limit');

const router = express.Router();
const JWT_SECRET = process.env.JWT_SECRET || 'your_jwt_secret_change_in_production';
const JWT_REFRESH_SECRET = process.env.JWT_REFRESH_SECRET || 'your_refresh_secret';

// Rate limiting
const loginLimiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 5, // 5 attempts
  message: '너무 많은 로그인 시도가 있었습니다. 15분 후에 다시 시도하세요.'
});

// 회원가입
router.post('/register',
  [
    body('email').isEmail().withMessage('유효한 이메일 주소를 입력하세요').normalizeEmail(),
    body('username').isLength({ min: 2, max: 30 }).withMessage('사용자명은 2~30자여야 합니다').trim(),
    body('password').isLength({ min: 6 }).withMessage('비밀번호는 최소 6자 이상이어야 합니다'),
    body('full_name').optional().trim()
  ],
  async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ 
        error: errors.array()[0].msg,
        errors: errors.array() 
      });
    }

    const { email, username, password, full_name } = req.body;
    const pgPool = req.app.locals.pgPool;

    try {
      // 중복 체크
      const existingUser = await pgPool.query(
        'SELECT id FROM users WHERE email = $1 OR username = $2',
        [email, username]
      );

      if (existingUser.rows.length > 0) {
        return res.status(400).json({ error: '이미 존재하는 이메일 또는 사용자명입니다.' });
      }

      // 비밀번호 해시
      const password_hash = await bcrypt.hash(password, 10);

      // 사용자 생성
      const result = await pgPool.query(
        `INSERT INTO users (email, username, password_hash, full_name, subscription_plan)
         VALUES ($1, $2, $3, $4, 'free')
         RETURNING id, email, username, full_name, subscription_plan, created_at`,
        [email, username, password_hash, full_name || username]
      );

      const user = result.rows[0];

      // Audit log
      await pgPool.query(
        `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
         VALUES ($1, 'register', $2, $3, $4)`,
        [user.id, req.ip, req.get('user-agent'), JSON.stringify({ email, username })]
      );

      res.status(201).json({
        message: '회원가입이 완료되었습니다.',
        user: {
          id: user.id,
          email: user.email,
          username: user.username,
          full_name: user.full_name,
          subscription_plan: user.subscription_plan
        }
      });

    } catch (error) {
      console.error('Registration error:', error);
      res.status(500).json({ error: '회원가입 중 오류가 발생했습니다.' });
    }
  }
);

// 로그인
router.post('/login', loginLimiter,
  [
    body('email').isEmail().normalizeEmail(),
    body('password').notEmpty()
  ],
  async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ errors: errors.array() });
    }

    const { email, password } = req.body;
    const pgPool = req.app.locals.pgPool;

    try {
      // 사용자 조회
      const result = await pgPool.query(
        'SELECT * FROM users WHERE email = $1 AND is_active = true',
        [email]
      );

      if (result.rows.length === 0) {
        return res.status(401).json({ error: '이메일 또는 비밀번호가 올바르지 않습니다.' });
      }

      const user = result.rows[0];

      // 비밀번호 검증
      const isValid = await bcrypt.compare(password, user.password_hash);
      if (!isValid) {
        // Audit log
        await pgPool.query(
          `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, success)
           VALUES ($1, 'login_failed', $2, $3, false)`,
          [user.id, req.ip, req.get('user-agent')]
        );

        return res.status(401).json({ error: '이메일 또는 비밀번호가 올바르지 않습니다.' });
      }

      // JWT 토큰 생성
      const accessToken = jwt.sign(
        { userId: user.id, email: user.email, username: user.username },
        JWT_SECRET,
        { expiresIn: '1h' }
      );

      const refreshToken = jwt.sign(
        { userId: user.id },
        JWT_REFRESH_SECRET,
        { expiresIn: '7d' }
      );

      // 세션 저장
      const sessionToken = `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
      await pgPool.query(
        `INSERT INTO sessions (user_id, session_token, refresh_token, ip_address, user_agent, expires_at)
         VALUES ($1, $2, $3, $4, $5, NOW() + INTERVAL '7 days')`,
        [user.id, sessionToken, refreshToken, req.ip, req.get('user-agent')]
      );

      // Last login 업데이트
      await pgPool.query(
        'UPDATE users SET last_login = NOW() WHERE id = $1',
        [user.id]
      );

      // Audit log
      await pgPool.query(
        `INSERT INTO audit_logs (user_id, action, ip_address, user_agent, details)
         VALUES ($1, 'login_success', $2, $3, $4)`,
        [user.id, req.ip, req.get('user-agent'), JSON.stringify({ session_token: sessionToken })]
      );

      // HTTP-only 쿠키 설정
      res.cookie('refreshToken', refreshToken, {
        httpOnly: true,
        secure: process.env.NODE_ENV === 'production',
        sameSite: 'strict',
        maxAge: 7 * 24 * 60 * 60 * 1000 // 7 days
      });

      res.cookie('sessionToken', sessionToken, {
        httpOnly: true,
        secure: process.env.NODE_ENV === 'production',
        sameSite: 'strict',
        maxAge: 7 * 24 * 60 * 60 * 1000
      });

      res.json({
        message: '로그인 성공',
        accessToken,
        sessionToken,
        user: {
          id: user.id,
          email: user.email,
          username: user.username,
          full_name: user.full_name,
          profile_image: user.profile_image,
          subscription_plan: user.subscription_plan
        }
      });

    } catch (error) {
      console.error('Login error:', error);
      res.status(500).json({ error: '로그인 중 오류가 발생했습니다.' });
    }
  }
);

// 토큰 갱신
router.post('/refresh', async (req, res) => {
  const refreshToken = req.cookies.refreshToken || req.body.refreshToken;

  if (!refreshToken) {
    return res.status(401).json({ error: 'Refresh token이 필요합니다.' });
  }

  const pgPool = req.app.locals.pgPool;

  try {
    // 토큰 검증
    const decoded = jwt.verify(refreshToken, JWT_REFRESH_SECRET);

    // 세션 확인
    const result = await pgPool.query(
      `SELECT s.*, u.email, u.username 
       FROM sessions s
       JOIN users u ON s.user_id = u.id
       WHERE s.refresh_token = $1 AND s.is_active = true AND s.expires_at > NOW()`,
      [refreshToken]
    );

    if (result.rows.length === 0) {
      return res.status(401).json({ error: '유효하지 않은 세션입니다.' });
    }

    const session = result.rows[0];

    // 새 Access Token 생성
    const accessToken = jwt.sign(
      { userId: session.user_id, email: session.email, username: session.username },
      JWT_SECRET,
      { expiresIn: '1h' }
    );

    // 세션 활동 시간 업데이트
    await pgPool.query(
      'UPDATE sessions SET last_activity = NOW() WHERE id = $1',
      [session.id]
    );

    res.json({
      accessToken,
      sessionToken: session.session_token
    });

  } catch (error) {
    console.error('Token refresh error:', error);
    res.status(401).json({ error: '토큰 갱신 실패' });
  }
});

// 로그아웃
router.post('/logout', async (req, res) => {
  const sessionToken = req.cookies.sessionToken || req.body.sessionToken;
  const pgPool = req.app.locals.pgPool;

  try {
    if (sessionToken) {
      await pgPool.query(
        'UPDATE sessions SET is_active = false WHERE session_token = $1',
        [sessionToken]
      );
    }

    res.clearCookie('refreshToken');
    res.clearCookie('sessionToken');
    res.json({ message: '로그아웃되었습니다.' });

  } catch (error) {
    console.error('Logout error:', error);
    res.status(500).json({ error: '로그아웃 중 오류가 발생했습니다.' });
  }
});

// 현재 사용자 정보
router.get('/me', authenticateToken, async (req, res) => {
  const pgPool = req.app.locals.pgPool;

  try {
    const result = await pgPool.query(
      'SELECT id, email, username, full_name, profile_image, subscription_plan, created_at FROM users WHERE id = $1',
      [req.user.userId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: '사용자를 찾을 수 없습니다.' });
    }

    res.json({ user: result.rows[0] });

  } catch (error) {
    console.error('Get user error:', error);
    res.status(500).json({ error: '사용자 정보 조회 실패' });
  }
});

// 세션 토큰 검증 API (Lua 스크립트에서 호출용)
router.post('/verify-session', async (req, res) => {
  const { session_token } = req.body;
  
  if (!session_token) {
    return res.status(400).json({ valid: false, error: 'session_token이 필요합니다.' });
  }

  const pgPool = req.app.locals.pgPool;

  try {
    const result = await pgPool.query(
      `SELECT s.*, u.id as user_id, u.email, u.username, u.subscription_plan 
       FROM sessions s
       JOIN users u ON s.user_id = u.id
       WHERE s.session_token = $1 AND s.is_active = true AND s.expires_at > NOW()`,
      [session_token]
    );

    if (result.rows.length === 0) {
      return res.json({ valid: false, reason: 'invalid_or_expired' });
    }

    const session = result.rows[0];

    // 세션 활동 시간 업데이트
    await pgPool.query(
      'UPDATE sessions SET last_activity = NOW() WHERE session_token = $1',
      [session_token]
    );

    res.json({
      valid: true,
      user: {
        id: session.user_id,
        email: session.email,
        username: session.username,
        subscription_plan: session.subscription_plan
      }
    });

  } catch (error) {
    console.error('Session verification error:', error);
    res.status(500).json({ valid: false, error: 'internal_error' });
  }
});

// 인증 미들웨어
function authenticateToken(req, res, next) {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];

  if (!token) {
    return res.status(401).json({ error: '인증 토큰이 필요합니다.' });
  }

  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err) {
      return res.status(401).json({
        error: err.name === 'TokenExpiredError' ? '인증 토큰이 만료되었습니다.' : '유효하지 않은 토큰입니다.'
      });
    }
    req.user = user;
    next();
  });
}

module.exports = router;
module.exports.authenticateToken = authenticateToken;
