"""
Detection Reporter - Scrubber API에 탐지 결과 보고
"""

import logging
import requests
from typing import Dict, List

logger = logging.getLogger(__name__)


class DetectionReporter:
    """Scrubber API에 의심스러운 엔티티 보고"""
    
    def __init__(self, scrubber_api_url: str):
        self.api_url = scrubber_api_url
        self.report_endpoint = f"{scrubber_api_url}/api/detection/report"
        logger.info(f"Reporter initialized: {self.report_endpoint}")
    
    def report_suspicious(self, suspicious_entities: Dict[str, List[str]]) -> Dict:
        """의심스러운 IP/토큰/Referer를 Scrubber에 보고"""
        payload = {
            'suspicious_ips': suspicious_entities.get('ips', []),
            'suspicious_tokens': suspicious_entities.get('tokens', []),
            'suspicious_referers': suspicious_entities.get('referers', [])
        }
        
        try:
            response = requests.post(
                self.report_endpoint,
                json=payload,
                timeout=10
            )
            
            response.raise_for_status()
            result = response.json()
            
            logger.info(f"Successfully reported suspicious entities: {result}")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to report to Scrubber API: {e}")
            return {'error': str(e)}
