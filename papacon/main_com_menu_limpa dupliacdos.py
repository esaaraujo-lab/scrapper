import subprocess
import sys

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

print("Instalando dependências...")
install('beautifulsoup4')
install('requests')
install('yt-dlp')
install('imageio-ffmpeg')

import imageio_ffmpeg
import os
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ['PATH'] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get('PATH', '')

import re
import json
import time
import logging
import requests
import shutil
import pathlib
from pathlib import Path
from urllib.parse import quote
from bs4 import BeautifulSoup
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import queue
import yt_dlp
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Importa papa_capture para fallback DRM via Selenium
try:
    from papa_capture import capture_and_download as _drm_capture
    _DRM_CAPTURE_AVAILABLE = True
except ImportError:
    _DRM_CAPTURE_AVAILABLE = False

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
BASE_DIR  = pathlib.Path.cwd() / ''
TEMP_DIR  = pathlib.Path('D:/drivedepobre-temp')
CACHE_DIR = BASE_DIR

os.makedirs(BASE_DIR,  exist_ok=True)
os.makedirs(TEMP_DIR,  exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

BATCH_DELAY       = 0.3
RETRY_ATTEMPTS    = 3
CHUNK_SIZE        = 1024 * 256
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 2
MAX_PDF_WORKERS   = 5
FFMPEG_DIR        = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
    'referer': 'https://portal2025.papaconcursos.com.br/portal',
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# UTILITÁRIOS
# ============================================================================
def clear_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip().rstrip('.')

def create_folder(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

def is_valid_pdf(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'rb') as f:
            return f.read(5).startswith(b'%PDF')
    except Exception:
        return False

def is_video_complete(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 1024 * 1024

# ============================================================================
# COLETOR DE LINKS
# ============================================================================
class LinkCollector:
    def __init__(self, session: requests.Session):
        self.session = session

    def collect_all_links(self, course_id: str, course_name: str,
                          force_rebuild: bool = False,
                          on_item_found=None) -> Dict:
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
                    if on_item_found:
                        for rel_path, data in cached.items():
                            try:
                                on_item_found(rel_path, data)
                            except Exception as e:
                                logger.warning(f"Callback erro ({rel_path}): {e}")
                    return cached
                logger.warning(f"Cache vazio — recoletando: {course_name}")
                os.remove(cache_file)

        logger.info(f"Coletando links: {course_name}")
        root_items = self._get_root_items(course_id, course_name)

        if not root_items:
            raise RuntimeError(
                f"Sessão expirada ou course_id/name incorreto para '{course_name}'. "
                f"Renove os cookies e rode novamente.")

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links,
                          on_item_found=on_item_found)

        if not all_links:
            raise RuntimeError(
                f"Estrutura encontrada mas 0 aulas coletadas para '{course_name}'. "
                f"Possível sessão expirada no meio da coleta.")

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(all_links, f, ensure_ascii=False, indent=2)

        logger.info(f"Cache salvo: {len(all_links)} aulas")
        return all_links

    def _get_root_items(self, course_id: str, course_name: str) -> Dict[str, str]:
        url = (f"https://portal2025.papaconcursos.com.br/portal/curso-aula"
               f"/produto-pacote/{course_id}/{course_name}")
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()

        if '<title>Login' in resp.text[:500]:
            raise RuntimeError(
                "\n╔══════════════════════════════════════════════════════════╗\n"
                "║  SESSÃO EXPIRADA — cookies inválidos ou vencidos!        ║\n"
                "╚══════════════════════════════════════════════════════════╝"
            )

        soup  = BeautifulSoup(resp.content, 'html.parser')
        items: Dict[str, str] = {}
        for li in soup.find_all('li', class_=lambda c: c and 'item-tree' in c):
            text   = li.get_text(strip=True).split('Disponível')[0].strip()
            parts  = li.get('onclick', '').split("'")
            token  = parts[1] if len(parts) > 1 else None
            if token:
                items[text] = token
        return items

    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict,
                 on_item_found=None):
        logger.info(f"Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"Erro getTopico ({token}): {e}")
            return

        for sub in data.get('listTopics', []):
            sub_token = f"item-{course_id}-{sub['token']}"
            self._recurse(sub_token, course_id,
                          os.path.join(path, clear_name(sub['nome'])), all_links,
                          on_item_found=on_item_found)

        for item in data.get('listTopicsMedia', []):
            self._process_media_item(item, token, course_id, path, all_links,
                                      on_item_found=on_item_found)

    def _process_media_item(self, item: dict, parent_token: str,
                            course_id: str, path: str, all_links: Dict,
                            on_item_found=None):
        titulo     = item.get('titulo', 'sem-titulo')
        item_title = clear_name(titulo)
        prefix     = '-'.join(parent_token.split('-')[:2])
        media_token = f"{prefix}-{item['token']}"

        try:
            media_resp = self.session.get(
                'https://portal2025.papaconcursos.com.br/portal/media',
                params={'token': media_token}, headers=HEADERS, timeout=TIMEOUT)
            media_resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Erro media ({titulo}): {e}")
            return

        soup      = BeautifulSoup(media_resp.content, 'html.parser')
        video_url = None

        VIDEO_DOMAINS = (
            'videotecaead.com.br', 'player.videotecaead.com.br',
            'embed.videotecaead.com.br', 'player.vdocipher.com',
            'player.sambatech.com.br', 'cdn.plyr.io', 'vdocipher.com', 'sambatech.com.br'
        )

        for iframe in soup.find_all('iframe'):
            iframe_src = (iframe.get('src') or '').strip()
            if not iframe_src or not any(d in iframe_src for d in VIDEO_DOMAINS):
                continue
            try:
                vid_resp = self.session.get(iframe_src, headers=HEADERS, timeout=TIMEOUT)

                m = re.search(r"manifestUrl\s*=\s*['\"]([^'\"]+\.mpd[^'\"]*)", vid_resp.text)
                if m:
                    mpd_url = m.group(1)
                    m3u8_url = re.sub(r'/drm/dash/master\.mpd.*$', '/hls/master.m3u8', mpd_url)
                    try:
                        m3u8_resp = self.session.get(m3u8_url, headers=HEADERS, timeout=10)
                        if m3u8_resp.status_code == 200 and '#EXTM3U' in m3u8_resp.text[:20]:
                            video_url = m3u8_url
                            break
                    except Exception:
                        pass

                    if not video_url:
                        mpd_url_str = mpd_url
                        title_m = re.search(r'<title>([^<]+)</title>', vid_resp.text)
                        if title_m:
                            raw_title = title_m.group(1).replace('.mp4', '').strip()
                            slug = self._title_to_slug(raw_title)
                            if slug:
                                old_player_url = f"https://embed.videotecaead.com.br/papaconcursos/{slug}"
                                try:
                                    old_resp = self.session.get(old_player_url, headers=HEADERS, timeout=TIMEOUT)
                                    if old_resp.status_code == 200 and len(old_resp.text) > 500:
                                        old_soup = BeautifulSoup(old_resp.content, 'html.parser')
                                        for script in old_soup.find_all('script'):
                                            mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', script.string or '')
                                            if mo:
                                                video_url = mo.group(1)
                                                break
                                        if not video_url:
                                            mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', old_resp.text)
                                            if mo:
                                                video_url = mo.group(1)
                                except Exception:
                                    pass

                    if not video_url:
                        video_url = mpd_url_str
                    break

                m = re.search(r"(https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*)", vid_resp.text)
                if m:
                    video_url = m.group(1)
                    break

                m = re.search(r"(https?://[^\s'\"<>]+\.mpd[^\s'\"<>]*)", vid_resp.text)
                if m:
                    video_url = m.group(1)
                    break

            except Exception as e:
                logger.warning(f"  Erro iframe {iframe_src[:60]}: {e}")

        if not video_url:
            m = re.search(r"(https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*)", media_resp.text)
            if m: video_url = m.group(1)
        if not video_url:
            m = re.search(r"(https?://[^\s'\"<>]+\.mpd[^\s'\"<>]*)", media_resp.text)
            if m: video_url = m.group(1)

        materials = []
        portal_base = "https://portal2025.papaconcursos.com.br/portal"
        try:
            resp_docs = self.session.get(
                f"{portal_base}/getDocumentoTopico",
                params={
                    "format": "json",
                    "token": item["token"],
                    "tokenCurso": course_id,
                    "flagAI": "0-1-1-1-1-1-1-1-1-1-1-1-1-1-1-",
                    "flagTranscricao": "1",
                },
                headers=HEADERS, timeout=TIMEOUT)
            resp_docs.raise_for_status()
            docs = resp_docs.json()
            for doc in (docs or []):
                tipo = doc.get("tipo", "")
                doc_token = doc.get("token")
                nome = (doc.get("nome") or tipo).strip()[:80]

                if not doc_token:
                    continue

                if tipo in ("D", "L"):
                    url = f"{portal_base}/documento-online-key?idDocumento={doc_token}&tipo=D&token={course_id}"
                    materials.append({"url": url, "nome": nome, "tipo": "PDF"})

                elif tipo == "I":
                    url = f"{portal_base}/documento-online-key?idDocumento={doc_token}&tipo=I&token={course_id}"
                    materials.append({"url": url, "nome": nome, "tipo": "PDF"})

                elif tipo == "T":
                    url = f"{portal_base}/getTranscricao?format=json&token={doc_token}"
                    materials.append({"url": url, "nome": nome or "Transcricao", "tipo": "TXT"})

                elif tipo in ("A-1", "A-2", "A-3", "A-4"):
                    url = f"{portal_base}/getEbookAI?token={doc_token}"
                    materials.append({"url": url, "nome": nome or "Resumo IA", "tipo": "HTML"})

        except Exception as e:
            logger.warning(f"    getDocumentoTopico falhou: {e}")

        lesson_key = os.path.join(path, item_title)
        is_drm = False
        if video_url and ('.mpd' in video_url or '/drm/' in video_url):
            is_drm = True

        item_data = {
            'video': video_url,
            'materials': materials,
            'media_token': media_token,
            'is_drm': is_drm,
        }
        all_links[lesson_key] = item_data
        drm_tag = '🔒DRM' if is_drm else '✓'
        logger.info(f"  ✓ {lesson_key} | vídeo={'sim' if video_url else 'não'} {drm_tag} | PDFs={len(materials)}")

        if on_item_found:
            try:
                on_item_found(lesson_key, item_data)
            except Exception as e:
                logger.warning(f"  ⚠ Callback erro ({lesson_key}): {e}")

    def _title_to_slug(self, title: str) -> str:
        import unicodedata
        nfkd  = unicodedata.normalize('NFKD', title)
        ascii_ = nfkd.encode('ASCII', 'ignore').decode('ASCII').upper()
        clean = re.sub(r'[^A-Z0-9\s\-]', '', ascii_)
        parts = [p.strip() for p in clean.split('-') if p.strip()]
        return '-'.join(p.replace(' ', '_') for p in parts)

    def _get_topico(self, token: str) -> dict:
        resp = self.session.get(
            'https://portal2025.papaconcursos.com.br/portal/getTopico',
            params={'format': 'json', 'token': token},
            headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

# ============================================================================
# DOWNLOADER
# ============================================================================
class PapaDownloader:
    def __init__(self, mode: str = 'all'):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.mode = mode

    def login(self, email: str, jsessionid: str, chave: str):
        self._email = email
        self._jsessionid = jsessionid
        self._chave = chave
        for domain in ['.papaconcursos.com.br', 'www.papaconcursos.com.br',
                       'portal2025.papaconcursos.com.br']:
            try:
                self.session.cookies.set('email', email, domain=domain, path='/')
                self.session.cookies.set('JSESSIONID', jsessionid, domain=domain, path='/')
                self.session.cookies.set('chave', chave, domain=domain, path='/')
            except Exception:
                pass
        logger.info(f"Sessão iniciada — cookies setados em 3 domínios")

    def _refresh_session(self):
        if hasattr(self, '_email') and self._email:
            try:
                for domain in ['.papaconcursos.com.br', 'www.papaconcursos.com.br',
                               'portal2025.papaconcursos.com.br']:
                    try:
                        self.session.cookies.set('email', self._email, domain=domain, path='/')
                        self.session.cookies.set('JSESSIONID', self._jsessionid, domain=domain, path='/')
                        self.session.cookies.set('chave', self._chave, domain=domain, path='/')
                    except Exception:
                        pass
            except Exception:
                pass

    def download_course(self, course_id: str, course_name: str):
        self.course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        collector  = LinkCollector(self.session)

        try:
            logger.info(f"🔍 Iniciando escaneamento (Modo: {self.mode.upper()}): {course_name}")
            collector.collect_all_links(course_id, course_name,
                                          force_rebuild=True,
                                          on_item_found=self._download_item_inline)
        except RuntimeError as e:
            logger.error(f"✗ {e}")
        except Exception as e:
            logger.error(f"✗ Erro no escaneamento: {e}")

    def _cleanup_duplicates_and_double_extensions(self, material_dir: str):
        """
        1. Corrige extensões duplas (.pdf.pdf) e prefixos numéricos repetidos.
        2. Compara o tamanho exato dos arquivos e remove nomes genéricos legados
           (ex: '001 - material.pdf') se o arquivo com nome correto tiver o mesmo tamanho.
        """
        try:
            mat_path = Path(material_dir)
            if not mat_path.is_dir():
                return

            # --- PARTE 1: Correção de extensão dupla e prefixo ---
            for file_path in list(mat_path.glob('*')):
                if not file_path.is_file():
                    continue

                filename = file_path.name

                if re.search(r'\.(pdf|md|txt|html)\.(pdf|md|txt|html)$', filename, flags=re.I):
                    clean_name = re.sub(r'\.(pdf|md|txt|html)$', '', filename, flags=re.I)
                    target_path = file_path.parent / clean_name

                    if target_path.exists() and target_path != file_path:
                        try:
                            file_path.unlink()
                            logger.info(f"  🗑️ Removido duplicado com extensão dupla: {filename}")
                        except Exception as e:
                            logger.warning(f"  ⚠ Não foi possível deletar {filename}: {e}")
                    else:
                        try:
                            file_path.rename(target_path)
                            logger.info(f"  ✏️ Renomeado: {filename} -> {clean_name}")
                        except Exception as e:
                            logger.warning(f"  ⚠ Não foi possível renomear {filename}: {e}")

                elif re.match(r'^(\d{3}\s*-\s*)\1', filename):
                    clean_name = re.sub(r'^(\d{3}\s*-\s*)\1', r'\1', filename)
                    target_path = file_path.parent / clean_name
                    if target_path.exists() and target_path != file_path:
                        try:
                            file_path.unlink()
                            logger.info(f"  🗑️ Removido duplicado de prefixo: {filename}")
                        except Exception as e:
                            logger.warning(f"  ⚠ Não foi possível deletar {filename}: {e}")
                    else:
                        try:
                            file_path.rename(target_path)
                            logger.info(f"  ✏️ Renomeado prefixo duplicado: {filename} -> {clean_name}")
                        except Exception as e:
                            logger.warning(f"  ⚠ Não foi possível renomear {filename}: {e}")

            # --- PARTE 2: Remoção de legados genéricos (ex: 001 - material.pdf) por TAMANHO EXATO ---
            files = [f for f in mat_path.glob('*') if f.is_file()]
            by_size = {}

            for f in files:
                try:
                    sz = f.stat().st_size
                    if sz > 0:
                        by_size.setdefault(sz, []).append(f)
                except Exception:
                    pass

            for sz, group in by_size.items():
                if len(group) > 1:
                    generic_files = []
                    descriptive_files = []

                    for f in group:
                        fname_lower = f.name.lower()
                        # Identifica padrões de nomes legados/genéricos
                        if re.search(r'^\d*[\s\-_.]*material\.(pdf|md|txt|html)$', fname_lower):
                            generic_files.append(f)
                        else:
                            descriptive_files.append(f)

                    # Se temos o novo arquivo específico e a versão genérica antiga do mesmo tamanho:
                    if descriptive_files and generic_files:
                        for gen_f in generic_files:
                            try:
                                gen_f.unlink()
                                logger.info(f"  🗑️ Removido arquivo legado/genérico duplicado ({sz} bytes): {gen_f.name}")
                            except Exception as e:
                                logger.warning(f"  ⚠ Erro ao deletar legado {gen_f.name}: {e}")

        except Exception as e:
            logger.warning(f"  ⚠ Erro ao limpar pasta {material_dir}: {e}")

    def _download_item_inline(self, rel_path: str, data: dict):
        try:
            lesson_dir = create_folder(os.path.join(self.course_dir, rel_path))
            video_file = os.path.join(lesson_dir, '001 - aula.mp4')
            mat_dir = create_folder(os.path.join(lesson_dir, 'material'))

            # --- 1. BAIXAR VÍDEOS ---
            if self.mode in ('all', 'videos'):
                if data.get('video') and not is_video_complete(video_file):
                    is_drm = data.get('is_drm', False)
                    logger.info(f"📥 Baixando vídeo {'🔒DRM' if is_drm else 'regular'}: {rel_path}")
                    self._refresh_session()
                    try:
                        self._download_video(data['video'], video_file, data.get('media_token', ''))
                    except Exception as e:
                        logger.error(f"  ❌ Erro download vídeo: {e}")
                elif data.get('video'):
                    logger.info(f"✓ Vídeo já existe: {rel_path}")

            # --- 2. BAIXAR MATERIAIS / PDFs ---
            if self.mode in ('all', 'materials'):
                self._cleanup_duplicates_and_double_extensions(mat_dir)
                self._convert_existing_to_md(mat_dir)

                for idx, mat in enumerate(data.get('materials', []), 1):
                    mat_name = mat.get('nome', '') or 'material'
                    safe_name = re.sub(r'[<>:"/\\|?*]', '', mat_name).strip()[:80]
                    safe_name = re.sub(r'\.(pdf|md|txt|html)$', '', safe_name, flags=re.I).strip()
                    safe_name = re.sub(r'^\d+[\s\-_.]*', '', safe_name).strip()
                    if not safe_name:
                        safe_name = 'material'

                    mat_tipo = mat.get('tipo', 'PDF')
                    if mat_tipo == 'TXT' or 'transcri' in mat_name.lower():
                        ext = '.md'
                    elif mat_tipo == 'HTML' or 'ebook' in mat_name.lower():
                        ext = '.md'
                    else:
                        ext = '.pdf'

                    pdf_file = os.path.join(mat_dir, f"{idx:03d} - {safe_name}{ext}")
                    stem = pdf_file.rsplit('.', 1)[0]

                    if (is_valid_pdf(stem + '.pdf') or 
                        (os.path.exists(stem + '.md') and os.path.getsize(stem + '.md') > 100) or
                        (os.path.exists(stem + '.txt') and os.path.getsize(stem + '.txt') > 100) or
                        (os.path.exists(stem + '.html') and os.path.getsize(stem + '.html') > 100)):
                        continue

                    try:
                        if self._download_pdf(mat['url'], pdf_file, mat_name=mat_name):
                            # Executa limpeza logo após o download para deletar o 'material.pdf' se for cópia
                            self._cleanup_duplicates_and_double_extensions(mat_dir)
                    except Exception as e:
                        logger.warning(f"  ⚠ Erro PDF: {e}")

        except Exception as e:
            logger.warning(f"  ⚠ on_item_found erro ({rel_path}): {e}")

    def _download_video(self, manifest_url: str, output_path: str, media_token: str = ''):
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        try:
            ydl_opts = {
                'format': 'bv*+ba/b',
                'outtmpl': temp_path,
                'quiet': True,
                'no_warnings': True,
                'retries': RETRY_ATTEMPTS,
                'concurrent_fragment_downloads': 10,
                'socket_timeout': TIMEOUT,
                'ffmpeg_location': FFMPEG_DIR,
                'merge_output_format': 'mp4',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([manifest_url])

            downloaded = temp_path
            if not os.path.exists(downloaded):
                for ext in ['.mp4', '.mkv', '.webm', '.m4v']:
                    if os.path.exists(downloaded + ext):
                        downloaded = downloaded + ext
                        break

            if os.path.exists(downloaded) and os.path.getsize(downloaded) > 100 * 1024:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.move(downloaded, output_path)
                logger.info(f"✓ Vídeo: {os.path.basename(output_path)}")

        except Exception as e:
            if 'drm' in str(e).lower() and _DRM_CAPTURE_AVAILABLE:
                cookies_path = str(pathlib.Path.home() / 'PapaConcursos_Downloads' / 'cookies_papa.json')
                _drm_capture(manifest_url, output_path, media_token, cookies_path)

    def _download_pdf(self, url: str, output_path: str, mat_name: str = '') -> bool:
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                self._refresh_session()
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
                if resp.status_code == 200:
                    if resp.content.startswith(b'%PDF'):
                        with open(temp_path, 'wb') as f:
                            f.write(resp.content)
                        shutil.move(temp_path, output_path)
                        logger.info(f"  ✓ PDF OK: {os.path.basename(output_path)}")
                        return True
                    else:
                        md_content = self._html_to_markdown(resp.text)
                        md_path = str(Path(output_path).with_suffix('.md'))
                        with open(md_path, 'w', encoding='utf-8') as f:
                            f.write(md_content)
                        logger.info(f"  ✓ MD OK: {os.path.basename(md_path)}")
                        return True
            except Exception:
                time.sleep(2 ** attempt)
        return False

    def _html_to_markdown(self, html_content: str) -> str:
        if not html_content: return ""
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.S | re.I)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.S | re.I)
        html_content = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\n\1\n\n', html_content, flags=re.S | re.I)
        html_content = re.sub(r'<[^>]+>', '', html_content)
        return html_content.strip() + '\n'

    def _convert_existing_to_md(self, material_dir: str):
        pass

# ============================================================================
# MENU INTERATIVO E MAIN
# ============================================================================
def show_menu() -> str:
    print("\n" + "="*60)
    print("           PAPA CONCURSOS - MENU DE DOWNLOAD")
    print("="*60)
    print("  [1] Baixar TUDO (Vídeos + Materiais/PDFs)")
    print("  [2] Baixar apenas MATERIAIS (PDFs, Resumos IA, Transcrições)")
    print("  [3] Baixar apenas VÍDEOS")
    print("="*60)
    
    while True:
        choice = input("Escolha uma opção (1, 2 ou 3): ").strip()
        if choice == '1':
            return 'all'
        elif choice == '2':
            return 'materials'
        elif choice == '3':
            return 'videos'
        print("Opção inválida! Digite 1, 2 ou 3.")

def main():
    download_mode = show_menu()

    downloader = PapaDownloader(mode=download_mode)
    downloader.login(
        email="kaique.novaes.wf923@mailinator.com",
        jsessionid="A70306BC87DA59939130A2DB9B79C424",
        chave="d9PKoBSpJxPrgoKMReLS4fUFirBbAQYHLDlAQ1Jn2HY"
    )

    courses = [
        {"id": "071de3df2f21a589f0bbf00bd083d86f", "name": "isolada-direito-administrativo"},
        {"id": "9773914616f89ab1980acb57b7ed5eaf", "name": "isolada-direito-processual-do-trabalho"},
        {"id": "6f2ca43f9856aa8e1b83d423b7fe6b2c", "name": "isolada-direito-previdenciario"},
        {"id": "bd7a372e7bf194f46273efc12acd5db2", "name": "isolada-direito-processual-civil"},
        {"id": "95df24c5d81653d4013e704a1cf70ab1", "name": "isolada-direito-civil"},
        {"id": "fa391685fd6d4e89184eea3d0e2eedb5", "name": "isolada-administracao-geral-e-publica"},
        {"id": "2a3afca6af0b4548d92d6ef60abadcfb", "name": "isolada-direito-constitucional"},  
        {"id": "5184d86695b06d7afaa4d530c3541268", "name": "isolada-direito-eleitoral"}, 
        {"id": "7bcaca6a5b7c106e57235ecdb059475a", "name": "isolada-administracao-financeira-e-orcamentaria"},
        {"id": "d332f1704eaec04933e89f4c5728f1c1", "name": "isolada-direito-ambiental"},
        {"id": "50a5e2a6583434db945c7da380350e2c", "name": "isolada-direito-penal"},
        {"id": "fd561e39799561927ec7d7762cc07765", "name": "isolada-direito-do-trabalho"},
        {"id": "07fbb3dd4bd52014827468fbfc590023", "name": "isolada-direito-da-pessoa-com-deficiencia"},
        {"id": "217cfca4e68433a0c7dd9cc66e1719e4", "name": "isolada-direito-processual-penal"},
        {"id": "70be9a43452f51045fb0db4449a231d8", "name": "isolada-direito-tributario"},
        {"id": "67212dfc502e3356ff22bf2fc526705d", "name": "isolada-direitos-humanos"},
        {"id": "9de5c23b9e74457ad121d5d0f13340a9", "name": "isolada-informatica"},
        {"id": "7d31ca838e231721238f3694f8e5f13e", "name": "isolada-lei-8112-90-novo"},
        {"id": "0c155380069771f015df1122ac7b04be", "name": "isolada-leis-penais-especiais"},
        {"id": "e3f5c0659a3eb8bd182730216be3b117", "name": "isolada-raciocinio-logico-matematico"},
        {"id": "93c270527e0fce62e43acb9f8be61a4e", "name": "projeto-enam-2026"},           
    ]

    for course in courses:
        logger.info(f"\n{'='*60}\nINICIANDO ({download_mode.upper()}): {course['name']}\n{'='*60}")
        downloader.download_course(course['id'], course['name'])

if __name__ == "__main__":
    main()