# -*- coding: utf-8 -*-
"""
Papa Concursos Downloader v4
- Versão estável focada em vídeos e materiais
- APIs de geração IA (gerarAITranscricao, gerarAIEbook) retornam 403/erro
- Encoding UTF-8 corrigido
- Sem tentativas de geração que falham
"""

import subprocess
import sys

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

print("Instalando dependências...")
install('beautifulsoup4')
install('requests')
install('yt-dlp')
install('imageio-ffmpeg')
install('charset-normalizer')

import imageio_ffmpeg
import os
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
os.environ['PATH'] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get('PATH', '')

import re
import json
import time
import logging
import threading
import requests
import shutil
import pathlib
import unicodedata
from urllib.parse import quote
from bs4 import BeautifulSoup
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
BASE_DIR  = pathlib.Path.cwd() / 'teste'
TEMP_DIR  = pathlib.Path('D:/drivedepobre-temp')
CACHE_DIR = BASE_DIR

os.makedirs(BASE_DIR,  exist_ok=True)
os.makedirs(TEMP_DIR,  exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# LOGGING COM UTF-8
class UTF8Formatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, str):
            try:
                record.msg = record.msg.encode('utf-8', errors='replace').decode('utf-8')
            except:
                pass
        return super().format(record)

log_handler = logging.StreamHandler()
log_handler.setFormatter(UTF8Formatter(
    fmt='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

BATCH_DELAY       = 0.3
RETRY_ATTEMPTS    = 3
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 2
MAX_PDF_WORKERS   = 5
FFMPEG_DIR        = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())

BASE_URL = "https://www.papaconcursos.com.br"
PORTAL   = f"{BASE_URL}/portal"

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'accept-charset': 'utf-8,iso-8859-1;q=0.9,*;q=0.8',
    'referer': f'{BASE_URL}/portal',
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# UTILITÁRIOS
# ============================================================================
def safe_decode(data, fallback='utf-8'):
    if isinstance(data, str):
        return data
    if not isinstance(data, bytes):
        return str(data)
    
    encodings = ['utf-8', 'utf-8-sig', 'windows-1252', 'iso-8859-1', 'cp1252']
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return data.decode('utf-8', errors='replace')


def normalize_text(text):
    """Normaliza Unicode: remove combining marks duplos que causam Transcrieco"""
    if not isinstance(text, str):
        text = str(text)
    
    # Decompõe (NFD): é → e + ´
    text = unicodedata.normalize('NFD', text)
    
    # Remove TODAS as combining marks (diacríticos)
    # Isso evita o dobro de acentos que causa "Transcrieco"
    text = ''.join(
        c for c in text 
        if unicodedata.category(c) not in ('Mn', 'Mc')  # Remove combining marks
    )
    
    # Recompõe em forma canônica (NFC)
    text = unicodedata.normalize('NFC', text)
    
    return text


def clear_name(name: str) -> str:
    """Limpa nomes mantendo acentos corretos"""
    if not name:
        return ""
    
    if isinstance(name, bytes):
        name = safe_decode(name)
    else:
        name = str(name)
    
    name = normalize_text(name)
    name = re.sub(r'\.(pdf|docx|xlsx|pptx)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    name = name.rstrip('.')
    
    return name


def create_folder(path: str) -> str:
    """Cria pasta com Unicode normalizado"""
    path = normalize_text(path)
    os.makedirs(path, exist_ok=True)
    return path


def is_valid_pdf(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'rb') as f:
            return f.read(5).startswith(b'%PDF')
    except:
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
                          force_rebuild: bool = False) -> Dict:
        cache_file = os.path.join(CACHE_DIR, f"{course_id}_links.json")
        if os.path.exists(cache_file) and not force_rebuild:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if cached:
                logger.info(f"📦 Carregando cache: {course_name} ({len(cached)} aulas)")
                return cached
            os.remove(cache_file)

        logger.info(f"🔍 Coletando links: {course_name}")
        root_items = self._get_root_items(course_id, course_name)

        if not root_items:
            raise RuntimeError(f"Sessão expirada ou dados incorretos: {course_name}")

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links)

        if not all_links:
            raise RuntimeError(f"Nenhuma aula coletada: {course_name}")

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(all_links, f, ensure_ascii=False, indent=2)

        logger.info(f"💾 Cache salvo: {len(all_links)} aulas")
        return all_links

    def _get_root_items(self, course_id: str, course_name: str) -> Dict[str, str]:
        url = f"{PORTAL}/curso-aula/produto-pacote/{course_id}/{course_name}"
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        
        # Deteccão inteligente de encoding
        if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
            try:
                from charset_normalizer import from_bytes
                detected = from_bytes(resp.content).best()
                if detected:
                    resp.encoding = str(detected.encoding)
                else:
                    resp.encoding = 'utf-8'
            except:
                resp.encoding = 'utf-8'

        html_content = safe_decode(resp.content, resp.encoding or 'utf-8')
        html_content = normalize_text(html_content)

        if '<title>Login' in html_content[:500]:
            raise RuntimeError("SESSÃO EXPIRADA - Cookies inválidos ou vencidos!")

        soup = BeautifulSoup(html_content, 'html.parser')
        items: Dict[str, str] = {}

        for div in soup.find_all('div', class_=lambda c: c and 'header-wrapper' in c and 'primary' in c):
            target = div.get('data-target', '')
            m = re.search(r'#(item-[a-f0-9]+-[a-f0-9]+)$', target)
            if not m:
                continue
            token = m.group(1)
            h3 = div.find('h3')
            if not h3:
                continue
            for badge in h3.find_all('span', class_='badge'):
                badge.decompose()
            
            name = h3.get_text(strip=True)
            name = normalize_text(name)
            
            if name:
                cleaned = clear_name(name)
                items[cleaned] = token
                logger.debug(f"  ✓ {name}")

        logger.info(f"  Itens raiz: {len(items)}")
        return items

    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict):
        logger.info(f"📂 Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"  ⚠ Erro getTopico: {e}")
            return

        for sub in data.get('listTopics', []):
            parent_token = data.get('token', token)
            sub_token = f"{parent_token}-{sub['token']}"
            sub_name = normalize_text(sub.get('nome', ''))
            self._recurse(sub_token, course_id,
                          f"{path}/{clear_name(sub_name)}", all_links)

        for item in data.get('listTopicsMedia', []):
            self._process_media_item(item, data.get('token', token), course_id, path, all_links)

    def _process_media_item(self, item: dict, parent_token: str,
                           course_id: str, path: str, all_links: Dict):
        titulo = item.get('titulo', 'sem-titulo')
        titulo = normalize_text(titulo)
        item_title = clear_name(titulo)
        
        media_token = f"{parent_token}-{item['token']}"

        video_url = self._get_video_url(media_token, titulo)
        materials = self._get_materials(item['token'], course_id)

        lesson_key = f"{path}/{item_title}"
        all_links[lesson_key] = {
            'video': video_url,
            'materials': materials,
        }
        
        tipos = [m['tipo'] for m in materials]
        logger.info(f"  ✓ {lesson_key} | vídeo={'✓' if video_url else '✗'} | {len(materials)} mat {tipos}")

    def _get_video_url(self, media_token: str, titulo: str) -> str | None:
        """Obtém URL do vídeo"""
        try:
            resp = self.session.get(
                f"{PORTAL}/media",
                params={'token': media_token},
                headers=HEADERS, 
                timeout=TIMEOUT
            )
            resp.raise_for_status()
            text = resp.text
            
            # M3U8
            m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', text)
            if m:
                return m.group(1)
        except Exception as e:
            logger.debug(f"    ⚠ Video error: {e}")
        return None

    def _get_materials(self, topico_token: str, course_id: str) -> List[Dict]:
        """Obtém materiais (apenas os que já existem no servidor)"""
        materials = []
        try:
            resp = self.session.get(
                f"{PORTAL}/getDocumentoTopico",
                params={
                    'format': 'json',
                    'token': topico_token,
                    'tokenCurso': course_id,
                    'flagAI': '0-1-1-1-1-1-1-1-1-1-1-1-1-1-1-',
                    'flagTranscricao': '1'
                },
                headers=HEADERS, 
                timeout=TIMEOUT
            )
            resp.raise_for_status()
            
            docs = resp.json()
            if not docs:
                return []

            for doc in docs:
                tipo = doc.get('tipo', '')
                token = doc.get('token')
                nome = normalize_text(doc.get('nome', tipo))
                nome = clear_name(nome)

                # Documentos (D, L)
                if tipo in ('D', 'L') and token:
                    url = (f"{PORTAL}/documento-online-key"
                           f"?idDocumento={token}&tipo=D&token={course_id}")
                    materials.append({'url': url, 'nome': nome, 'tipo': tipo})

                # Transcrições que já existem (não tenta gerar)
                elif tipo in ('T', 'A-1') and token and token not in ('A-1', 'A-3'):
                    url = f"{PORTAL}/getTranscricao?token={token}&format=json"
                    materials.append({'url': url, 'nome': nome or 'Transcrição', 'tipo': tipo})

        except Exception as e:
            logger.debug(f"    ⚠ Materials error: {e}")
        
        return materials

    def _get_topico(self, token: str) -> Dict:
        url = f"{PORTAL}/getTopico"
        data_payload = {'token': token}
        
        resp = self.session.post(url, data=data_payload, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()


# ============================================================================
# DOWNLOADER
# ============================================================================
class PapaDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False

    def login(self, email: str, jsessionid: str, chave: str):
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.cookies.set('chave', chave)
        logger.info(f"✓ Login: {email}")

    def download_course(self, course_id: str, course_name: str,
                       force_rebuild: bool = False):
        collector = LinkCollector(self.session)
        all_links = collector.collect_all_links(course_id, course_name, force_rebuild)

        course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        
        video_tasks = []
        pdf_tasks = []

        # Organiza downloads
        for lesson_name, lesson_data in all_links.items():
            lesson_dir = create_folder(os.path.join(course_dir, lesson_name))
            
            # Vídeo
            if lesson_data.get('video'):
                video_file = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(video_file):
                    video_tasks.append((lesson_data['video'], video_file))

            # Materiais
            mat_idx = 1
            for mat in lesson_data.get('materials', []):
                url = mat.get('url', '')
                nome = mat.get('nome', 'material')
                
                mat_dir = create_folder(os.path.join(lesson_dir, 'material'))
                mat_file = os.path.join(mat_dir, f"{mat_idx:03d} - {nome}.pdf")
                mat_idx += 1

                if is_valid_pdf(mat_file):
                    continue
                if os.path.exists(mat_file):
                    os.remove(mat_file)
                if url:
                    pdf_tasks.append((url, mat_file))

        # Baixa em paralelo
        logger.info(f"\n⬇️  Iniciando downloads:")
        logger.info(f"  🎬 {len(video_tasks)} vídeos")
        logger.info(f"  📄 {len(pdf_tasks)} materiais\n")

        with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as video_pool, \
             ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pdf_pool:
            
            # Vídeos
            video_futures = [video_pool.submit(self._download_video, u, p) for u, p in video_tasks]
            for f in as_completed(video_futures):
                try:
                    f.result()
                except Exception as e:
                    logger.debug(f"  Video error: {e}")

            # PDFs
            pdf_futures = [pdf_pool.submit(self._download_pdf, u, p) for u, p in pdf_tasks]
            for f in as_completed(pdf_futures):
                try:
                    f.result()
                except Exception as e:
                    logger.debug(f"  PDF error: {e}")

        logger.info(f"\n✅ Concluído: {course_name}")

    def _download_video(self, manifest_url: str, output_path: str):
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
                'concurrent_fragment_downloads': 4,
                'socket_timeout': TIMEOUT,
                'ffmpeg_location': FFMPEG_DIR,
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
                logger.info(f"  ✓ Vídeo: {os.path.basename(output_path)}")
            else:
                logger.warning(f"  ⚠ Arquivo inválido")

        except Exception as e:
            err = str(e).lower()
            if 'drm' in err or 'encrypted' in err or 'widevine' in err:
                logger.error(f"  🔒 DRM: {os.path.basename(output_path)}")
            else:
                logger.error(f"  ✗ Vídeo: {str(e)[:80]}")

    def _download_pdf(self, url: str, output_path: str) -> bool:
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue

                content_type = resp.headers.get('content-type', '').lower()

                # JSON com URL dentro
                if 'application/json' in content_type or resp.content.startswith(b'{'):
                    try:
                        data = resp.json()
                        target_url = None
                        if isinstance(data, dict):
                            target_url = data.get('url') or data.get('link') or data.get('path')

                        if target_url:
                            if not target_url.startswith('http'):
                                target_url = f"{BASE_URL}{quote(target_url, safe=':/')}"
                            pdf_resp = self.session.get(target_url, stream=True, timeout=60, verify=False)
                            if pdf_resp.status_code == 200 and pdf_resp.content.startswith(b'%PDF'):
                                with open(temp_path, 'wb') as f:
                                    f.write(pdf_resp.content)
                                shutil.move(temp_path, output_path)
                                return True
                    except:
                        pass

                # PDF direto
                if resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    return True

            except Exception as e:
                logger.debug(f"  PDF retry {attempt + 1}: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"  ✗ PDF: {os.path.basename(output_path)}")
        return False


# ============================================================================
# MAIN
# ============================================================================
def main():
    downloader = PapaDownloader()
    downloader.login(
        email="jpsantos@gmail.com.br",
        jsessionid="80DC5DBF4C447527B0A5B7DF4EE21778",
        chave="e7441bc5116be064c5a0a49112e198377c01caecbbb429b0b05a13664790dff7159e6958cd5c4d2aec1b3040a6464488db1cd77a12556aeef12f150b0ee343cc"
    )

    courses = [
        {"id": "39fdd7ef56075ad91735dfcd97d0a223", "name": "novo-projeto-tj"},
    ]

    for course in courses:
        logger.info(f"\n{'='*70}\n📚 {course['name'].upper()}\n{'='*70}")
        downloader.download_course(course['id'], course['name'], force_rebuild=False)

if __name__ == "__main__":
    main()