import subprocess
import sys

# INSTALAÇÃO SILENCIOSA
def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

print("Instalando dependências...")
install('beautifulsoup4')
install('requests')
install('yt-dlp')

import os
import re
import json
import time
import logging
import requests
import pathlib
import shutil
from urllib.parse import quote
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# CONFIGURAÇÕES PARA WINDOWS / LOCAL
# ============================================================================
BASE_DIR = pathlib.Path(r"G:\Drives compartilhados\! Assinatura Papa Concursos\teste")
TEMP_DIR = pathlib.Path("C:/Temp/papaconcursos-temp")
CACHE_DIR = BASE_DIR

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

if os.name == 'nt':
    os.system('')

# ============================================================================
# LOGGING COLORIDO
# ============================================================================
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[92m',
        'WARNING': '\033[93m',
        'ERROR': '\033[91m',
        'CRITICAL': '\033[95m',
        'RESET': '\033[0m'
    }
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.msg = f"{color}{record.msg}{self.COLORS['RESET']}"
        return super().format(record)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ColoredFormatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S'))
logger.handlers = [handler]

# ============================================================================
# CONSTANTES
# ============================================================================
BATCH_DELAY = 2
RETRY_ATTEMPTS = 3
MAX_VIDEO_WORKERS = 2
HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'referer': 'https://portal2025.papaconcursos.com.br/portal',
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# FUNÇÕES UTILITÁRIAS
# ============================================================================
def clear_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip().rstrip('.')

def create_folder(path: str):
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

def is_video_complete(video_path: str) -> bool:
    return os.path.exists(video_path) and os.path.getsize(video_path) > 1024 * 1024  # > 1MB

# ============================================================================
# CLASSE PARA COLETAR TODOS OS LINKS
# ============================================================================
class LinkCollector:
    def __init__(self, session: requests.Session):
        self.session = session

    def collect_all_links(self, course_id: str, course_name: str) -> Dict:
        cache_file = os.path.join(CACHE_DIR, f"{course_id}_links.json")
        if os.path.exists(cache_file):
            logger.info(f"Carregando cache de links: {course_name}")
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    links = json.load(f)
                    if links:
                        return links
            except Exception:
                logger.warning("Cache corrompido ou vazio, recriando...")

        logger.info(f"Coletando todos os links do curso: {course_name}")
        structure = self._get_course_structure(course_id, course_name)
        all_links = self._traverse_structure(structure, course_id)

        # Salva cache se encontrou dados
        if all_links:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(all_links, f, ensure_ascii=False, indent=2)

        logger.info(f"Coleta finalizada: {len(all_links)} aulas encontradas")
        return all_links

    def _get_course_structure(self, course_id: str, course_name: str) -> Dict:
        url = f"https://portal2025.papaconcursos.com.br/portal/curso-aula/produto-pacote/{course_id}/{course_name}"
        response = self.session.get(url, headers=HEADERS, timeout=30, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        structure = {}
        
        # 1. Busca por padrão de Pacotes / Cursos Completos
        items = soup.find_all('li', class_='list-group-item item-tree clearfix')
        for item in items:
            text = item.get_text(strip=True).split('Disponível')[0].strip()
            onclick = item.get('onclick')
            if onclick and "'" in onclick:
                token = onclick.split("'")[1]
                if token:
                    structure[text] = {"token": token, "sub_items": {}}

        # 2. Fallback para Disciplinas Isoladas
        if not structure:
            logger.info("Tentando extrair estrutura para Disciplina Isolada...")
            for element in soup.find_all(['li', 'div', 'tr', 'a'], onclick=True):
                onclick = element.get('onclick', '')
                if any(k in onclick for k in ['getTopico', 'carregarTopico', 'carregaMidia']) or "'" in onclick:
                    parts = onclick.split("'")
                    if len(parts) > 1:
                        token = parts[1]
                        text = element.get_text(strip=True).split('Disponível')[0].strip()
                        if text and token and len(token) > 5 and text not in structure:
                            structure[text] = {"token": token, "sub_items": {}}

        return structure

    def _traverse_structure(self, structure: Dict, course_id: str, path: str = "") -> Dict:
        all_links = {}
        for name, data in structure.items():
            token = data['token']
            folder_name = clear_name(name)
            full_path = os.path.join(path, folder_name) if path else folder_name

            logger.info(f"Explorando: {name}")
            try:
                media_data = self._get_media_data(token, course_id)
                items_to_process = []

                # Suporte a listTopics (Disciplinas isoladas / Módulos diretos)
                if media_data.get('listTopics'):
                    for sub in media_data['listTopics']:
                        items_to_process.append({
                            'token': sub['token'],
                            'titulo': sub.get('nome') or sub.get('titulo') or 'Aula'
                        })

                # Suporte a listTopicsMedia (Cursos completos)
                if media_data.get('listTopicsMedia'):
                    for item in media_data['listTopicsMedia']:
                        items_to_process.append({
                            'token': item['token'],
                            'titulo': item.get('titulo') or item.get('nome') or 'Aula'
                        })

                for item in items_to_process:
                    media_token = f"item-{course_id}-{item['token']}"
                    media_response = self.session.get(
                        'https://portal2025.papaconcursos.com.br/portal/media',
                        params={'token': media_token},
                        headers=HEADERS,
                        timeout=30, verify=False)
                    
                    soup = BeautifulSoup(media_response.text, 'html.parser')

                    iframe = soup.find('iframe', class_='videofrontplayer')
                    video_url = None
                    if iframe and iframe.get('src'):
                        vid_page = self.session.get(
                            iframe['src'], 
                            headers={'User-Agent': HEADERS['user-agent'], 'Referer': 'https://portal2025.papaconcursos.com.br/'},
                            timeout=30, verify=False
                        ).text
                        m = re.search(r'(https?://[^\s\'"]+\.m3u8[^\s\'"]*)', vid_page)
                        if m:
                            video_url = m.group(1)

                    materials = []
                    for btn in soup.find_all('button', id=re.compile(r'btnMaterialDownload')):
                        doc_id = btn.get('data-value')
                        token_val = btn.get('data-token')
                        if doc_id and token_val:
                            materials.append(
                                f"https://portal2025.papaconcursos.com.br/portal/documento-online-key?idDocumento={doc_id}&tipo=D&token={token_val}"
                            )

                    lesson_key = os.path.join(full_path, clear_name(item['titulo']))
                    all_links[lesson_key] = {
                        "video": video_url,
                        "materials": materials
                    }

            except Exception as e:
                logger.warning(f"Erro em {name}: {e}")

        return all_links

    def _get_media_data(self, token: str, course_id: str):
        resp = self.session.get(
            'https://portal2025.papaconcursos.com.br/portal/getTopico',
            params={'format': 'json', 'token': token},
            headers=HEADERS,
            timeout=30, verify=False
        )
        return resp.json()

# ============================================================================
# DOWNLOADER COMPLETO
# ============================================================================
class PapaDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def login(self, email: str, jsessionid: str, chave: str):
        self.session.cookies.set('email', email)
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.cookies.set('chave', chave)
        logger.info("Sessão iniciada com cookies")

    def download_course(self, course_id: str, course_name: str):
        course_dir = create_folder(os.path.join(BASE_DIR, clear_name(course_name)))
        collector = LinkCollector(self.session)
        links = collector.collect_all_links(course_id, course_name)

        logger.info(f"Iniciando download de {len(links)} aulas encontradas...")

        video_tasks = []
        pdf_tasks = []

        for rel_path, data in links.items():
            lesson_dir = create_folder(os.path.join(course_dir, rel_path))
            mat_dir = create_folder(os.path.join(lesson_dir, 'material'))

            # Vídeo
            if data.get('video'):
                video_file = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(video_file):
                    video_tasks.append((data['video'], video_file))
                else:
                    logger.info(f"Vídeo já existe: {os.path.basename(lesson_dir)}")

            # PDFs
            for idx, url in enumerate(data.get('materials', []), 1):
                pdf_file = os.path.join(mat_dir, f"{idx:03d} - material.pdf")
                if os.path.exists(pdf_file) and is_valid_pdf(pdf_file):
                    continue
                if os.path.exists(pdf_file):
                    os.remove(pdf_file)
                pdf_tasks.append((url, pdf_file))

        # Baixar vídeos em paralelo
        if video_tasks:
            logger.info(f"Baixando {len(video_tasks)} vídeos em paralelo...")
            with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as exec:
                futures = [
                    exec.submit(self._download_video, url, path)
                    for url, path in video_tasks
                ]
                for f in as_completed(futures):
                    f.result()

        # Baixar PDFs sequencialmente
        if pdf_tasks:
            logger.info(f"Baixando {len(pdf_tasks)} PDFs...")
            for url, path in pdf_tasks:
                success = self._download_pdf(url, path)
                if success:
                    logger.info(f"PDF OK: {os.path.basename(path)}")
                time.sleep(BATCH_DELAY)

    def _download_video(self, m3u8_url: str, output_path: str):
        rel_path = os.path.relpath(output_path, BASE_DIR)
        temp_path = os.path.join(TEMP_DIR, rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        try:
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'outtmpl': temp_path,
                'quiet': True,
                'no_warnings': True,
                'retries': 3,
                'fragment_retries': 3,
                'concurrent_fragments': 8,
                'http_headers': {
                    'User-Agent': HEADERS['user-agent'],
                    'Referer': 'https://portal2025.papaconcursos.com.br/'
                }
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])
            
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            shutil.move(temp_path, output_path)
            logger.info(f"Vídeo salvo: {os.path.basename(os.path.dirname(output_path))}")
        except Exception as e:
            logger.error(f"Falha vídeo {os.path.basename(output_path)}: {e}")

    def _download_pdf(self, url: str, output_path: str) -> bool:
        rel_path = os.path.relpath(output_path, BASE_DIR)
        temp_path = os.path.join(TEMP_DIR, rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, headers=HEADERS, timeout=30, verify=False)
                if resp.status_code != 200:
                    time.sleep(2)
                    continue

                content_type = resp.headers.get('content-type', '').lower()
                
                if 'application/json' in content_type or 'text/plain' in content_type:
                    data = resp.json()
                    if data.get('url'):
                        pdf_url = "https://portal2025.papaconcursos.com.br" + quote(data['url'], safe=':/')
                        pdf_resp = self.session.get(pdf_url, headers=HEADERS, stream=True, timeout=60, verify=False)
                        if pdf_resp.status_code == 200:
                            content_pdf = pdf_resp.content
                            if content_pdf.startswith(b'%PDF'):
                                with open(temp_path, 'wb') as f:
                                    for chunk in pdf_resp.iter_content(1024*256):
                                        if chunk:
                                            f.write(chunk)
                                shutil.move(temp_path, output_path)
                                return True
                elif 'application/pdf' in content_type or 'octet-stream' in content_type:
                    content = resp.content
                    if content.startswith(b'%PDF'):
                        with open(temp_path, 'wb') as f:
                            f.write(content)
                        shutil.move(temp_path, output_path)
                        return True
            except Exception as e:
                logger.warning(f"Tentativa {attempt+1} PDF falhou: {e}")
                time.sleep(2)
        return False

# ============================================================================
# EXECUÇÃO PRINCIPAL
# ============================================================================
def main():
    downloader = PapaDownloader()
    downloader.login(
        email="psantos@gmail.com.br",
        jsessionid="21CAB32A8E198A9F58383013559CDD8B",
        chave="1ec59f04fe526ac91a776972b2b353bf26c441887cd8cedea3dcb4f6e867855bd8c7d6c87e00eef315f8c487ac9fc9e88a988390a21479451f4aa4ffebd0e569"
    )

    courses = [
        # ISOLADAS
        {"id": "f864fe05d6600d4fbe1d55550a6ab4c9", "name": "novo-projeto-tj"},

        # COMPLETOS

        # {"id": "eb5b92c6017e8a03bce8ba532646be93", "name": "projeto-enam-2026"},
        # {"id": "3c17971ccacd714c35fdb1156d30cc92", "name": "jornada-fgv-tjsc"},        
        # {"id": "39fdd7ef56075ad91735dfcd97d0a223", "name": "novo-projeto-tj"},
        # {"id": "03c8d5b8c19fd36838dec9b7e14cb6a9", "name": "novo-projeto-tre"},
        # {"id": "682a742a194b477c1d56eb88801764c9", "name": "novo-projeto-trf"},
        # {"id": "21980a7f65806717a077d1282906bf80", "name": "projeto-trt-(novo)"},
    ]

    for course in courses:
        logger.info(f"\n{'='*60}\nINICIANDO: {course['name']}\n{'='*60}")
        try:
            downloader.download_course(course['id'], course['name'])
        except Exception as e:
            logger.error(f"Erro crítico no curso {course['name']}: {e}")

if __name__ == "__main__":
    main()
