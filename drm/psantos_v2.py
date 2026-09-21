import os
import re
import sys
import json
import time
import glob
import logging
import shutil
import pathlib
import threading
import unicodedata
import subprocess
from urllib.parse import quote
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

print("Instalando dependências...")
dependencies = [
    'beautifulsoup4',
    'requests',
    'yt-dlp',
    'imageio-ffmpeg',
    'pywidevine',
    'DDownloader'
]

for dep in dependencies:
    try:
        install(dep)
    except Exception as e:
        print(f"Aviso ao instalar {dep}: {e}")

import requests
import urllib3
import imageio_ffmpeg
from bs4 import BeautifulSoup
import yt_dlp

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Tentar importar pywidevine (opcional)
try:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import Pssh
    WIDEVINE_AVAILABLE = True
except ImportError:
    WIDEVINE_AVAILABLE = False
    print("⚠ pywidevine não disponível. DRM automático desabilitado.")

# Configuração do FFmpeg no PATH
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_DIR = os.path.dirname(ffmpeg_path)
os.environ['PATH'] = FFMPEG_DIR + os.pathsep + os.environ.get('PATH', '')

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
BASE_DIR = pathlib.Path(r'D:/Papa Concursos 2026')
TEMP_DIR = BASE_DIR / 'temp'
CACHE_DIR = BASE_DIR
DRM_DIR = BASE_DIR / 'drm'

BASE_URL = "https://portal2025.papaconcursos.com.br"
PORTAL = f"{BASE_URL}/portal"

# Chave global no formato KID:KEY (se vazio, tenta obter via KeyOS)
DEFAULT_DRM_KEY = ""

# Configurações KeyOS
KEYOS_LICENSE_URL = "https://playready.keyos.com/api/v4/getLicense"
KEYOS_TOKEN = ""  # Seu token XML em Base64 do KeyOS
KEYOS_WVD_PATH = "device.wvd"  # Caminho para o device.wvd do Widevine L3
KEYOS_ENABLED = True  # Ativar/desativar obtenção automática de chaves

# Garante que os diretórios existam
for d in [BASE_DIR, TEMP_DIR, CACHE_DIR, DRM_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

BATCH_DELAY       = 0.3
RETRY_ATTEMPTS    = 3
CHUNK_SIZE        = 1024 * 256
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 1
MAX_PDF_WORKERS   = 5

HEADERS = {
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'referer': f"{BASE_URL}/portal",
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# KEYOS RESOLVER - Obtenção automática de chaves DRM
# ============================================================================
class KeyosResolver:
    """Extrai PSSH do MPD e obtém chaves do servidor KeyOS automaticamente."""
    
    def __init__(self, session: requests.Session, wvd_path: str = KEYOS_WVD_PATH, keyos_token: str = KEYOS_TOKEN):
        self.session = session
        self.wvd_path = wvd_path
        self.keyos_token = keyos_token
        self.key_cache = {}  # Cache de chaves já obtidas
    
    def get_pssh_from_mpd(self, mpd_url: str) -> Optional[str]:
        """Baixa o MPD e extrai a tag PSSH."""
        try:
            resp = self.session.get(mpd_url, verify=False, timeout=TIMEOUT)
            resp.raise_for_status()
            
            # Tentar parsear como XML
            try:
                soup = BeautifulSoup(resp.content, 'xml')
                pssh_element = soup.find('cenc:pssh')
                if pssh_element:
                    return pssh_element.text.strip()
            except Exception:
                pass
            
            # Fallback: regex no texto do MPD
            match = re.search(r'<cenc:pssh[^>]*>(.*?)</cenc:pssh>', resp.text)
            if match:
                return match.group(1).strip()
            
            logger.warning(f"PSSH não encontrado no MPD: {mpd_url}")
            return None
            
        except Exception as e:
            logger.error(f"Erro ao extrair PSSH do MPD: {e}")
            return None
    
    def fetch_drm_key_automatically(self, mpd_url: str) -> Optional[str]:
        """
        Extrai PSSH do MPD, simula CDM Widevine e obtém a chave do KeyOS.
        Retorna no formato KID:KEY ou None se falhar.
        """
        if not WIDEVINE_AVAILABLE or not self.keyos_token:
            logger.warning("KeyOS desabilitado ou configuração incompleta.")
            return None
        
        # Verificar cache
        if mpd_url in self.key_cache:
            logger.info(f"Usando chave em cache para MPD")
            return self.key_cache[mpd_url]
        
        try:
            # 1. Extrair PSSH do MPD
            logger.info("Extraindo PSSH do MPD...")
            pssh_str = self.get_pssh_from_mpd(mpd_url)
            if not pssh_str:
                return None
            
            pssh = Pssh(pssh_str)
            
            # 2. Carregar dispositivo L3 e iniciar sessão CDM
            logger.info("Inicializando CDM Widevine...")
            if not os.path.exists(self.wvd_path):
                logger.error(f"device.wvd não encontrado em: {self.wvd_path}")
                return None
            
            device = Device.load(self.wvd_path)
            cdm = Cdm.from_device(device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh)
            
            # 3. Fazer requisição POST para o servidor de licença do KeyOS
            logger.info("Enviando desafio para KeyOS...")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Content-Type': 'application/octet-stream',
                'customdata': self.keyos_token
            }
            
            license_resp = self.session.post(
                KEYOS_LICENSE_URL,
                data=challenge,
                headers=headers,
                verify=False,
                timeout=TIMEOUT
            )
            license_resp.raise_for_status()
            
            # 4. Processar a licença retornada
            logger.info("Processando resposta do KeyOS...")
            cdm.parse_license(session_id, license_resp.content)
            
            keys = []
            for key in cdm.get_keys(session_id):
                if key.type == 'OPERATIONAL':
                    key_pair = f"{key.kid.hex()}:{key.key.hex()}"
                    keys.append(key_pair)
                    logger.info(f"✓ Chave obtida: {key_pair[:20]}...")
            
            cdm.close(session_id)
            
            if not keys:
                logger.error("Nenhuma chave operacional obtida.")
                return None
            
            # Cachear a chave
            self.key_cache[mpd_url] = keys[0]
            return keys[0]
            
        except Exception as e:
            logger.error(f"Erro ao obter chave KeyOS: {e}")
            return None

# ============================================================================
# HELPER DE DESCRIPTOGRAFIA DRM (SHAKA / BENTO4)
# ============================================================================
def check_binary(binary_name: str) -> bool:
    """Verifica se o binário executável está no PATH."""
    return shutil.which(binary_name) is not None

def decrypt_with_shaka(input_file: str, output_file: str, key_pair: str):
    """Descriptografa arquivo usando shaka-packager."""
    kid, key = key_pair.split(':')
    binary = 'shaka-packager' if check_binary('shaka-packager') else 'packager'
    cmd = [
        binary,
        f'in={input_file},stream=data,out={output_file}',
        '--enable_raw_key_decryption',
        f'--keys=key_id={kid}:key={key}'
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

def decrypt_with_mp4decrypt(input_file: str, output_file: str, key_pair: str):
    """Descriptografa arquivo usando mp4decrypt (Bento4)."""
    cmd = [
        'mp4decrypt',
        '--key', key_pair,
        input_file,
        output_file
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

# ============================================================================
# UTILITÁRIOS
# ============================================================================
def clear_name(name: str) -> str:
    try:
        name = name.encode('latin-1').decode('utf-8')
    except Exception:
        pass
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = nfkd.encode('ASCII', 'ignore').decode('ASCII')
    cleaned = re.sub(r'[<>:"/\\|?*]', '', ascii_name).strip().rstrip('.')
    return cleaned

def create_folder(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

# ============================================================================
# COLETOR DE LINKS
# ============================================================================
class LinkCollector:
    def __init__(self, session: requests.Session):
        self.session = session

    def collect_all_links(self, course_id: str, course_name: str, force_rebuild: bool = False) -> Dict:
        cache_file = os.path.join(CACHE_DIR, f"{course_id}_links.json")
        if os.path.exists(cache_file):
            if force_rebuild:
                logger.warning(f"force_rebuild=True — descartando cache: {course_name}")
                os.remove(cache_file)
            else:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if cached:
                    logger.info(f"Carregando cache: {course_name} ({len(cached)} aulas)")
                    return cached
                logger.warning(f"Cache vazio — recoletando: {course_name}")
                os.remove(cache_file)

        logger.info(f"Coletando links: {course_name}")
        root_items = self._get_root_items(course_id, course_name)

        if not root_items:
            raise RuntimeError(f"Sessão expirada ou course_id incorreto para '{course_name}'.")

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links)

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(all_links, f, ensure_ascii=False, indent=2)

        return all_links

    def _get_root_items(self, course_id: str, course_name: str) -> Dict[str, str]:
        url = f"{PORTAL}/api/items?token={course_id}"
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        return resp.json() if resp.status_code == 200 else {}

    def _recurse(self, item_token: str, course_id: str, parent_name: str, all_links: Dict):
        url = f"{PORTAL}/api/items/{item_token}"
        try:
            resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            data = resp.json()
        except Exception:
            return

        children = data.get('children', {})
        if isinstance(children, dict):
            for child_id, child_data in children.items():
                child_name = child_data.get('name', 'unknown')
                safe_name = clear_name(child_name)
                child_token = child_data.get('token')

                if not child_token:
                    continue

                if child_data.get('type') == 'folder':
                    child_path = f"{parent_name}/{safe_name}"
                    self._recurse(child_token, course_id, child_path, all_links)
                else:
                    item_key = f"{parent_name}/{safe_name}"
                    item_links = {'video': None, 'pdf': []}
                    try:
                        item_resp = self.session.get(
                            f"{PORTAL}/api/items/{child_token}",
                            headers=HEADERS, timeout=TIMEOUT, verify=False)
                        item_data = item_resp.json()
                        item_links['video'] = item_data.get('video')
                        item_links['pdf'] = item_data.get('pdfs', [])
                    except Exception:
                        pass

                    all_links[item_key] = item_links

# ============================================================================
# DOWNLOADER PRINCIPAL
# ============================================================================
class PapaDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.lock = threading.Lock()
        self.successful_videos = 0
        self.failed_videos = 0
        self.drm_failed_videos = []
        self.successful_pdfs = 0
        self.failed_pdfs = 0
        self.keyos_resolver = KeyosResolver(self.session)

    def login(self, email: str, jsessionid: str, chave: str):
        """Autentica na plataforma."""
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.headers.update(HEADERS)
        logger.info(f"✓ Autenticado como {email}")

    def download_course(self, course_id: str, course_name: str, force_rebuild: bool = False):
        """Baixa todas as aulas de um curso."""
        collector = LinkCollector(self.session)
        all_links = collector.collect_all_links(course_id, course_name, force_rebuild)

        if not all_links:
            logger.warning(f"Nenhum link encontrado para {course_name}")
            return

        logger.info(f"Iniciando download: {len(all_links)} itens")

        with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as video_executor:
            video_futures = {}
            for item_name, item_links in all_links.items():
                if item_links.get('video'):
                    future = video_executor.submit(self._download_video, item_name, item_links['video'])
                    video_futures[future] = item_name

            for future in as_completed(video_futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Erro no download: {e}")

        with ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pdf_executor:
            pdf_futures = {}
            for item_name, item_links in all_links.items():
                for pdf_url in item_links.get('pdf', []):
                    future = pdf_executor.submit(self._download_pdf_safe, item_name, pdf_url)
                    pdf_futures[future] = (item_name, pdf_url)

            for future in as_completed(pdf_futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Erro no download PDF: {e}")

        self._print_summary()

    def _print_summary(self):
        """Exibe resumo dos downloads."""
        logger.info(f"\n{'='*60}")
        logger.info(f"Vídeos OK: {self.successful_videos}")
        logger.info(f"Vídeos com falha: {self.failed_videos}")
        if self.drm_failed_videos:
            logger.warning(f"Vídeos com DRM falho (sem chave): {len(self.drm_failed_videos)}")
        logger.info(f"PDFs OK: {self.successful_pdfs}")
        logger.info(f"PDFs com falha: {self.failed_pdfs}")
        logger.info(f"{'='*60}\n")

    def _get_key_for_video(self, manifest_url: str) -> Optional[str]:
        """
        Obtém a chave DRM para um vídeo.
        Prioridade: DEFAULT_DRM_KEY > KeyOS automático > None
        """
        if DEFAULT_DRM_KEY:
            return DEFAULT_DRM_KEY
        
        if KEYOS_ENABLED and self.keyos_resolver:
            logger.info("Tentando obter chave via KeyOS...")
            key = self.keyos_resolver.fetch_drm_key_automatically(manifest_url)
            if key:
                return key
        
        return None

    def _download_video(self, item_name: str, video_url: str):
        """Baixa um vídeo com suporte a DRM automático."""
        output_dir = os.path.join(BASE_DIR, os.path.dirname(item_name))
        os.makedirs(output_dir, exist_ok=True)
        
        safe_name = clear_name(os.path.basename(item_name))
        output_path = os.path.join(output_dir, f"{safe_name}.mp4")

        if os.path.exists(output_path) and os.path.getsize(output_path) > 5 * 1024 * 1024:
            logger.info(f"✓ Já existe: {safe_name}")
            with self.lock:
                self.successful_videos += 1
            return

        logger.info(f"Baixando: {safe_name}")
        
        # Obter chave DRM se necessário
        key_pair = self._get_key_for_video(video_url)
        if key_pair:
            logger.info(f"  → Chave DRM disponível: {key_pair[:20]}...")

        # Tentar download
        self._download_video_internal(video_url, output_path, key_pair, item_name)

    def _download_video_internal(self, manifest_url: str, output_path: str, key_pair: Optional[str], item_name: str):
        """Implementação interna do download de vídeo com fallback DRM."""
        temp_dir = os.path.join(TEMP_DIR, clear_name(os.path.dirname(item_name)))
        os.makedirs(temp_dir, exist_ok=True)

        # -------------------------------------------------------------------
        # TENTATIVA 1: MÉTODO DIRETO (yt-dlp)
        # -------------------------------------------------------------------
        ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'retries': RETRY_ATTEMPTS,
            'ffmpeg_location': FFMPEG_DIR,
            'merge_output_format': 'mp4',
            'socket_timeout': TIMEOUT,
            'concurrent_fragment_downloads': 4,
            'allow_unplayable_formats': True,
        }
        if key_pair:
            ydl_opts['decryption_key'] = key_pair

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(manifest_url, download=True)
                downloaded_file = ydl.prepare_filename(info)

            if not os.path.exists(downloaded_file):
                base_name, _ = os.path.splitext(downloaded_file)
                if os.path.exists(f"{base_name}.mp4"):
                    downloaded_file = f"{base_name}.mp4"

            if os.path.exists(downloaded_file) and os.path.getsize(downloaded_file) > 5 * 1024 * 1024:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                if os.path.exists(output_path):
                    os.remove(output_path)
                shutil.move(downloaded_file, output_path)
                logger.info(f"✓ Vídeo OK (yt-dlp): {os.path.basename(output_path)}")
                with self.lock:
                    self.successful_videos += 1
                return
        except Exception as e:
            logger.warning(f"⚠ Método direto falhou. Iniciando Fallback DRM...")

        # -------------------------------------------------------------------
        # TENTATIVA 2: FALLBACK MANUAL (Raw Download -> Decrypt -> Mux)
        # -------------------------------------------------------------------
        if not key_pair:
            logger.error("🔒 Falha: Vídeo com DRM e nenhuma chave configurada.")
            with self.lock:
                self.failed_videos += 1
                self.drm_failed_videos.append(manifest_url)
            return

        temp_enc_v = os.path.join(temp_dir, "enc_video.mp4")
        temp_enc_a = os.path.join(temp_dir, "enc_audio.m4a")
        temp_dec_v = os.path.join(temp_dir, "dec_video.mp4")
        temp_dec_a = os.path.join(temp_dir, "dec_audio.m4a")

        try:
            # 1. Download bruto
            logger.info("  → Iniciando fallback manual...")
            opts_v = {'format': 'bv', 'allow_unplayable_formats': True, 'outtmpl': temp_enc_v, 'quiet': True, 'ffmpeg_location': FFMPEG_DIR}
            opts_a = {'format': 'ba', 'allow_unplayable_formats': True, 'outtmpl': temp_enc_a, 'quiet': True, 'ffmpeg_location': FFMPEG_DIR}

            with yt_dlp.YoutubeDL(opts_v) as ydl:
                ydl.download([manifest_url])
            with yt_dlp.YoutubeDL(opts_a) as ydl:
                ydl.download([manifest_url])

            # 2. Descriptografia
            decrypted = False
            if check_binary('shaka-packager') or check_binary('packager'):
                try:
                    logger.info("   [Fallback] Descriptografando via shaka-packager...")
                    decrypt_with_shaka(temp_enc_v, temp_dec_v, key_pair)
                    decrypt_with_shaka(temp_enc_a, temp_dec_a, key_pair)
                    decrypted = True
                except Exception as err_shaka:
                    logger.warning(f"   [!] shaka-packager falhou: {err_shaka}")

            if not decrypted and check_binary('mp4decrypt'):
                try:
                    logger.info("   [Fallback] Descriptografando via mp4decrypt...")
                    decrypt_with_mp4decrypt(temp_enc_v, temp_dec_v, key_pair)
                    decrypt_with_mp4decrypt(temp_enc_a, temp_dec_a, key_pair)
                    decrypted = True
                except Exception as err_mp4dec:
                    logger.warning(f"   [!] mp4decrypt falhou: {err_mp4dec}")

            if not decrypted:
                raise RuntimeError("Nenhum binário de descriptografia instalado.")

            # 3. Muxing via FFmpeg
            logger.info("   [Fallback] Unindo faixas com FFmpeg...")
            ffmpeg_bin = os.path.join(FFMPEG_DIR, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
            if not os.path.exists(ffmpeg_bin):
                ffmpeg_bin = 'ffmpeg'

            cmd_ffmpeg = [
                ffmpeg_bin, '-y',
                '-i', temp_dec_v,
                '-i', temp_dec_a,
                '-c', 'copy',
                output_path
            ]
            subprocess.run(cmd_ffmpeg, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024 * 1024:
                logger.info(f"✓ Vídeo OK (Fallback Manual): {os.path.basename(output_path)}")
                with self.lock:
                    self.successful_videos += 1
            else:
                raise RuntimeError("Falha ao gerar arquivo final.")

        except Exception as err:
            logger.error(f"❌ Erro no Fallback: {err}")
            with self.lock:
                self.failed_videos += 1
                self.drm_failed_videos.append(manifest_url)

        finally:
            for temp_f in [temp_enc_v, temp_enc_a, temp_dec_v, temp_dec_a]:
                if os.path.exists(temp_f):
                    try:
                        os.remove(temp_f)
                    except Exception:
                        pass

    def _download_pdf_safe(self, item_name: str, pdf_url: str):
        """Wrapper thread-safe para download de PDF."""
        output_dir = os.path.join(BASE_DIR, os.path.dirname(item_name))
        safe_name = clear_name(os.path.basename(item_name))
        output_path = os.path.join(output_dir, f"{safe_name}.pdf")
        
        if self._download_pdf(pdf_url, output_path):
            with self.lock:
                self.successful_pdfs += 1
        else:
            with self.lock:
                self.failed_pdfs += 1

    def _download_pdf(self, url: str, output_path: str) -> bool:
        """Baixa um PDF."""
        rel_path = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, timeout=TIMEOUT, verify=False)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue

                content_type = resp.headers.get('content-type', '').lower()

                if 'application/json' in content_type or 'text/plain' in content_type:
                    try:
                        data = resp.json()
                    except Exception:
                        time.sleep(2 ** attempt)
                        continue
                    if not isinstance(data, dict) or not data.get('url'):
                        time.sleep(2 ** attempt)
                        continue
                    pdf_url = f"{BASE_URL}{quote(data['url'], safe=':/')}"
                    pdf_resp = self.session.get(pdf_url, stream=True, timeout=60, verify=False)
                    content = pdf_resp.content
                    if pdf_resp.status_code == 200 and content.startswith(b'%PDF'):
                        with open(temp_path, 'wb') as f:
                            for chunk in [content[i:i+CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]:
                                f.write(chunk)
                        if os.path.exists(output_path):
                            os.remove(output_path)
                        shutil.move(temp_path, output_path)
                        return True
                    time.sleep(2 ** attempt)

                elif resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    shutil.move(temp_path, output_path)
                    return True
                else:
                    time.sleep(2 ** attempt)

            except Exception as e:
                logger.warning(f"Tentativa {attempt+1} PDF: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"✗ Falha PDF: {os.path.basename(output_path)}")
        return False

# ============================================================================
# MAIN
# ============================================================================
def main():
    downloader = PapaDownloader()
    downloader.login(
        email="psantos@gmail.com.br",
        jsessionid="992A9BFD233877D441B3541DC421E5E0",
        chave="e40065da97cdb123facebf6ff0de050a7712c2588771d3e4b00c97d0c4d070e509945d42241756803094669d9d99899a0e1d412c4ee1aab4c655465a13f5fd31"
    )

    courses = [
        {"id": "acb8c21948c052842af01f106ee5c872", "name": "isolada-direito-constitucional"},
    ]

    for course in courses:
        logger.info(f"\n{'='*60}\nINICIANDO: {course['name']}\n{'='*60}")
        try:
            downloader.download_course(course['id'], course['name'])
        except Exception as e:
            logger.error(f"Erro ao baixar curso {course['name']}: {e}")

if __name__ == "__main__":
    main()