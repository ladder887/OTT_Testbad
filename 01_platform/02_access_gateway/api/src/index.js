const express = require('express');
const cors = require('cors');
const cookieParser = require('cookie-parser');
const helmet = require('helmet');
const fs = require('fs');
const path = require('path');
const { Client } = require('@elastic/elasticsearch');
const neo4j = require('neo4j-driver');
const { Pool } = require('pg');
const { requireEnv } = require('./config/env');
const { ensureContentsSchema, listContents } = require('./services/contentService');
const { startLiveLoopTranscode } = require('./services/hlsTranscoder');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 3001;
const sourceUploadDir = process.env.UPLOAD_SOURCE_DIR || '/var/www/source';

const allowedOrigins = (process.env.CORS_ORIGIN || '')
  .split(',')
  .map((origin) => origin.trim())
  .filter(Boolean)

// Middleware
app.use(helmet());
app.use(cors({
  origin: (origin, callback) => {
    if (!origin || allowedOrigins.length === 0 || allowedOrigins.includes(origin)) {
      return callback(null, true)
    }
    return callback(new Error('Not allowed by CORS'))
  },
  credentials: true
}));
app.use(express.json());
app.use(cookieParser());

// PostgreSQL Client
const pgPool = new Pool({
  host: process.env.POSTGRES_HOST || 'postgres',
  port: process.env.POSTGRES_PORT || 5432,
  database: process.env.POSTGRES_DB || 'ott_platform',
  user: process.env.POSTGRES_USER || 'ott_admin',
  password: requireEnv('POSTGRES_PASSWORD')
});

// Make pgPool available to routes
app.locals.pgPool = pgPool;

function isEnabled(value, fallback = true) {
  if (value === undefined || value === null || value === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
}

function resolveLiveSourceFile(sourcePath) {
  const raw = String(sourcePath || '').trim();
  if (!raw) return '';

  if (raw.startsWith('/source/')) {
    return path.join(sourceUploadDir, raw.slice('/source/'.length));
  }

  if (path.isAbsolute(raw)) {
    return raw;
  }

  return path.join(sourceUploadDir, raw.replace(/^\/+/, ''));
}

async function restoreRegisteredLiveStreams() {
  if (!isEnabled(process.env.RESTORE_LIVE_STREAMS_ON_START, true)) {
    console.log('Live stream restore on start is disabled');
    return;
  }

  const liveContents = await listContents(pgPool, { type: 'live' });
  let restored = 0;

  for (const content of liveContents) {
    const sourceFilePath = resolveLiveSourceFile(content.sourcePath);
    if (!sourceFilePath) {
      console.warn(`Live stream ${content.id} has no sourcePath; skipping restore`);
      continue;
    }

    if (!fs.existsSync(sourceFilePath)) {
      console.warn(`Live stream ${content.id} source file missing: ${sourceFilePath}`);
      continue;
    }

    try {
      const result = await startLiveLoopTranscode({
        sourceFilePath,
        hlsPath: content.hlsPath,
        resolutions: content.availableResolutions || ['1080p', '720p'],
      });
      restored += 1;
      console.log(
        `Restored live stream ${content.id}: hlsPath=${result.hlsPath}, resolutions=${result.resolutions.join(',')}`
      );
    } catch (error) {
      console.error(`Failed to restore live stream ${content.id}:`, error.message);
    }
  }

  console.log(`Live stream restore complete: ${restored}/${liveContents.length}`);
}

ensureContentsSchema(pgPool)
  .then(async () => {
    console.log('Content schema is ready');
    await restoreRegisteredLiveStreams();
  })
  .catch((error) => {
    console.error('Failed to initialize content schema or restore live streams:', error.message);
  });

pgPool.query("ALTER TABLE watch_history ADD COLUMN IF NOT EXISTS label VARCHAR(100) DEFAULT 'normal'")
  .catch((error) => {
    console.error('Failed to ensure watch_history label column:', error.message);
  });

pgPool.query('CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_history_user_content ON watch_history(user_id, content_id)')
  .catch((error) => {
    console.error('Failed to ensure watch_history unique index:', error.message);
  });

// Elasticsearch Client
const esClient = new Client({
  node: process.env.ELASTICSEARCH_URL || 'http://elasticsearch:9200'
});
const ACCESS_LOG_INDEX_PATTERN = 'access-gateway-nginx-*,scrubber-nginx-*';
app.locals.esClient = esClient;

// Neo4j Driver
let neo4jDriver = null;
try {
  neo4jDriver = neo4j.driver(
    process.env.NEO4J_URI || 'bolt://neo4j:7687',
    neo4j.auth.basic(
      process.env.NEO4J_USER || 'neo4j',
      requireEnv('NEO4J_PASSWORD')
    )
  );
} catch (error) {
  console.error('Neo4j connection error:', error.message);
}

// Import routes
const authRoutes = require('./routes/auth');
const userRoutes = require('./routes/user');
const contentRoutes = require('./routes/content');
const adminRoutes = require('./routes/admin');
const playbackRoutes = require('./routes/playback');
const browseRoutes = require('./routes/browse');

// Mount routes
app.use('/api/auth', authRoutes);
app.use('/api/user', userRoutes);
app.use('/api/content', contentRoutes);
app.use('/api/admin', adminRoutes);
app.use('/api/playback', playbackRoutes);
app.use('/api/browse', browseRoutes);

// Blacklist (메모리 저장, 추후 Redis로 확장 가능)
const blacklist = {
  ips: new Set(),
  tokens: new Set(),
  referers: new Set()
};

// ==================== Routes ====================

// Health Check
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    service: 'access-api',
    elasticsearch: esClient ? 'connected' : 'disconnected',
    neo4j: neo4jDriver ? 'connected' : 'disconnected'
  });
});

// 최근 요청 로그 조회
app.get('/api/logs/recent', async (req, res) => {
  try {
    const { size = 100, from = 0 } = req.query;
    
    const result = await esClient.search({
      index: ACCESS_LOG_INDEX_PATTERN,
      body: {
        sort: [{ timestamp: { order: 'desc' } }],
        size: parseInt(size),
        from: parseInt(from)
      }
    });

    const logs = result.hits.hits.map(hit => ({
      id: hit._id,
      ...hit._source,
      blocked: isBlocked(hit._source)
    }));

    res.json({
      total: result.hits.total.value,
      logs
    });
  } catch (error) {
    console.error('Error fetching logs:', error);
    res.status(500).json({ 
      error: 'Failed to fetch logs',
      message: error.message 
    });
  }
});

// 차단 통계
app.get('/api/stats/blocked', async (req, res) => {
  try {
    const result = await esClient.search({
      index: ACCESS_LOG_INDEX_PATTERN,
      body: {
        size: 0,
        query: {
          range: {
            timestamp: {
              gte: 'now-1h'
            }
          }
        },
        aggs: {
          total_requests: {
            value_count: { field: 'remote_addr.keyword' }
          },
          blocked_requests: {
            filter: {
              term: { status: 403 }
            }
          },
          top_blocked_ips: {
            terms: {
              field: 'remote_addr.keyword',
              size: 10
            }
          }
        }
      }
    });

    res.json({
      total: result.aggregations.total_requests.value,
      blocked: result.aggregations.blocked_requests.doc_count,
      top_ips: result.aggregations.top_blocked_ips.buckets
    });
  } catch (error) {
    console.error('Error fetching stats:', error);
    res.status(500).json({ 
      error: 'Failed to fetch stats',
      message: error.message 
    });
  }
});

// 블랙리스트 조회
app.get('/api/blacklist', (req, res) => {
  res.json({
    ips: Array.from(blacklist.ips),
    tokens: Array.from(blacklist.tokens),
    referers: Array.from(blacklist.referers)
  });
});

// 블랙리스트 추가
app.post('/api/blacklist', (req, res) => {
  const { type, value } = req.body;

  if (!type || !value) {
    return res.status(400).json({ error: 'Type and value are required' });
  }

  if (!blacklist[type]) {
    return res.status(400).json({ error: 'Invalid type. Use: ips, tokens, or referers' });
  }

  blacklist[type].add(value);
  
  res.json({ 
    message: 'Added to blacklist',
    type,
    value,
    total: blacklist[type].size
  });
});

// 블랙리스트 삭제
app.delete('/api/blacklist', (req, res) => {
  const { type, value } = req.body;

  if (!type || !value) {
    return res.status(400).json({ error: 'Type and value are required' });
  }

  if (!blacklist[type]) {
    return res.status(400).json({ error: 'Invalid type' });
  }

  blacklist[type].delete(value);
  
  res.json({ 
    message: 'Removed from blacklist',
    type,
    value,
    total: blacklist[type].size
  });
});

// Reserved integration endpoint for a future policy enforcement service.
app.post('/api/policy/report', (req, res) => {
  const { blocked_ips = [], blocked_tokens = [], blocked_referers = [] } = req.body;

  blocked_ips.forEach(ip => blacklist.ips.add(ip));
  blocked_tokens.forEach(token => blacklist.tokens.add(token));
  blocked_referers.forEach(ref => blacklist.referers.add(ref));

  console.log('Policy report received:', {
    ips: blocked_ips.length,
    tokens: blocked_tokens.length,
    referers: blocked_referers.length
  });

  res.json({ 
    message: 'Policy report processed',
    blacklist_size: {
      ips: blacklist.ips.size,
      tokens: blacklist.tokens.size,
      referers: blacklist.referers.size
    }
  });
});

// 요청 검증 (Lua에서 호출 가능)
app.post('/api/check', (req, res) => {
  const { remote_addr, session_token, referer } = req.body;

  if (blacklist.ips.has(remote_addr) || 
      blacklist.tokens.has(session_token) || 
      (referer && blacklist.referers.has(referer))) {
    return res.status(403).json({ 
      allowed: false,
      reason: 'Blacklisted'
    });
  }

  res.json({ allowed: true });
});

// Neo4j 쿼리 예시 (의심 세션 조회)
app.get('/api/neo4j/suspicious-sessions', async (req, res) => {
  if (!neo4jDriver) {
    return res.status(503).json({ error: 'Neo4j not available' });
  }

  const session = neo4jDriver.session();
  
  try {
    const result = await session.run(`
      MATCH (s:Session)-[:USED_IP]->(ip:IPAddress)
      WHERE s.suspicious = true
      RETURN s, ip
      LIMIT 50
    `);

    const sessions = result.records.map(record => ({
      session: record.get('s').properties,
      ip: record.get('ip').properties
    }));

    res.json({ sessions });
  } catch (error) {
    console.error('Neo4j query error:', error);
    res.status(500).json({ error: error.message });
  } finally {
    await session.close();
  }
});

// ==================== Helper Functions ====================

function isBlocked(log) {
  return blacklist.ips.has(log.remote_addr) ||
         blacklist.tokens.has(log.http_x_session_token) ||
         (log.http_referer && blacklist.referers.has(log.http_referer));
}

// ==================== Server Start ====================

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Access API running on port ${PORT}`);
  console.log(`Elasticsearch: ${process.env.ELASTICSEARCH_URL || 'http://elasticsearch:9200'}`);
  console.log(`Neo4j: ${process.env.NEO4J_URI || 'bolt://neo4j:7687'}`);
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, closing connections...');
  if (neo4jDriver) {
    await neo4jDriver.close();
  }
  process.exit(0);
});
