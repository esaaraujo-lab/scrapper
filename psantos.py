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
from urllib.parse import quote
from bs4 import BeautifulSoup
from typing import List, Dict, Tuple
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

BATCH_DELAY       = 0.3   # delay entre PDFs (paralelo, pode ser menor)
RETRY_ATTEMPTS    = 3
CHUNK_SIZE        = 1024 * 256
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 2
MAX_PDF_WORKERS   = 5     # PDFs em paralelo
FFMPEG_DIR        = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())

HEADERS = {
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
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
                          force_rebuild: bool = False) -> Dict:
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
            raise RuntimeError(
                f"Sessão expirada ou course_id/name incorreto para '{course_name}'. "
                f"Renove os cookies e rode novamente.")

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links)

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
                "║  1. Abra portal2025.papaconcursos.com.br no Chrome       ║\n"
                "║  2. Faça login                                           ║\n"
                "║  3. F12 → Application → Cookies → copie JSESSIONID      ║\n"
                "║     e chave → cole na função main() do script            ║\n"
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

    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict):
        logger.info(f"Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"Erro getTopico ({token}): {e}")
            return

        for sub in data.get('listTopics', []):
            sub_token = f"item-{course_id}-{sub['token']}"
            self._recurse(sub_token, course_id,
                          os.path.join(path, clear_name(sub['nome'])), all_links)

        for item in data.get('listTopicsMedia', []):
            self._process_media_item(item, token, course_id, path, all_links)

    def _process_media_item(self, item: dict, parent_token: str,
                            course_id: str, path: str, all_links: Dict):
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

        VIDEO_DOMAINS = ('videotecaead.com.br',)
        candidate_iframes = [
            f for f in soup.find_all('iframe')
            if any(d in (f.get('src') or '') for d in VIDEO_DOMAINS)
        ]

        for iframe in candidate_iframes:
            iframe_src = iframe.get('src', '').strip()
            if not iframe_src:
                continue
            try:
                vid_resp = self.session.get(iframe_src, headers=HEADERS, timeout=TIMEOUT)

                # 1) m3u8 em scripts
                for script in BeautifulSoup(vid_resp.content, 'html.parser').find_all('script'):
                    m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                  script.string or '')
                    if m:
                        video_url = m.group(1)
                        break

                # 2) m3u8 no texto completo
                if not video_url:
                    m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', vid_resp.text)
                    if m:
                        video_url = m.group(1)

                # 3) Player DRM → tenta player antigo via slug
                if not video_url:
                    m = re.search(
                        r"const\s+manifestUrl\s*=\s*'(https?://[^']+\.mpd[^']*)'",
                        vid_resp.text)
                    if m:
                        mpd_url = m.group(1)
                        title_m = re.search(r'<title>([^<]+)</title>', vid_resp.text)
                        if title_m:
                            raw_title = title_m.group(1).replace('.mp4', '').strip()
                            slug      = self._title_to_slug(raw_title)
                            old_url   = f"https://embed.videotecaead.com.br/papaconcursos/{slug}"
                            try:
                                old_resp = self.session.get(old_url, headers=HEADERS, timeout=TIMEOUT)
                                if old_resp.status_code == 200:
                                    for script in BeautifulSoup(old_resp.content, 'html.parser').find_all('script'):
                                        mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                                       script.string or '')
                                        if mo:
                                            video_url = mo.group(1)
                                            break
                                    if not video_url:
                                        mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', old_resp.text)
                                        if mo:
                                            video_url = mo.group(1)
                                    if video_url:
                                        logger.info(f"  ✓ m3u8 via player antigo: {video_url[:80]}")
                            except Exception:
                                pass

                        if not video_url:
                            video_url = mpd_url
                            logger.warning(f"  ⚠ DRM sem alternativa, usando .mpd: {mpd_url[:60]}")

                if video_url:
                    break

            except Exception as e:
                logger.warning(f"  Erro iframe {iframe_src[:60]}: {e}")

        # PDFs
        materials = []
        for btn in soup.find_all('button', id=re.compile(r'btnMaterialDownload[a-f0-9]+')):
            doc_id = btn.get('data-value')
            tok    = btn.get('data-token')
            if doc_id and tok:
                materials.append(
                    f"https://portal2025.papaconcursos.com.br/portal/documento-online-key"
                    f"?idDocumento={doc_id}&tipo=D&token={tok}")

        lesson_key = os.path.join(path, item_title)
        all_links[lesson_key] = {'video': video_url, 'materials': materials}
        logger.info(f"  ✓ {lesson_key} | vídeo={'sim' if video_url else 'não'} | PDFs={len(materials)}")

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
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def login(self, email: str, jsessionid: str, chave: str):
        self.session.cookies.set('email', email)
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.cookies.set('chave', chave)
        logger.info("Sessão iniciada")

    def download_course(self, course_id: str, course_name: str):
        course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        collector  = LinkCollector(self.session)
        try:
            links = collector.collect_all_links(course_id, course_name, force_rebuild=False)
        except RuntimeError as e:
            logger.error(f"✗ {e}")
            logger.error("Renove os cookies no main() e rode novamente.")
            return

        logger.info(f"Iniciando download: {len(links)} aulas")

        video_tasks: List[Tuple[str, str]] = []
        pdf_tasks:   List[Tuple[str, str]] = []

        for rel_path, data in links.items():
            lesson_dir = create_folder(os.path.join(course_dir, rel_path))

            if data.get('video'):
                video_file = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(video_file):
                    video_tasks.append((data['video'], video_file))
                else:
                    logger.info(f"Vídeo já existe: {rel_path}")

            for idx, url in enumerate(data.get('materials', []), 1):
                mat_dir  = create_folder(os.path.join(lesson_dir, 'material'))
                pdf_file = os.path.join(mat_dir, f"{idx:03d} - material.pdf")
                if is_valid_pdf(pdf_file):
                    continue
                if os.path.exists(pdf_file):
                    os.remove(pdf_file)
                pdf_tasks.append((url, pdf_file))

        if video_tasks:
            logger.info(f"Baixando {len(video_tasks)} vídeos...")
            with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as pool:
                futures = [pool.submit(self._download_video, u, p) for u, p in video_tasks]
                for f in as_completed(futures):
                    f.result()

        if pdf_tasks:
            logger.info(f"Baixando {len(pdf_tasks)} PDFs (paralelo x{MAX_PDF_WORKERS})...")
            with ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pool:
                futures = {pool.submit(self._download_pdf, u, p): p for u, p in pdf_tasks}
                for f in as_completed(futures):
                    if f.result():
                        logger.info(f"PDF OK: {os.path.basename(futures[f])}")
                    time.sleep(BATCH_DELAY)

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
                'ffmpeg_location': FFMPEG_DIR,   # ← fix do warning AAC/MPEG-TS
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
            else:
                logger.warning(f"⚠ Arquivo inválido após download: {manifest_url[:70]}")

        except Exception as e:
            err = str(e)
            if any(x in err.lower() for x in ('drm', 'encrypted', 'widevine')):
                logger.error(f"🔒 DRM ativo: {os.path.basename(output_path)}")
            else:
                logger.error(f"Falha vídeo: {err[:120]}")

    def _download_pdf(self, url: str, output_path: str) -> bool:
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
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
                    pdf_url  = f"https://portal2025.papaconcursos.com.br{quote(data['url'], safe=':/')}"
                    pdf_resp = self.session.get(pdf_url, stream=True, timeout=60, verify=False)
                    content  = pdf_resp.content
                    if pdf_resp.status_code == 200 and content.startswith(b'%PDF'):
                        with open(temp_path, 'wb') as f:
                            for chunk in [content[i:i+CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]:
                                f.write(chunk)
                        shutil.move(temp_path, output_path)
                        return True
                    time.sleep(2 ** attempt)

                elif resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
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
        jsessionid="1FCB1E837316AFAE8CCB811F5071C27E",
        chave="27c4596a5145d6db6abcfef89eef6d8fb3a844f65b643829bb4d6721a7e10fb40724fc6f7e720a0ceb0c02b39a8a12fb89661ff2e8a9413346e6414372215d71"
    )

    courses = [
    
        {"id": "f98b2cf0852633f5b6f82aeb4bc6f046", "name": "novo-projeto-tj"},
        {"id": "469c72429c91520758f9e39f023c0c85", "name": "novo-projeto-tj"},
        {"id": "eba238e1bf1e1cc7ddec99284a011e85", "name": "novo-projeto-tj"},
        {"id": "d186b982663037d8d90b232c4cc257d1", "name": "novo-projeto-tj"},
        
        # ENAM / COMPLETOS

        {"id": "f98b2cf0852633f5b6f82aeb4bc6f046", "name": "novo-projeto-tj"},
        {"id": "469c72429c91520758f9e39f023c0c85", "name": "novo-projeto-tre"},
        {"id": "eba238e1bf1e1cc7ddec99284a011e85", "name": "novo-projeto-trf"},
        {"id": "d186b982663037d8d90b232c4cc257d1", "name": "projeto-trt-(novo)"},
        {"id": "8305cb076c642a7161be4948951797be", "name": "projeto-enam-2026"},

        # ISOLADAS
#        {"id": "927e039b5965fe89845e6468bd154b63", "name": "isolada-direito-administrativo"},
#        {"id": "b2786a4a77348a6d85a3945a51fb6462", "name": "isolada-direito-civil"},
#        {"id": "d46a1ccb6c598ce7124459cf9dccc613", "name": "isolada-direito--processual-do-trabalho"},
#       {"id": "7a6d5a08cdda488baa1b56e8fbeb6ad0", "name": "isolada-administracao-financeira-e-orcamentaria"},
#      {"id": "e8957556a65795d109fb499db86210ea", "name": "isolada-direito-ambiental"},
#        {"id": "d18e8aff4db3e025a3596e42c1b3b1ed", "name": "isolada-direito-previdenciario"},
#        {"id": "a0bbc1292e6ab81dabff5e2d1fbf83cf", "name": "isolada-direito-penal"},
#        {"id": "1e00353aff5113662810e224c564c20c", "name": "isolada-direito-do-trabalho"},
#        {"id": "6d4c35e2e7ea656cb4c5527fcbe0dcb5", "name": "isolada-direito-da-pessoa-com-deficiencia"},
#        {"id": "f26285f6e7ba4844d3bc1783cb528f4b", "name": "isolada-direito-processual-penal"},
#        {"id": "1786beaf94a74265bf5b6415efa40c63", "name": "isolada-direito-tributario"},
#        {"id": "1ae41acd8ae3e1ebfdb202b7d80b7b41", "name": "isolada-direitos-humanos"},
#        {"id": "0d495aa132cbc54bbfff254fcc3d6caa", "name": "isolada-informatica"},
#        {"id": "75d383ec676afc3d6a5d6c57b0972d4b", "name": "isolada-lei-8112-90-novo"},
#        {"id": "1556f7b5b9ac48e8f4000e78fc85149c", "name": "isolada-leis-penais-especiais"},
#        {"id": "7eaa6558bfbd18f6f696484ad5b404e6", "name": "isolada-direito-processual-civil"},
#        {"id": "73d8395bd4281fa19b9b1e7065dfb34a", "name": "isolada-raciocinio-logico-matematico"},
    ]

    for course in courses:
        logger.info(f"\n{'='*60}\nINICIANDO: {course['name']}\n{'='*60}")
        downloader.download_course(course['id'], course['name'])

if __name__ == "__main__":
    main()
