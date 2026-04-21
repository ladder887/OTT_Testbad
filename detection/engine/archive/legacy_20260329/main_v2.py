"""
OTT CDN 리칭(계정 공유) 탐지 시스템 - Detection Engine V2
개선사항:
- 상세 로그 수집 (토큰, 사용자, IP, 컨텐츠)
- 개선된 지식 그래프 생성
- 리칭 패턴 통계
"""
import os
import time
import logging
import schedule
from datetime import datetime
from data_collector_v2 import DataCollectorV2
from graph_builder_v2 import GraphBuilderV2

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 환경 변수
LOOKBACK_MINUTES = int(os.getenv('LOOKBACK_MINUTES', '1440'))  # 24시간
UPDATE_INTERVAL = int(os.getenv('UPDATE_INTERVAL', '300'))  # 5분

class DetectionEngine:
    def __init__(self):
        self.collector = DataCollectorV2()
        self.graph_builder = GraphBuilderV2()
        self.cycle_count = 0
    
    def run_cycle(self):
        """단일 사이클 실행"""
        self.cycle_count += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"Cycle #{self.cycle_count} Started")
        logger.info(f"{'='*60}")
        
        try:
            # 1. 로그 수집
            logger.info("Step 1: Collecting logs from Elasticsearch...")
            logs = self.collector.collect_logs(lookback_minutes=LOOKBACK_MINUTES)
            
            if not logs:
                logger.warning("No logs collected. Skipping this cycle.")
                return
            
            # 로그 통계
            log_stats = self.collector.get_statistics(logs)
            logger.info(f"Log Statistics:")
            logger.info(f"  - Total Requests: {log_stats['total']}")
            logger.info(f"  - Unique IPs: {log_stats['unique_ips']}")
            logger.info(f"  - Unique Users: {log_stats['unique_users']}")
            logger.info(f"  - Unique Tokens: {log_stats['unique_tokens']}")
            logger.info(f"  - Unique Contents: {log_stats['unique_contents']}")
            logger.info(f"  - Total Data: {log_stats['total_bytes_mb']} MB")
            
            # 2. 지식 그래프 생성
            logger.info("\nStep 2: Building knowledge graph...")
            result = self.graph_builder.build_graph(logs)
            
            if result['success']:
                logger.info("✓ Knowledge graph built successfully")
                logger.info(f"  - Processed: {result['processed']} logs")
                logger.info(f"  - Skipped: {result['skipped']} logs")
                
                # 그래프 통계
                stats = result['statistics']
                logger.info(f"\nGraph Statistics:")
                logger.info(f"  Nodes:")
                for node_type, count in stats['nodes'].items():
                    logger.info(f"    - {node_type}: {count}")
                logger.info(f"  Relationships:")
                for rel_type, count in stats['relationships'].items():
                    logger.info(f"    - {rel_type}: {count}")
                logger.info(f"  Total Nodes: {stats['total_nodes']}")
                logger.info(f"  Total Relationships: {stats['total_relationships']}")
                
                # 리칭 의심 패턴
                if stats['suspicious_tokens'] > 0:
                    logger.warning(f"\n⚠️  Suspicious Activity Detected!")
                    logger.warning(f"  - {stats['suspicious_tokens']} tokens used by multiple IPs")
                
            else:
                logger.error(f"✗ Failed to build graph: {result['message']}")
            
        except Exception as e:
            logger.error(f"Error in cycle: {e}", exc_info=True)
        
        logger.info(f"\nCycle #{self.cycle_count} Completed")
        logger.info(f"Next cycle in {UPDATE_INTERVAL} seconds\n")
    
    def start(self):
        """엔진 시작"""
        logger.info("="*60)
        logger.info("CDN Leeching Detection Engine V2 Started")
        logger.info("="*60)
        logger.info(f"Configuration:")
        logger.info(f"  - Lookback Window: {LOOKBACK_MINUTES} minutes")
        logger.info(f"  - Update Interval: {UPDATE_INTERVAL} seconds")
        logger.info(f"  - Elasticsearch: {os.getenv('ELASTICSEARCH_HOST', 'elasticsearch:9200')}")
        logger.info(f"  - Neo4j: {os.getenv('NEO4J_URI', 'bolt://neo4j:7687')}")
        logger.info("="*60 + "\n")
        
        # 첫 사이클 즉시 실행
        self.run_cycle()
        
        # 스케줄링
        schedule.every(UPDATE_INTERVAL).seconds.do(self.run_cycle)
        
        # 메인 루프
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("\nShutting down...")
                self.graph_builder.close()
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(10)

if __name__ == "__main__":
    engine = DetectionEngine()
    engine.start()
