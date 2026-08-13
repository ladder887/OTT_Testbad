-- OTT Platform Database Schema

-- Users Table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(200),
    profile_image VARCHAR(500),
    subscription_plan VARCHAR(50) DEFAULT 'free',
    is_active BOOLEAN DEFAULT true,
    is_verified BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP
);

-- Sessions Table
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    session_token VARCHAR(500) UNIQUE NOT NULL,
    refresh_token VARCHAR(500),
    ip_address VARCHAR(50),
    user_agent TEXT,
    device_type VARCHAR(50),
    is_active BOOLEAN DEFAULT true,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Watch History Table
CREATE TABLE IF NOT EXISTS watch_history (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    content_id VARCHAR(100) NOT NULL,
    session_token VARCHAR(500),
    label VARCHAR(100) DEFAULT 'normal',
    watch_duration INTEGER, -- seconds
    total_duration INTEGER,
    progress_percent DECIMAL(5,2),
    completed BOOLEAN DEFAULT false,
    ip_address VARCHAR(50),
    referer VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User Favorites Table
CREATE TABLE IF NOT EXISTS favorites (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    content_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, content_id)
);

-- Managed Contents Table
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
    rating VARCHAR(20) DEFAULT '전체',
    genre TEXT[] DEFAULT '{}',
    category VARCHAR(100) DEFAULT '콘텐츠',
    content_type VARCHAR(20) DEFAULT 'vod',
    featured BOOLEAN DEFAULT false,
    available_resolutions TEXT[] DEFAULT ARRAY['1080p','720p'],
    source_path VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit Logs Table (보안 로그)
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    ip_address VARCHAR(50),
    user_agent TEXT,
    details JSONB,
    success BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for Performance
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_sessions_token ON sessions(session_token);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_active ON sessions(is_active, expires_at);
CREATE INDEX idx_watch_history_user ON watch_history(user_id);
CREATE INDEX idx_watch_history_content ON watch_history(content_id);
CREATE UNIQUE INDEX idx_watch_history_user_content ON watch_history(user_id, content_id);
CREATE INDEX idx_contents_content_type ON contents(content_type);
CREATE INDEX idx_contents_category ON contents(category);
CREATE INDEX idx_audit_logs_user ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_action ON audit_logs(action);

-- Sample Data
-- Passwords are injected by the PostgreSQL container during first initialization.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\getenv ott_admin_password OTT_ADMIN_PASSWORD
\getenv ott_test_password OTT_TEST_PASSWORD

INSERT INTO users (email, username, password_hash, full_name, subscription_plan)
VALUES
('admin@ott.com', 'admin', crypt(:'ott_admin_password', gen_salt('bf')), 'Admin User', 'premium'),
('user1@test.com', 'testuser1', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 1', 'standard'),
('user2@test.com', 'testuser2', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 2', 'standard'),
('user3@test.com', 'testuser3', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 3', 'standard'),
('user4@test.com', 'testuser4', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 4', 'standard'),
('user5@test.com', 'testuser5', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 5', 'standard'),
('user6@test.com', 'testuser6', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 6', 'standard'),
('user7@test.com', 'testuser7', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 7', 'standard'),
('user8@test.com', 'testuser8', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 8', 'standard'),
('user9@test.com', 'testuser9', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 9', 'standard'),
('user10@test.com', 'testuser10', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 10', 'standard'),
('user11@test.com', 'testuser11', crypt(:'ott_test_password', gen_salt('bf')), 'Normal User 11', 'standard')
ON CONFLICT DO NOTHING;

-- TNSM 확장 실험용 logical user 계정.
-- user1~user11은 위 seed를 유지하고, user12~user100은 동일 패턴으로 보강한다.
INSERT INTO users (email, username, password_hash, full_name, subscription_plan, is_active, is_verified)
SELECT
  format('user%s@test.com', n),
  format('testuser%s', n),
  crypt(:'ott_test_password', gen_salt('bf')),
  format('Logical User %s', n),
  'standard',
  true,
  true
FROM generate_series(12, 100) AS n
ON CONFLICT (email) DO UPDATE SET
  username = EXCLUDED.username,
  password_hash = EXCLUDED.password_hash,
  full_name = EXCLUDED.full_name,
  subscription_plan = EXCLUDED.subscription_plan,
  is_active = true,
  is_verified = true,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO contents (content_id, hls_path, title, description, thumbnail, backdrop, duration, duration_sec, year, rating, genre, category, content_type, featured, available_resolutions)
VALUES
('movie_001', 'cat1', '지구달이1', '지구달이 시리즈 1편', '/thumbnails/cat1.jpg', '/thumbnails/cat1_backdrop.jpg', '3분 29초', 209, 2025, '전체', ARRAY['영상','시리즈'], '콘텐츠', 'vod', true, ARRAY['1080p','720p']),
('movie_002', 'cat2', '지구달이2', '지구달이 시리즈 2편', '/thumbnails/cat2.jpg', '/thumbnails/cat2_backdrop.jpg', '3분 15초', 195, 2025, '전체', ARRAY['영상','시리즈'], '콘텐츠', 'vod', false, ARRAY['1080p','720p']),
('movie_003', 'cat3', '지구달이3', '지구달이 시리즈 3편', '/thumbnails/cat3.jpg', '/thumbnails/cat3_backdrop.jpg', '2분 45초', 165, 2025, '전체', ARRAY['영상','시리즈'], '콘텐츠', 'vod', false, ARRAY['1080p','720p']),
('movie_004', 'cat4', '지구달이4', '지구달이 시리즈 4편', '/thumbnails/cat4.jpg', '/thumbnails/cat4_backdrop.jpg', '4분 10초', 250, 2025, '전체', ARRAY['영상','시리즈'], '콘텐츠', 'vod', false, ARRAY['1080p','720p']),
('live_001', 'live_001', '라이브 채널 1', '실증랩 라이브 채널 1', '/thumbnails/live_001.jpg', '/thumbnails/live_001_backdrop.jpg', 'LIVE', NULL, 2026, '전체', ARRAY['라이브','스포츠'], '라이브', 'live', false, ARRAY['1080p','720p']),
('live_002', 'live_002', '라이브 채널 2', '실증랩 라이브 채널 2', '/thumbnails/live_002.jpg', '/thumbnails/live_002_backdrop.jpg', 'LIVE', NULL, 2026, '전체', ARRAY['라이브','이벤트'], '라이브', 'live', false, ARRAY['1080p','720p'])
ON CONFLICT (content_id) DO NOTHING;
