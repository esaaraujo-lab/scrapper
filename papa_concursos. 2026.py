# -*- coding: utf-8 -*-
"""
Papa Concursos Downloader v3
Correções de encoding em TODOS os níveis (requests, BeautifulSoup, logging, filesystem)
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
install('charset-normalizer')  # ← ADICIONADO para detecção inteligente de encoding

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
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
BASE_DIR  = pathlib.Path.cwd() / 'Cursos Papa Concursos'
TEMP_DIR  = pathlib.Path('D:/drivedepobre-temp')
CACHE_DIR = BASE_DIR

os.makedirs(BASE_DIR,  exist_ok=True)
os.makedirs(TEMP_DIR,  exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING COM UTF-8 E TRATAMENTO ESPECIAL
# ═══════════════════════════════════════════════════════════════════════════
class UTF8Formatter(logging.Formatter):
    """Formatter que garante UTF-8 em todas as mensagens"""
    def format(self, record):
        # Força conversão de strings anormais para UTF-8
        if isinstance(record.msg, str):
            try:
                record.msg = record.msg.encode('utf-8', errors='replace').decode('utf-8')
            except:
                pass
        return super().format(record)

# Cria handler com encoding explícito
log_handler = logging.StreamHandler()
log_handler.setFormatter(UTF8Formatter(
    fmt='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(log_handler)

# Garante UTF-8 no stdout/stderr
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

BATCH_DELAY       = 0.3
RETRY_ATTEMPTS    = 3
CHUNK_SIZE        = 1024 * 256
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 2
MAX_PDF_WORKERS   = 5
AI_POLL_INTERVAL  = 300
FFMPEG_DIR        = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())

BASE_URL = "https://www.papaconcursos.com.br"
PORTAL   = f"{BASE_URL}/portal"

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'accept-charset': 'utf-8,iso-8859-1;q=0.9,*;q=0.8',  # ← Força negociação UTF-8
    'referer': f'{BASE_URL}/portal',
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# UTILITÁRIOS DE ENCODING
# ============================================================================
def safe_decode(data, fallback='utf-8'):
    """
    Decodifica bytes para string com múltiplas tentativas.
    Prioridade: UTF-8 → Windows-1252 → ISO-8859-1 → ASCII com substituição
    """
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
    
    # Último recurso
    return data.decode('utf-8', errors='replace')


def normalize_text(text):
    """
    Normaliza texto Unicode para garantir acentos corretos.
    Resolve problemas de encoding duplo e caracteres estranhos.
    """
    if not isinstance(text, str):
        text = str(text)
    
    # 1️⃣ Tenta decompor caracteres problemáticos (ő → o + ˝)
    text = unicodedata.normalize('NFD', text)
    
    # 2️⃣ Remove diacríticos estranhos que causam problemas
    # Mantém: acentos normais (á, é, í, ó, ú), cedilha (ç), til (ã, õ)
    # Remove: somente caracteres de combinação problemáticos
    cleaned = []
    for char in text:
        cat = unicodedata.category(char)
        # Mantém: letras, números, pontuação, espaço
        # Remove: marcas de combinação dupla, caracteres de controle
        if cat not in ('Mn', 'Mc', 'Cc', 'Cn'):  # Remove combining marks e controle
            cleaned.append(char)
    
    text = ''.join(cleaned)
    
    # 3️⃣ Recompõe para NFC (forma canônica)
    text = unicodedata.normalize('NFC', text)
    
    return text


def clear_name(name: str) -> str:
    """
    Limpa nomes de arquivos/pastas mantendo acentos em português.
    - Decodifica bytes corretamente
    - Normaliza Unicode
    - Remove caracteres inválidos
    """
    if not name:
        return ""
    
    # 1️⃣ Garante que temos string Unicode
    if isinstance(name, bytes):
        name = safe_decode(name)
    else:
        name = str(name)
    
    # 2️⃣ Normaliza acentos (NFC)
    name = normalize_text(name)
    
    # 3️⃣ Remove extensões
    name = re.sub(r'\.(pdf|docx|xlsx|pptx)$', '', name, flags=re.IGNORECASE)
    
    # 4️⃣ Remove APENAS caracteres inválidos no filesystem
    # Mantém: acentos, números, espaços, pontos, hífens, parênteses
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    
    # 5️⃣ Remove pontos finais extras
    name = name.rstrip('.')
    
    return name


def create_folder(path: str) -> str:
    """Cria pasta com tratamento de Unicode"""
    path = normalize_text(path)
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
        url = f"{PORTAL}/curso-aula/produto-pacote/{course_id}/{course_name}"
        
        # ═══════════════════════════════════════════════════════════════════
        # REQUISIÇÃO COM TRATAMENTO EXPLÍCITO DE ENCODING
        # ═══════════════════════════════════════════════════════════════════
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        
        # Força detecção de encoding correto
        if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
            # Tenta detectar com charset-normalizer
            try:
                from charset_normalizer import from_bytes
                detected = from_bytes(resp.content).best()
                if detected:
                    resp.encoding = str(detected.encoding)
                    logger.debug(f"Encoding detectado: {resp.encoding}")
                else:
                    resp.encoding = 'utf-8'
            except:
                resp.encoding = 'utf-8'
        
        # Decodifica conteúdo com tratamento explícito
        html_content = safe_decode(resp.content, resp.encoding or 'utf-8')
        html_content = normalize_text(html_content)

        if '<title>Login' in html_content[:500]:
            raise RuntimeError(
                "\n╔══════════════════════════════════════════════════════════╗\n"
                "║  SESSÃO EXPIRADA — cookies inválidos ou vencidos!        ║\n"
                "║  1. Abra papaconcursos.com.br no Chrome                  ║\n"
                "║  2. Faça login                                           ║\n"
                "║  3. F12 → Application → Cookies → copie JSESSIONID      ║\n"
                "║     e chave → cole na função main() do script            ║\n"
                "╚══════════════════════════════════════════════════════════╝"
            )

        # BeautifulSoup (html_content já é string Unicode normalizada)
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
            name = normalize_text(name)  # ← Normaliza antes de limpar
            
            if name:
                cleaned = clear_name(name)
                items[cleaned] = token
                logger.debug(f"  Item: '{name}' → '{cleaned}'")

        logger.info(f"  Itens raiz encontrados: {len(items)} — {list(items.keys())[:5]}")
        return items

    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict):
        logger.info(f"Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"Erro getTopico ({token}): {e}")
            return

        for sub in data.get('listTopics', []):
            parent_token = data.get('token', token)
            sub_token = f"{parent_token}-{sub['token']}"
            sub_name = normalize_text(sub.get('nome', ''))
            # Usa forward slash para compatibilidade multiplataforma
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

        # idVideo para geração de IA (transcrição / ebook)
        id_video = None
        video_to = item.get('videoTO') or {}
        if video_to:
            id_video = video_to.get('param') or video_to.get('id')

        video_url = self._get_video_url(media_token, titulo)
        materials = self._get_materials(item['token'], course_id)

        # Usa forward slash para compatibilidade
        lesson_key = f"{path}/{item_title}"
        all_links[lesson_key] = {
            'video': video_url,
            'materials': materials,
            'id_video': id_video,
        }
        
        tipos = [m['tipo'] for m in materials]
        logger.info(f"  ✓ {lesson_key} | vídeo={'sim' if video_url else 'não'} | materiais={len(materials)} {tipos}")

    def _get_video_url(self, media_token: str, titulo: str) -> str | None:
        """Obtém URL do vídeo (manifest M3U8 ou MPD) com suporte a vários formatos"""
        try:
            resp = self.session.get(
                f"{PORTAL}/media",
                params={'token': media_token},
                headers=HEADERS, 
                timeout=TIMEOUT
            )
            resp.raise_for_status()
            
            # Detecta encoding
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
                    
        except Exception as e:
            logger.debug(f"  Erro media ({titulo}): {e}")
            return None

        text = resp.text

        # 1️⃣ Procura master.m3u8 direto
        m = re.search(r'(https?://[^\s\'"<>]+/master\.m3u8[^\s\'"<>]*)', text)
        if m:
            return m.group(1)

        # 2️⃣ Procura .m3u8 genérico
        m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', text)
        if m:
            return m.group(1)

        # 3️⃣ Procura iframe videotecaead.com.br
        soup = BeautifulSoup(text, 'html.parser')
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '')
            if 'videotecaead.com.br' not in src:
                continue
            try:
                vid_resp = self.session.get(src, headers=HEADERS, timeout=TIMEOUT)
                
                # Detecta encoding no iframe também
                if not vid_resp.encoding or vid_resp.encoding.lower() == 'iso-8859-1':
                    try:
                        from charset_normalizer import from_bytes
                        detected = from_bytes(vid_resp.content).best()
                        if detected:
                            vid_resp.encoding = str(detected.encoding)
                        else:
                            vid_resp.encoding = 'utf-8'
                    except:
                        vid_resp.encoding = 'utf-8'

                # Procura M3U8 em scripts
                for script in BeautifulSoup(vid_resp.text, 'html.parser').find_all('script'):
                    if script.string:
                        mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', script.string)
                        if mo:
                            return mo.group(1)

                # Procura M3U8 no texto geral
                mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', vid_resp.text)
                if mo:
                    return mo.group(1)

                # Procura MPD (DASH)
                mo = re.search(r"const\s+manifestUrl\s*=\s*'(https?://[^']+\.mpd[^']*)'", vid_resp.text)
                if mo:
                    logger.debug(f"  ⚠ MPD (DASH) detectado para: {titulo}")
                    return mo.group(1)

            except Exception as e:
                logger.debug(f"  Erro iframe player ({titulo}): {e}")

        logger.debug(f"  ⚠ Sem URL M3U8: {titulo}")
        return None

    def _get_materials(self, topico_token: str, course_id: str) -> List[Dict]:
        """Obtém lista de materiais (PDFs, docs, transcrições) via getDocumentoTopico"""
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
            
            # Detecta encoding
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
            
            docs = resp.json()
            if not docs:
                logger.debug(f"  Nenhum material em {topico_token}")
                return []

            for doc in docs:
                tipo = doc.get('tipo', '')
                token = doc.get('token')
                nome = normalize_text(doc.get('nome', tipo))
                nome = clear_name(nome)

                # Documentos (D, L) e Transcrições (T, A-1, A-3)
                if tipo in ('D', 'L') and token:
                    url = (f"{PORTAL}/documento-online-key"
                           f"?idDocumento={token}&tipo=D&token={course_id}")
                    materials.append({'url': url, 'nome': nome, 'tipo': tipo})

                elif tipo in ('T', 'A-1', 'A-3') and token and token not in ('A-1', 'A-3'):
                    url = f"{PORTAL}/getTranscricao?token={token}&format=json"
                    materials.append({'url': url, 'nome': nome or tipo, 'tipo': tipo})
                else:
                    if tipo in ('A-1', 'A-3'):
                        logger.debug(f"  {tipo} ainda não gerado para {topico_token}")

        except requests.exceptions.JSONDecodeError as e:
            logger.debug(f"  Erro JSON getDocumentoTopico ({topico_token}): {e}")
        except Exception as e:
            logger.debug(f"  Erro getDocumentoTopico ({topico_token}): {e}")
        
        return materials

    def _get_topico(self, token: str) -> Dict:
        """Busca um tópico com tratamento de encoding"""
        url = f"{PORTAL}/getTopico"
        data_payload = {'token': token}
        
        resp = self.session.post(url, data=data_payload, headers=HEADERS, timeout=TIMEOUT)
        
        # Detecta encoding como na requisição GET
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
        """Realiza login e configura cookies"""
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.cookies.set('chave', chave)
        logger.info(f"✓ Sessão iniciada: {email}")

    def download_course(self, course_id: str, course_name: str,
                       force_rebuild: bool = False):
        """Baixa um curso completo com suporte a geração de IA"""
        collector = LinkCollector(self.session)
        all_links = collector.collect_all_links(course_id, course_name, force_rebuild)

        course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        
        video_tasks = []
        pdf_static = []
        ai_pending = []

        # ── Organizar downloads ─────────────────────────────────────────────
        for lesson_name, lesson_data in all_links.items():
            lesson_dir = create_folder(os.path.join(course_dir, lesson_name))
            
            # Vídeo
            if lesson_data.get('video'):
                video_file = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(video_file):
                    video_tasks.append((lesson_data['video'], video_file))

            # Materiais estáticos
            mat_idx = 1
            for mat in lesson_data.get('materials', []):
                url = mat.get('url', '')
                nome = mat.get('nome', 'material')
                tipo = mat.get('tipo', 'D')
                
                mat_dir = create_folder(os.path.join(lesson_dir, 'material'))
                mat_file = os.path.join(mat_dir, f"{mat_idx:03d} - {nome}.pdf")
                mat_idx += 1

                if is_valid_pdf(mat_file):
                    continue
                if os.path.exists(mat_file):
                    os.remove(mat_file)
                if url:
                    pdf_static.append((url, mat_file))

            # Registrar para geração IA
            id_video = lesson_data.get('id_video')
            if id_video:
                tipos_existentes = {m.get('tipo') for m in lesson_data.get('materials', [])}
                needs_transcricao = 'T' not in tipos_existentes and 'A-1' not in tipos_existentes
                needs_ebook = 'A-3' not in tipos_existentes

                if needs_transcricao or needs_ebook:
                    ai_pending.append({
                        'id_video': id_video,
                        'course_id': course_id,
                        'lesson_dir': lesson_dir,
                        'lesson_name': lesson_name,
                        'mat_idx': mat_idx,
                        'needs_transcricao': needs_transcricao,
                        'needs_ebook': needs_ebook,
                        'token_transcricao': None,
                        'token_ebook': None,
                    })

        # ── 1) Dispara geração IA em background ─────────────────────────────
        if ai_pending:
            logger.info(f"⚙ Disparando geração IA para {len(ai_pending)} aulas em background...")
            gen_thread = threading.Thread(
                target=self._fire_ai_generation,
                args=(ai_pending,),
                daemon=True
            )
            gen_thread.start()

        # ── 2) Baixa PDFs estáticos (já prontos) ───────────────────────────
        if pdf_static:
            logger.info(f"📥 Baixando {len(pdf_static)} materiais estáticos...")
            with ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pool:
                futures = {pool.submit(self._download_pdf, u, p): p for u, p in pdf_static}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.debug(f"Erro download PDF: {e}")
                    time.sleep(BATCH_DELAY)

        # ── 3) Baixa vídeos (com polling IA paralelo) ──────────────────────
        if video_tasks:
            logger.info(f"🎬 Baixando {len(video_tasks)} vídeos "
                        f"(polling IA a cada {AI_POLL_INTERVAL // 60} min)...")

            poll_stop = threading.Event()
            poll_thread = threading.Thread(
                target=self._ai_poll_loop,
                args=(ai_pending, poll_stop),
                daemon=True
            )
            poll_thread.start()

            with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as pool:
                futures = [pool.submit(self._download_video, u, p) for u, p in video_tasks]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.debug(f"Erro download vídeo: {e}")

            poll_stop.set()
            poll_thread.join(timeout=10)

        # ── 4) Varredura final: baixa IA que terminou ──────────────────────
        if ai_pending:
            logger.info("🔄 Varredura final de materiais IA...")
            self._poll_and_download_ai(ai_pending)

        logger.info(f"✅ Curso concluído: {course_name}")

    def _fire_ai_generation(self, ai_pending: List[dict]):
        for entry in ai_pending:
            id_video = entry['id_video']
            if entry.get('needs_transcricao'):
                tok = self._call_gerar_ai('transcricao', id_video)
                if tok:
                    entry['token_transcricao'] = tok
            if entry.get('needs_ebook'):
                tok = self._call_gerar_ai('ebook', id_video)
                if tok:
                    entry['token_ebook'] = tok
            time.sleep(0.5)

    def _call_gerar_ai(self, tipo: str, id_video: str) -> str | None:
        endpoint = {
            'transcricao': f"{PORTAL}/gerarAITranscricao",
            'ebook':       f"{PORTAL}/gerarAIEbook",
        }[tipo]
        try:
            resp = self.session.post(
                endpoint,
                data={'idVideo': id_video},
                headers=HEADERS,
                timeout=180
            )
            resp.raise_for_status()
            data = resp.json()
            tok  = data.get('token')
            if tok:
                logger.debug(f"  ⚙ {tipo} disparado id={id_video} → token={str(tok)[:16]}")
                return tok
        except Exception as e:
            logger.debug(f"  ⚠ Falha ao disparar {tipo} id={id_video}: {e}")
        return None

    def _ai_poll_loop(self, ai_pending: List[dict], stop: threading.Event):
        while not stop.wait(timeout=AI_POLL_INTERVAL):
            logger.info("⏱ Polling IA — verificando materiais prontos...")
            self._poll_and_download_ai(ai_pending)

    def _poll_and_download_ai(self, ai_pending: List[dict]):
        for entry in ai_pending:
            lesson_dir = entry['lesson_dir']
            mat_dir    = create_folder(os.path.join(lesson_dir, 'material'))
            mat_idx    = entry.get('mat_idx', 99)

            for tipo_key, nome_arquivo in [
                ('token_transcricao', 'Transcrição IA'),
                ('token_ebook',       'Ebook IA'),
            ]:
                tok = entry.get(tipo_key)
                if not tok:
                    continue

                mat_file = os.path.join(mat_dir, f"{mat_idx:03d} - {nome_arquivo}.pdf")
                if is_valid_pdf(mat_file):
                    continue

                url = f"{PORTAL}/getTranscricao?token={tok}&format=json"
                if self._download_pdf(url, mat_file):
                    logger.info(f"  ✓ IA pronto: {nome_arquivo} → {os.path.basename(lesson_dir)}")
                    entry['mat_idx'] = mat_idx + 1
                    mat_idx = entry['mat_idx']

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
                logger.info(f"✓ Vídeo: {os.path.basename(output_path)}")
            else:
                logger.warning(f"⚠ Arquivo inválido: {manifest_url[:70]}")

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
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue

                content_type = resp.headers.get('content-type', '').lower()

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
                    except Exception:
                        pass

                if resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    return True

            except Exception as e:
                logger.warning(f"Tentativa {attempt + 1} PDF: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"✗ Falha PDF: {os.path.basename(output_path)}")
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
        logger.info(f"\n{'='*60}\nINICIANDO: {course['name']}\n{'='*60}")
        downloader.download_course(course['id'], course['name'], force_rebuild=False)

if __name__ == "__main__":
    main()
