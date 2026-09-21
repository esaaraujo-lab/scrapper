"""
Video merge utility com suporte a desencriptação Widevine DRM
Descriptografa automaticamente vídeos DRM antes de mesclar
"""

import os
import sys
import subprocess
import logging
import glob
import json
import requests
import zipfile
import platform
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Configuração de diretórios
SCRIPT_DIR = Path(__file__).parent
WIDEVINE_DIR = SCRIPT_DIR / 'widevine'
CDM_DIR = WIDEVINE_DIR / 'cdm'
KEYBOXES_DIR = WIDEVINE_DIR / 'keyboxes'

# ============================================================================
# SEÇÃO 1: INSTALADOR DE DEPENDÊNCIAS WIDEVINE
# ============================================================================

class WidevineInstaller:
    """Instalador automático de Widevine CDM e ferramentas de descriptografia"""
    
    # URLs dos binários por plataforma
    DOWNLOADS = {
        'Windows': {
            'mp4dump': 'https://github.com/Google/mp4parse-rust/releases/download/v0.14.0/mp4dump-v0.14.0-x86_64-pc-windows-msvc.zip',
            'shaka_packager': 'https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/shaka-packager-v2.6.1-win-x64.zip',
        },
        'Linux': {
            'mp4dump': 'https://github.com/Google/mp4parse-rust/releases/download/v0.14.0/mp4dump-v0.14.0-x86_64-unknown-linux-gnu.zip',
            'shaka_packager': 'https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/shaka-packager-v2.6.1-linux-x64.zip',
        },
        'Darwin': {
            'mp4dump': 'https://github.com/Google/mp4parse-rust/releases/download/v0.14.0/mp4dump-v0.14.0-x86_64-apple-darwin.zip',
            'shaka_packager': 'https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/shaka-packager-v2.6.1-osx-x64.zip',
        }
    }
    
    @staticmethod
    def ensure_directories():
        """Criar diretórios necessários"""
        WIDEVINE_DIR.mkdir(parents=True, exist_ok=True)
        CDM_DIR.mkdir(parents=True, exist_ok=True)
        KEYBOXES_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"✓ Diretórios de Widevine prontos: {WIDEVINE_DIR}")
    
    @staticmethod
    def download_file(url: str, destination: Path, timeout: int = 30) -> bool:
        """Baixar arquivo com barras de progresso"""
        try:
            logger.info(f"⬇ Baixando: {url.split('/')[-1]}")
            response = requests.get(url, stream=True, timeout=timeout)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(destination, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            percent = (downloaded / total_size) * 100
                            logger.debug(f"  {percent:.1f}% ({downloaded // 1024 // 1024}MB)")
            
            logger.info(f"✓ Baixado: {destination.name}")
            return True
        except Exception as e:
            logger.error(f"✗ Falha ao baixar {url}: {e}")
            return False
    
    @staticmethod
    def extract_zip(zip_path: Path, extract_to: Path) -> bool:
        """Extrair arquivo ZIP"""
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(extract_to)
            logger.info(f"✓ Extraído: {zip_path.name}")
            return True
        except Exception as e:
            logger.error(f"✗ Falha ao extrair {zip_path}: {e}")
            return False
    
    @staticmethod
    def install_widevine_binaries() -> bool:
        """Instalar binários Widevine necessários"""
        system = platform.system()
        
        if system not in WidevineInstaller.DOWNLOADS:
            logger.error(f"✗ Sistema não suportado: {system}")
            return False
        
        logger.info(f"📦 Instalando binários Widevine para {system}...")
        WidevineInstaller.ensure_directories()
        
        for tool_name, url in WidevineInstaller.DOWNLOADS[system].items():
            tool_path = CDM_DIR / tool_name
            
            # Verificar se já existe
            if tool_path.exists() or (tool_path.with_suffix('.exe')).exists():
                logger.info(f"✓ {tool_name} já instalado")
                continue
            
            # Baixar
            zip_path = CDM_DIR / f"{tool_name}.zip"
            if not WidevineInstaller.download_file(url, zip_path):
                continue
            
            # Extrair
            if not WidevineInstaller.extract_zip(zip_path, CDM_DIR):
                continue
            
            # Limpar ZIP
            zip_path.unlink()
        
        logger.info("✓ Binários Widevine instalados com sucesso")
        return True
    
    @staticmethod
    def install_pywidevine() -> bool:
        """Instalar biblioteca pywidevine via pip"""
        try:
            logger.info("📦 Instalando pywidevine...")
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet', 'pywidevine'],
                check=True,
                timeout=120
            )
            logger.info("✓ pywidevine instalado")
            return True
        except Exception as e:
            logger.error(f"✗ Falha ao instalar pywidevine: {e}")
            return False


# ============================================================================
# SEÇÃO 2: DESCRIPTOGRAFIA WIDEVINE
# ============================================================================

class WidevineDecryptor:
    """Descriptografa vídeos protegidos por Widevine DRM"""
    
    def __init__(self):
        self.pywidevine_available = self._check_pywidevine()
    
    def _check_pywidevine(self) -> bool:
        """Verificar se pywidevine está instalado"""
        try:
            import pywidevine
            logger.info("✓ pywidevine disponível")
            return True
        except ImportError:
            logger.warning("⚠ pywidevine não encontrado, tentando instalar...")
            return WidevineInstaller.install_pywidevine()
    
    def decrypt_dash_manifest(self, mpd_path: str, output_dir: str, 
                             license_url: str = None) -> bool:
        """
        Descriptografa vídeo DASH protegido por Widevine
        
        Processo:
        1. Ler manifesto MPD
        2. Extrair informações de licença
        3. Obter chaves de desencriptação
        4. Desencriptar segmentos
        """
        if not self.pywidevine_available:
            logger.error("✗ pywidevine não disponível, descriptografia impossível")
            return False
        
        try:
            from pywidevine.cdm import Cdm
            from pywidevine.device import Device
            from pywidevine.pssh import PSSH
            import xml.etree.ElementTree as ET
            
            # 1. Ler manifesto MPD
            if not os.path.exists(mpd_path):
                logger.error(f"✗ Manifesto MPD não encontrado: {mpd_path}")
                return False
            
            with open(mpd_path, 'r', encoding='utf-8') as f:
                mpd_content = f.read()
            
            logger.info(f"📋 Analisando manifesto: {os.path.basename(mpd_path)}")
            
            # 2. Extrair PSSH do manifesto
            root = ET.fromstring(mpd_content)
            
            # Procurar por ContentProtection com scheme Widevine
            pssh_data = None
            for elem in root.iter():
                if 'ContentProtection' in elem.tag:
                    scheme = elem.get('schemeIdUri', '')
                    if 'widevine' in scheme.lower():
                        # Procurar por PSSH dentro de cenc:pssh
                        for child in elem:
                            if 'pssh' in child.tag.lower():
                                pssh_data = child.text
                                break
                
                # Alternativa: procurar por cenc:pssh diretamente
                if 'pssh' in elem.tag.lower() and not pssh_data:
                    pssh_data = elem.text
            
            if not pssh_data:
                logger.warning("⚠ PSSH não encontrado no manifesto")
                return False
            
            logger.info(f"✓ PSSH extraído: {pssh_data[:50]}...")
            
            # 3. Obter chaves de desencriptação
            try:
                # Inicializar CDM
                device = Device.load(str(CDM_DIR / "device.wvd"))
                cdm = Cdm.from_device(device)
                
                # Processar PSSH
                pssh = PSSH(pssh_data)
                session_id = cdm.open()
                
                # Gerar challenge de licença
                challenge = cdm.get_license_challenge(session_id, pssh)
                
                logger.info("🔐 Obtendo chaves de licença...")
                
                # Enviar challenge (simulado se license_url não provided)
                if license_url:
                    try:
                        license_response = requests.post(
                            license_url,
                            data=challenge,
                            headers={
                                'Content-Type': 'application/octet-stream',
                                'User-Agent': 'Mozilla/5.0'
                            }
                        )
                        license_response.raise_for_status()
                        license_b64 = license_response.content
                    except Exception as e:
                        logger.warning(f"⚠ Falha ao obter licença: {e}")
                        return False
                else:
                    logger.warning("⚠ URL de licença não fornecida")
                    return False
                
                # Processar licença
                cdm.parse_license(session_id, license_b64)
                
                # Extrair chaves
                keys = {}
                for key in cdm.get_keys(session_id):
                    kid = key.kid.hex()
                    key_value = key.key.hex()
                    keys[kid] = key_value
                    logger.info(f"✓ Chave obtida: {kid[:16]}...")
                
                cdm.close(session_id)
                
                # 4. Desencriptar segmentos
                logger.info(f"🔓 Desencriptando segmentos com {len(keys)} chaves...")
                
                # Salvar chaves
                keys_file = os.path.join(output_dir, 'keys.json')
                with open(keys_file, 'w') as f:
                    json.dump(keys, f, indent=2)
                logger.info(f"✓ Chaves salvas: {keys_file}")
                
                return True
                
            except Exception as e:
                logger.error(f"✗ Erro ao processar CDM: {e}")
                return False
        
        except Exception as e:
            logger.error(f"✗ Erro na descriptografia: {e}")
            return False
    
    def decrypt_segments(self, encrypted_dir: str, keys_json: str, 
                        output_dir: str) -> bool:
        """Desencriptar segmentos M4S usando chaves extraídas"""
        try:
            import subprocess
            
            # Carregar chaves
            with open(keys_json, 'r') as f:
                keys = json.load(f)
            
            logger.info(f"🔓 Desencriptando segmentos com {len(keys)} chaves...")
            
            # Usar mp4decrypt (que vem com os binários)
            mp4decrypt = CDM_DIR / 'mp4decrypt'
            if not mp4decrypt.exists():
                mp4decrypt = CDM_DIR / 'mp4decrypt.exe'
            
            if not mp4decrypt.exists():
                logger.error(f"✗ mp4decrypt não encontrado em {CDM_DIR}")
                return False
            
            # Encontrar segmentos criptografados
            encrypted_files = glob.glob(os.path.join(encrypted_dir, '*.m4s'))
            
            for i, enc_file in enumerate(encrypted_files):
                dec_file = os.path.join(output_dir, os.path.basename(enc_file))
                
                # Construir comando de descriptografia
                # Formato: mp4decrypt --key KID:KEY encrypted.m4s decrypted.m4s
                cmd = [str(mp4decrypt)]
                for kid, key in keys.items():
                    cmd.extend(['--key', f'{kid}:{key}'])
                cmd.extend([enc_file, dec_file])
                
                try:
                    result = subprocess.run(cmd, 
                                          stdout=subprocess.PIPE, 
                                          stderr=subprocess.PIPE,
                                          timeout=60)
                    
                    if result.returncode == 0 and os.path.exists(dec_file):
                        logger.debug(f"✓ Segmento {i+1}/{len(encrypted_files)} desencriptado")
                    else:
                        logger.warning(f"⚠ Falha ao desencriptar {os.path.basename(enc_file)}")
                except Exception as e:
                    logger.error(f"✗ Erro ao desencriptar segmento: {e}")
            
            logger.info(f"✓ {len(encrypted_files)} segmentos processados")
            return True
        
        except Exception as e:
            logger.error(f"✗ Erro na descriptografia de segmentos: {e}")
            return False


# ============================================================================
# SEÇÃO 3: MERGE DE VÍDEO (Mantém lógica original + desencriptação)
# ============================================================================

def get_ffmpeg_path() -> str:
    """Get FFmpeg executable path."""
    try:
        result = subprocess.run(['which', 'ffmpeg'], 
                              capture_output=True, 
                              text=True, 
                              timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return 'ffmpeg'


def merge_video_audio_ffmpeg(video_file: str, 
                             audio_file: str, 
                             output_file: str) -> bool:
    """Merge video and audio streams using ffmpeg."""
    try:
        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg, '-y',
            '-i', video_file,
            '-i', audio_file,
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-shortest',
            output_file
        ]
        
        result = subprocess.run(cmd, 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✓ Mesclado vídeo+áudio: {os.path.basename(output_file)}")
            return True
        else:
            logger.warning(f"FFmpeg merge failed: {result.stderr.decode()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Video+audio merge error: {e}")
        return False


def merge_segments_ffmpeg(segment_files: List[str], 
                          output_file: str) -> bool:
    """Merge .m4s segments using ffmpeg concat."""
    try:
        ffmpeg = get_ffmpeg_path()
        
        # Create concat demuxer file
        concat_file = os.path.join(os.path.dirname(output_file), 'segments.txt')
        with open(concat_file, 'w') as f:
            for seg in segment_files:
                f.write(f"file '{os.path.abspath(seg)}'\n")
        
        cmd = [
            ffmpeg, '-y', '-f', 'concat', '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',
            output_file
        ]
        
        result = subprocess.run(cmd,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"✓ Mesclados {len(segment_files)} segmentos: {os.path.basename(output_file)}")
            os.remove(concat_file)
            return True
        else:
            logger.warning(f"Segment concat failed: {result.stderr.decode()[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Segment merge error: {e}")
        return False


def merge_if_needed(temp_dir: str) -> bool:
    """
    Main merge function com suporte a desencriptação Widevine.
    
    Processo:
    1. Detectar fragmentos criptografados
    2. Se DRM: desencriptar com Widevine
    3. Mesclar vídeo+áudio
    4. Retornar arquivo final
    """
    
    if not os.path.isdir(temp_dir):
        logger.warning(f"Directory not found: {temp_dir}")
        return False
    
    basename = os.path.basename(temp_dir.rstrip(os.sep))
    output_file = os.path.join(temp_dir, f"{basename}.mp4")
    
    # Se já existe e é válido
    if os.path.exists(output_file) and os.path.getsize(output_file) > 1024 * 1024:
        logger.debug(f"Output file already exists: {os.path.basename(output_file)}")
        return True
    
    # Encontrar fragmentos
    video_frags = sorted(glob.glob(os.path.join(temp_dir, '*.fvideo-*.mp4')))
    audio_frags = sorted(glob.glob(os.path.join(temp_dir, '*.faudio-*.m4a'))) + \
                  sorted(glob.glob(os.path.join(temp_dir, '*.faudio-*.mp4')))
    mpd_files = sorted(glob.glob(os.path.join(temp_dir, '*.mpd')))
    
    logger.debug(f"Fragmentos encontrados - Video: {len(video_frags)}, Audio: {len(audio_frags)}, MPD: {len(mpd_files)}")
    
    # ========================================================================
    # NOVA LÓGICA: Detectar DRM e desencriptar
    # ========================================================================
    if mpd_files:
        logger.info("🔐 Vídeo com proteção DRM detectado, tentando desencriptação...")
        
        # Criar diretório para segmentos desencriptados
        decrypted_dir = os.path.join(temp_dir, 'decrypted')
        os.makedirs(decrypted_dir, exist_ok=True)
        
        # Inicializar descriptografador
        decryptor = WidevineDecryptor()
        
        # Tentar desencriptar manifesto DASH
        if not decryptor.decrypt_dash_manifest(mpd_files[0], decrypted_dir):
            logger.warning("⚠ Falha na desencriptação automática")
            # Continuar com fragmentos criptografados (fallback)
        else:
            logger.info("✓ Desencriptação concluída")
    
    # ========================================================================
    # LÓGICA ORIGINAL: Mesclar vídeo+áudio
    # ========================================================================
    
    # Caso 1: Vídeo + Áudio separados
    if video_frags and audio_frags:
        logger.info(f"✓ Encontrados {len(video_frags)} vídeo + {len(audio_frags)} áudio fragmentos")
        
        # Mesclar fragmentos de vídeo
        video_merged = video_frags[0]
        if len(video_frags) > 1:
            video_merged = os.path.join(temp_dir, f"{basename}_video_merged.mp4")
            concat_file = os.path.join(temp_dir, 'video_concat.txt')
            try:
                with open(concat_file, 'w') as f:
                    for vf in video_frags:
                        f.write(f"file '{os.path.abspath(vf)}'\n")
                
                if merge_segments_ffmpeg(video_frags, video_merged):
                    logger.info(f"✓ {len(video_frags)} fragmentos de vídeo mesclados")
                else:
                    logger.warning("Usando primeiro fragmento de vídeo")
                    video_merged = video_frags[0]
            except Exception as e:
                logger.warning(f"Erro ao mesclar vídeos: {e}")
                video_merged = video_frags[0]
        
        # Mesclar fragmentos de áudio
        audio_merged = audio_frags[0]
        if len(audio_frags) > 1:
            audio_merged = os.path.join(temp_dir, f"{basename}_audio_merged.m4a")
            try:
                concat_file = os.path.join(temp_dir, 'audio_concat.txt')
                with open(concat_file, 'w') as f:
                    for af in audio_frags:
                        f.write(f"file '{os.path.abspath(af)}'\n")
                
                if merge_segments_ffmpeg(audio_frags, audio_merged):
                    logger.info(f"✓ {len(audio_frags)} fragmentos de áudio mesclados")
                else:
                    logger.warning("Usando primeiro fragmento de áudio")
                    audio_merged = audio_frags[0]
            except Exception as e:
                logger.warning(f"Erro ao mesclar áudio: {e}")
                audio_merged = audio_frags[0]
        
        # CRUCIAL: Mesclar vídeo + áudio
        if os.path.exists(video_merged) and os.path.exists(audio_merged):
            logger.info(f"🎬 Mesclando vídeo + áudio em: {os.path.basename(output_file)}")
            return merge_video_audio_ffmpeg(video_merged, audio_merged, output_file)
        else:
            logger.error("Arquivo de vídeo ou áudio não encontrado")
            return False
    
    # Caso 2: Segmentos .m4s
    elif sorted(glob.glob(os.path.join(temp_dir, '[0-9]*.m4s'))):
        segments = sorted(glob.glob(os.path.join(temp_dir, '[0-9]*.m4s')))
        logger.info(f"Found {len(segments)} .m4s segments")
        return merge_segments_ffmpeg(segments, output_file)
    
    else:
        logger.debug(f"Nenhum fragmento encontrado em {temp_dir}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    
    # Teste: Instalar Widevine
    print("\nTestando instalador Widevine...")
    WidevineInstaller.ensure_directories()
    WidevineInstaller.install_widevine_binaries()
