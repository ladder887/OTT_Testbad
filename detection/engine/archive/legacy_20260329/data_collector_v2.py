"""
개선된 데이터 수집기 - 상세 로그 수집 및 파싱
"""
import os
from datetime import datetime, timedelta
from typing import List, Dict
import requests
from elasticsearch import Elasticsearch
import logging

logger = logging.getLogger(__name__)

class DataCollectorV2:
    def __init__(self):
        self.es_host = os.getenv('ELASTICSEARCH_HOST', 'elasticsearch:9200')
        self.client = Elasticsearch([f'http://{self.es_host}'])
        
        # Elasticsearch 연결 확인
        if not self.client.ping():
            raise ConnectionError(f"Cannot connect to Elasticsearch at {self.es_host}")
        
        logger.info(f"Connected to Elasticsearch at {self.es_host}")
    
    def collect_logs(self, lookback_minutes: int = 1440) -> List[Dict]:
        """
        Elasticsearch에서 로그 수집 (개선된 버전)
        - Query parameter에서 토큰/사용자/컨텐츠 추출
        - X-Forwarded-For에서 실제 클라이언트 IP 추출
        """
        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(minutes=lookback_minutes)
            
            # 모든 /hls 요청 수집 (비디오 스트리밍)
            response = self.client.search(
                index="scrubber-nginx-*",
                size=10000,
                sort=[{"@timestamp": {"order": "desc"}}],
                query={
                    "bool": {
                        "must": [
                            {
                                "range": {
                                    "@timestamp": {
                                        "gte": start_time.isoformat(),
                                        "lte": end_time.isoformat()
                                    }
                                }
                            },
                            {
                                "wildcard": {
                                    "request_uri": "/hls/*"
                                }
                            }
                        ],
                        "must_not": [
                            {
                                "term": {
                                    "request_method": "OPTIONS"
                                }
                            }
                        ]
                    }
                }
            )
            
            hits = response['hits']['hits']
            logger.info(f"Collected {len(hits)} log entries (/hls requests)")
            
            # 로그 파싱
            parsed_logs = []
            for hit in hits:
                parsed_log = self.parse_log_entry(hit)
                # 유효성 검증 완화 (더 많은 로그 수집)
                if parsed_log['content_id'] not in ['-', '', None] or \
                   parsed_log['token'] not in ['-', '', None] or \
                   parsed_log['username'] not in ['-', '', None]:
                    parsed_logs.append(parsed_log)
            
            logger.info(f"Parsed {len(parsed_logs)} valid log entries")
            return parsed_logs
            
        except Exception as e:
            logger.error(f"Error collecting logs: {e}")
            return []
    
    def parse_log_entry(self, log_entry: Dict) -> Dict:
        """
        로그 엔트리를 파싱하여 필요한 정보 추출 (대폭 개선)
        
        Elasticsearch 로그 구조:
        {
            "_source": {
                "@timestamp": "2025-12-02T10:30:45.000Z",
                "remote_addr": "172.18.0.1",  # Docker gateway
                "client_ip": "192.168.1.100",  # 실제 클라이언트 IP (X-Forwarded-For)
                "request_uri": "/hls/video1.mp4?content_id=1&user=testuser&token=abc123",
                "query_string": "content_id=1&user=testuser&token=abc123",
                "session_token": "abc123",  # $arg_token
                "content_id": "1",  # $arg_content_id
                "username": "testuser",  # $arg_user
                "http_referer": "http://localhost:3000/",
                "status": "200",
                "bytes_sent": "1048576",
                "request_time": "0.123"
            }
        }
        """
        source = log_entry.get('_source', {})
        
        # 1. 클라이언트 IP 추출 (우선순위: real_ip > client_real_ip > client_ip > x_forwarded_for > remote_addr)
        # X-Real-IP 헤더를 최우선으로 사용 (Scrubber Gateway에서 전달)
        client_ip = source.get('real_ip', source.get('client_real_ip', '-'))
        
        # X-Real-IP가 없으면 client_id 기반 device ID 사용
        if client_ip in ['-', '', None, '172.18.0.1']:
            client_id = source.get('client_id', '-')
            if client_id not in ['-', '', None]:
                client_ip = f"device_{client_id}"  # 디바이스 식별자를 IP처럼 사용
            else:
                # fallback: client_ip > x_forwarded_for > remote_addr
                client_ip = source.get('client_ip', source.get('x_forwarded_for', '-'))
                if client_ip in ['-', '', None]:
                    client_ip = source.get('remote_addr', 'unknown')
                # Docker gateway IP는 실제 클라이언트로 간주하지 않음
                if client_ip == '172.18.0.1':
                    client_ip = 'device_unknown'
        
        # 2. 세션 토큰 추출 (우선순위: query param > header)
        session_token = source.get('session_token', source.get('http_x_session_token', '-'))
        
        # 3. 컨텐츠 ID 추출 (우선순위: query param > header > URI 파싱)
        content_id = source.get('content_id', source.get('http_x_content_id', '-'))
        if content_id in ['-', '', None]:
            # URI에서 파일명 추출 (video1.mp4 -> video1)
            uri = source.get('request_uri', '')
            if '/hls/' in uri:
                filename = uri.split('/hls/')[-1].split('?')[0]
                if filename.endswith('.mp4'):
                    content_id = filename.replace('.mp4', '')
        
        # 4. 사용자명 추출
        username = source.get('username', '-')
        
        # 5. Referer 추출 및 도메인 파싱
        referer = source.get('http_referer', '-')
        referer_domain = self._extract_domain(referer) if referer not in ['-', '', None] else 'direct'
        
        # 6. 데이터 전송량
        bytes_sent = int(source.get('bytes_sent', source.get('body_bytes_sent', 0)) or 0)
        
        # 7. 요청 시간
        request_time = float(source.get('request_time', 0) or 0)
        
        # 8. 타임스탬프
        timestamp = source.get('@timestamp') or source.get('timestamp')
        
        # 9. Client ID 추가 추출
        client_id_raw = source.get('client_id', '-')
        
        # 10. 유효성 검증 (완화)
        is_valid = (
            content_id not in ['-', '', None] or
            session_token not in ['-', '', None] or
            username not in ['-', '', None]
        )
        
        return {
            'timestamp': timestamp,
            'ip': client_ip,
            'client_id': client_id_raw,
            'token': session_token,
            'username': username,
            'content_id': content_id,
            'referer': referer,
            'referer_domain': referer_domain,
            'user_agent': source.get('http_user_agent', '-'),
            'request_uri': source.get('request_uri', '-'),
            'query_string': source.get('query_string', '-'),
            'status': int(source.get('status', 0) or 0),
            'bytes': bytes_sent,
            'request_time': request_time,
            'connection': source.get('connection', '-'),
            'connection_requests': source.get('connection_requests', '-'),
            'is_valid': is_valid
        }
    
    def _extract_domain(self, url: str) -> str:
        """URL에서 도메인만 추출"""
        if not url or url == '-':
            return 'direct'
        
        # http://localhost:3000/ -> localhost:3000
        if '://' in url:
            domain = url.split('://')[1].split('/')[0]
        else:
            domain = url.split('/')[0]
        
        return domain
    
    def get_statistics(self, logs: List[Dict]) -> Dict:
        """수집된 로그 통계"""
        if not logs:
            return {'total': 0}
        
        unique_ips = set()
        unique_tokens = set()
        unique_users = set()
        unique_contents = set()
        total_bytes = 0
        
        for log in logs:
            if log['ip'] not in ['-', 'unknown', '', None, '172.18.0.1']:
                unique_ips.add(log['ip'])
            if log['token'] not in ['-', '', None]:
                unique_tokens.add(log['token'])
            if log['username'] not in ['-', '', None]:
                unique_users.add(log['username'])
            if log['content_id'] not in ['-', '', None]:
                unique_contents.add(log['content_id'])
            total_bytes += log['bytes']
        
        return {
            'total': len(logs),
            'unique_ips': len(unique_ips),
            'unique_tokens': len(unique_tokens),
            'unique_users': len(unique_users),
            'unique_contents': len(unique_contents),
            'total_bytes': total_bytes,
            'total_bytes_mb': round(total_bytes / 1024 / 1024, 2)
        }
