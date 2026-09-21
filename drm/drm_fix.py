#!/usr/bin/env python3
"""
DRM Video Merge Diagnostic and Fix Script
==========================================

Diagnóstico e correção para falhas ao mesclar vídeos DRM protegidos por Widevine.
"""

import os
import sys
import json
import subprocess
import logging
from pathlib import Path
from typing import List, Dict

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)


class DRMDiagnostic:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.issues = []
        self.fixes = []
    
    def check_ffmpeg(self) -> bool:
        """Verify FFmpeg installation."""
        try:
            result = subprocess.run(['ffmpeg', '-version'], 
                                  capture_output=True, 
                                  timeout=5)
            if result.returncode == 0:
                logger.info("✓ FFmpeg disponível")
                return True
        except FileNotFoundError:
            self.issues.append("FFmpeg não encontrado no PATH")
            return False
    
    def check_python_packages(self) -> bool:
        """Verify required Python packages."""
        packages = ['yt_dlp', 'requests', 'beautifulsoup4']
        missing = []
        
        for pkg in packages:
            try:
                __import__(pkg.replace('-', '_'))
                logger.info(f"✓ {pkg} instalado")
            except ImportError:
                missing.append(pkg)
        
        if missing:
            self.issues.append(f"Pacotes faltando: {', '.join(missing)}")
            self.fixes.append(
                f"Instale com: pip install {' '.join(missing)}"
            )
            return False
        return True
    
    def check_fragment_directories(self) -> List[Path]:
        """Find directories with DRM fragments."""
        fragment_dirs = []
        
        if not self.base_dir.exists():
            self.issues.append(f"Diretório base não existe: {self.base_dir}")
            return fragment_dirs
        
        # Look for temp directories
        for item in self.base_dir.rglob('*'):
            if item.is_dir():
                files = list(item.glob('*.fvideo-*.mp4')) + \
                        list(item.glob('*.faudio-*.mp4')) + \
                        list(item.glob('*.m4s'))
                if files:
                    fragment_dirs.append(item)
                    logger.info(f"✓ Encontrado diretório com {len(files)} fragmentos: {item.name}")
        
        return fragment_dirs
    
    def check_drm_manifests(self) -> List[Dict]:
        """Find DRM manifest files (.mpd)."""
        manifests = []
        
        for mpd_file in self.base_dir.rglob('*.mpd'):
            try:
                size = mpd_file.stat().st_size
                manifests.append({
                    'path': str(mpd_file),
                    'size': size,
                    'name': mpd_file.name
                })
                logger.info(f"✓ Manifesto DRM encontrado: {mpd_file.name} ({size} bytes)")
            except Exception as e:
                logger.warning(f"Erro ao verificar {mpd_file}: {e}")
        
        return manifests
    
    def analyze_logs(self, log_file: str = None) -> Dict:
        """Analyze logs for DRM errors."""
        if not log_file:
            # Try to find log file
            log_candidates = list(self.base_dir.rglob('*.log'))
            if not log_candidates:
                return {}
            log_file = str(log_candidates[0])
        
        errors = {
            'drm_failures': 0,
            'widevine_detected': 0,
            'merge_failures': 0,
            'download_failures': 0
        }
        
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if 'drm' in line.lower():
                        errors['drm_failures'] += 1
                    if 'widevine' in line.lower():
                        errors['widevine_detected'] += 1
                    if 'merge' in line.lower() and 'falha' in line.lower():
                        errors['merge_failures'] += 1
                    if 'download' in line.lower() and 'falha' in line.lower():
                        errors['download_failures'] += 1
        except Exception as e:
            logger.warning(f"Erro ao ler log: {e}")
        
        return errors
    
    def generate_report(self) -> str:
        """Generate diagnostic report."""
        report = [
            "\n" + "="*70,
            "RELATÓRIO DE DIAGNÓSTICO - VÍDEOS DRM",
            "="*70 + "\n"
        ]
        
        report.append("VERIFICAÇÕES REALIZADAS:")
        self.check_ffmpeg()
        self.check_python_packages()
        
        fragment_dirs = self.check_fragment_directories()
        manifests = self.check_drm_manifests()
        
        report.append(f"\n📊 RESUMO:")
        report.append(f"   Diretórios com fragmentos: {len(fragment_dirs)}")
        report.append(f"   Manifestos DRM encontrados: {len(manifests)}")
        
        if self.issues:
            report.append(f"\n⚠️  PROBLEMAS DETECTADOS ({len(self.issues)}):")
            for issue in self.issues:
                report.append(f"   • {issue}")
        
        if self.fixes:
            report.append(f"\n🔧 AÇÕES RECOMENDADAS:")
            for fix in self.fixes:
                report.append(f"   → {fix}")
        
        report.append("\n" + "="*70 + "\n")
        return "\n".join(report)


def main():
    base_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    
    logger.info(f"Iniciando diagnóstico para: {base_dir}")
    
    diagnostic = DRMDiagnostic(base_dir)
    report = diagnostic.generate_report()
    
    print(report)
    
    # Save report
    report_file = Path(base_dir) / 'drm_diagnostic_report.txt'
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info(f"Relatório salvo em: {report_file}")


if __name__ == "__main__":
    main()
