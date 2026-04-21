"""
Data Collector - Elasticsearch에서 로그 데이터 수집
"""

import logging
from datetime import datetime
from elasticsearch import Elasticsearch
from typing import List, Dict

logger = logging.getLogger(__name__)


class DataCollector:
    """Elasticsearch에서 CDN 로그 수집"""
    
    def __init__(self, es_url: str):
        self.es_url = es_url
        # Elasticsearch 8.x 호환
        self.client = Elasticsearch(
            [es_url],
            request_timeout=30,
            max_retries=3,
            retry_on_timeout=True
        )
        logger.info(f"Connected to Elasticsearch: {es_url}")
    
    def fetch_logs(self, start_time: datetime, end_time: datetime, size: int = 10000) -> List[Dict]:
        """지정된 시간 범위의 로그 가져오기"""
        
        try:
            # Elasticsearch 8.x 새로운 API 방식
            response = self.client.search(
                index="scrubber-nginx-*",
                size=size,
                sort=[{"@timestamp": {"order": "desc"}}],
                query={
                    "range": {
                        "@timestamp": {
                            "gte": start_time.isoformat(),
                            "lte": end_time.isoformat()
                        }
                    }
                }
            )
            
            logs = [hit['_source'] for hit in response['hits']['hits']]
            logger.info(f"Fetched {len(logs)} logs from {start_time} to {end_time}")
            return logs
            
        except Exception as e:
            logger.error(f"Error fetching logs: {e}")
            return []
    
    def extract_sessions(self, logs: List[Dict]) -> List[Dict]:
        """로그에서 세션 정보 추출"""
        sessions_dict = {}
        
        for log in logs:
            token = log.get('http_x_session_token')
            if not token:
                continue
            
            if token not in sessions_dict:
                sessions_dict[token] = {
                    'token': token,
                    'ips': set(),
                    'referers': set(),
                    'content_ids': set(),
                    'user_agents': set(),
                    'requests': [],
                    'total_bytes': 0,
                    'first_seen': log.get('timestamp'),
                    'last_seen': log.get('timestamp')
                }
            
            session = sessions_dict[token]
            session['ips'].add(log.get('remote_addr'))
            
            referer = log.get('http_referer')
            if referer:
                session['referers'].add(referer)
            
            content_id = log.get('http_x_content_id')
            if content_id:
                session['content_ids'].add(content_id)
            
            user_agent = log.get('http_user_agent')
            if user_agent:
                session['user_agents'].add(user_agent)
            
            session['requests'].append({
                'timestamp': log.get('timestamp'),
                'uri': log.get('request_uri'),
                'status': log.get('status'),
                'bytes': int(log.get('body_bytes_sent', 0))
            })
            
            session['total_bytes'] += int(log.get('body_bytes_sent', 0))
            session['last_seen'] = log.get('timestamp')
        
        # Set을 리스트로 변환
        sessions = []
        for session in sessions_dict.values():
            session['ips'] = list(session['ips'])
            session['referers'] = list(session['referers'])
            session['content_ids'] = list(session['content_ids'])
            session['user_agents'] = list(session['user_agents'])
            session['total_requests'] = len(session['requests'])
            sessions.append(session)
        
        return sessions
