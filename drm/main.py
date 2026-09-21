import subprocess, sys

def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"])

print("Instalando dependências...")
for pkg in ['beautifulsoup4', 'requests', 'yt-dlp', 'imageio-ffmpeg', 'pycryptodome']:
    install(pkg)

import pathlib, os, re, json, time, logging, requests, shutil
import video_merge_util
import threading, base64, unicodedata
from urllib.parse import quote
from bs4 import BeautifulSoup
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── ffmpeg via imageio ────────────────────────────────────────────────────────
import imageio_ffmpeg
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_DIR = os.path.dirname(ffmpeg_exe)
# Garante que o ffmpeg do imageio esteja no PATH para o yt-dlp encontrar
os.environ['PATH'] = FFMPEG_DIR + os.pathsep + os.environ.get('PATH', '')

# ============================================================================
# CONFIGURAÇÕES
# ============================================================================
BASE_DIR = pathlib.Path(r'D:/papaplata')
BASE_URL = "https://www.papaconcursos.com.br"
PORTAL   = f"{BASE_URL}/portal"
TEMP_DIR = BASE_DIR / 'temp'
CACHE_DIR = BASE_DIR

# Ensure directories exist
for d in [BASE_DIR, TEMP_DIR, CACHE_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

TIMEOUT           = 30
RETRY_ATTEMPTS    = 3
RETRY_ATTEMPTS_AI = 8   # PDFs de IA demoram para processar — mais tentativas com backoff maior
CHUNK_SIZE        = 1024 * 256
BATCH_DELAY       = 0.3
MAX_VIDEO_WORKERS = 2
MAX_PDF_WORKERS   = 5
AI_POLL_INTERVAL  = 300   # 5 minutos

# Duplicate BASE_URL definition removed – already set above
PORTAL   = f"{BASE_URL}/portal"

# Token KeyOS da plataforma (válido até 2034)
KEYOS_AUTH = (
    "PEtleU9TQXV0aGVudGljYXRpb25YTUw+CjxEYXRhPgogIDxHZW5lcmF0aW9uVGltZT4yMDI2LTA3LT"
    "IzIDE2OjIwOjMyLjQ1ODwvR2VuZXJhdGlvblRpbWU+CiAgPEV4cGlyYXRpb25UaW1lPjIwMzQtMTAt"
    "MDkgMTY6MjA6MzIuNDU4PC9FeHBprocGF0aW9uVGltZT4KICA8VW5pcXVlSWQ+YWIwM2IyYzY0NmZkYz"
    "M4NDU3ODdmY2JmMGYwNWFjMjE8L1VuaXF1ZUlkPgogIDxSU0FQdWJLZXlJZD44YTc2OWIxMzEyNzlk"
    "ZDkyNzQ3YjEzNTBkNzFiMWMwMjwvUlNBUHViS2V5SWQ+CiAgPFdpZGV2aW5lUG9saWN5IGZsX0Nhbl"
    "BsYXk9InRydWUiIGZsX0NhblBlcnNpc3Q9ImZhbHNlIiAvPgogIDxXaWRldmluZUNvbnRlbnRLZXlT"
    "cGVjIFRyYWNrVHlwZT0iSEQiPgogICAgPFNlY3VyaXR5TGV2ZWw+MTwvU2VjdXJpdHlMZXZlbD4K"
    "ICA8L1dpZGV2aW5lQ29udGVudEtleVNwZWM+CiAgPEZhaXJQbGF5UG9saWN5IHBlcnNpc3RlbnQ9Im"
    "ZhbHNlIiAvPgogIDxMaWNlbnNlIHR5cGU9InNpbXBsZSIgLz4KPC9EYXRhPgo8U2lnbmF0dXJlPlcx"
    "SmwxSjNFT0ZxZHlTMUU5Y2FzWWF2ZVF5Yk1BZHdOTitpajhTNDZOTWQ3OHJEU3FLaGIxN2lUME1Jd"
    "2srTTNQa0hUWGdjSGVUL0NVTnhNZXU3a3RZZlorbWdNMXZIZG9VbitIRExnZktDa2EySmNmVlV2Qy8v"
    "cEZvWWxkOUFvLzl3ajJjWnZpckRvRFJHeVFiemZUYnZrREZpNTFVMk9wZ0NJYzErdDJXQmpSTWEyR3I"
    "2QW13OVRDeW83b2gyYk9uYWZLTGhpQlpHbWFqcUk1Y3lsZlFyaWttYXc2aTNFdTZTeFcwb3MyTXhwSD"
    "JDTFNpNkFnVFp3SkEwc1NJejJJWXFkNVFMQUU5cG9aTGxYNC9ndmJCNHlnZ1NxTnlMM2FaTzlITFVl"
    "RjVmNWlYNklZQWFEb3VHdWZORGE0ZGtReERpeWozaXMrWXg1UjlONnZtNmlUZz09PC9TaWduYXR1cmU"
    "+CjwvS2V5T1NBdXRoZW50aWNhdGlvblhNTD4="
)

HEADERS = {
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'referer': f'{BASE_URL}/portal',
    'x-requested-with': 'XMLHttpRequest'
}

# ============================================================================
# UTILS
# ============================================================================
def clear_name(n: str) -> str:
    """
    Corrige nomes para uso em arquivos/pastas:
    - Corrige mojibake cp1252→utf-8
    - Mantém acentos corretamente
    - Remove caracteres proibidos no Windows/Linux
    - Remove extensão .pdf duplicada
    """
    try:
        n = n.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    # Normaliza mantendo acentos
    nfkc = unicodedata.normalize('NFKC', n)

    # Remove caracteres inválidos
    limpo = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', nfkc)
    limpo = limpo.strip().rstrip('.')

    # Remove extensão duplicada .pdf
    if limpo.lower().endswith('.pdf'):
        limpo = limpo[:-4].rstrip()

    return limpo


def rename_if_needed(path: str) -> str:
    """
    Se já existe um arquivo/pasta com nome errado, renomeia para o correto.
    """
    dir_name, base_name = os.path.split(path)
    clean_base = clear_name(base_name)
    clean_path = os.path.join(dir_name, clean_base)

    if path != clean_path and os.path.exists(path):
        try:
            os.rename(path, clean_path)
        except Exception:
            pass

    return clean_path


def create_folder(p: str) -> str:
    """
    Cria uma pasta com nome limpo.
    Se já existir com nome errado, renomeia para o correto.
    """
    parts = [clear_name(part) for part in pathlib.Path(p).parts]
    clean_path = os.path.join(*parts)

    original_path = os.path.join(*pathlib.Path(p).parts)
    if os.path.exists(original_path) and original_path != clean_path:
        try:
            os.rename(original_path, clean_path)
        except Exception:
            pass

    os.makedirs(clean_path, exist_ok=True)
    return clean_path


def is_valid_pdf(p: str) -> bool:
    """
    Verifica se o arquivo é um PDF válido.
    Se o nome estiver errado, renomeia antes de validar.
    """
    p = rename_if_needed(p)
    if not os.path.exists(p):
        return False
    try:
        with open(p, 'rb') as f:
            return f.read(5).startswith(b'%PDF')
    except Exception:
        return False


def is_video_complete(p: str) -> bool:
    """
    Verifica se o vídeo existe e tem tamanho mínimo (>1MB).
    Se o nome estiver errado, renomeia antes de validar.
    """
    p = rename_if_needed(p)
    return os.path.exists(p) and os.path.getsize(p) > 1024 * 1024

# ============================================================================
# LINK COLLECTOR
# ============================================================================
class LinkCollector:
    def __init__(self, session: requests.Session):
        self.session = session

    def collect_all_links(self, course_id: str, course_name: str,
                          force_rebuild: bool = False) -> Dict:
        cache_file = os.path.join(CACHE_DIR, f"{course_id}_links.json")
        if os.path.exists(cache_file):
            if force_rebuild:
                logger.warning(f"force_rebuild — descartando cache: {course_name}")
                os.remove(cache_file)
            else:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if cached:
                    logger.info(f"Cache: {course_name} ({len(cached)} aulas)")
                    return cached
                os.remove(cache_file)

        logger.info(f"Coletando: {course_name}")
        root_items = self._get_root_items(course_id, course_name)
        if not root_items:
            raise RuntimeError(f"Sessão expirada ou course_id incorreto para '{course_name}'")

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links)

        if not all_links:
            raise RuntimeError(f"0 aulas coletadas para '{course_name}' — possível sessão expirada")

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(all_links, f, ensure_ascii=False, indent=2)
        logger.info(f"Cache salvo: {len(all_links)} aulas")
        return all_links

    def _get_root_items(self, course_id: str, course_name: str) -> Dict[str, str]:
        url  = f"{PORTAL}/curso-aula/produto-pacote/{course_id}/{course_name}"
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        if '<title>Login' in resp.text[:500]:
            raise RuntimeError(
                "\n╔══════════════════════════════════════════════════════════╗\n"
                "║  SESSÃO EXPIRADA!  F12 → Application → Cookies          ║\n"
                "║  Copie JSESSIONID e chave → cole no main()              ║\n"
                "╚══════════════════════════════════════════════════════════╝"
            )
        soup  = BeautifulSoup(resp.content, 'html.parser')
        items = {}

        # Nova plataforma: div.header-wrapper com data-target
        for div in soup.find_all('div', class_=lambda c: c and 'header-wrapper' in c and 'primary' in c):
            target = div.get('data-target', '')
            m = re.search(r'#(item-[a-f0-9]+-[a-f0-9]+)$', target)
            if not m: continue
            h3 = div.find('h3')
            if not h3: continue
            for badge in h3.find_all('span', class_='badge'): badge.decompose()
            name = h3.get_text(strip=True)
            if name: items[name] = m.group(1)

        # Fallback: plataforma antiga com li onclick
        if not items:
            for li in soup.find_all('li', class_=lambda c: c and 'item-tree' in c):
                text  = li.get_text(strip=True).split('Disponível')[0].strip()
                parts = li.get('onclick', '').split("'")
                token = parts[1] if len(parts) > 1 else None
                if token: items[text] = token

        logger.info(f"  Itens raiz: {len(items)}")
        return items

    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict):
        logger.info(f"Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"Erro getTopico ({token}): {e}")
            return

        parent_token_from_api = data.get('token', token)

        for sub in data.get('listTopics', []):
            sub_token = f"{parent_token_from_api}-{sub['token']}"
            self._recurse(sub_token, course_id,
                          os.path.join(path, clear_name(sub['nome'])), all_links)

        for item in data.get('listTopicsMedia', []):
            self._process_media_item(item, parent_token_from_api, course_id, path, all_links)

    def _process_media_item(self, item: dict, parent_token: str,
                            course_id: str, path: str, all_links: Dict):
        titulo     = item.get('titulo', 'sem-titulo')
        item_title = clear_name(titulo)
        media_token = f"{parent_token}-{item['token']}"

        video_to = item.get('videoTO') or {}
        id_video = video_to.get('param') or video_to.get('id')

        video_url, is_drm = self._get_video_url(media_token, titulo)
        materials = self._get_materials(item['token'], course_id)

        lesson_key = os.path.join(path, item_title)
        all_links[lesson_key] = {
            'video':     video_url,
            'is_drm':    is_drm,
            'id_video':  id_video,
            'materials': materials,
        }
        drm_tag = ' [DRM]' if is_drm else ''
        logger.info(f"  ✓ {lesson_key} | vídeo={'sim'+drm_tag if video_url else 'não'} | mats={len(materials)}")

    def _get_video_url(self, media_token: str, titulo: str) -> Tuple[Optional[str], bool]:
        """Retorna (url, is_drm). Tenta player antigo como fallback para MPD."""
        try:
            resp = self.session.get(f"{PORTAL}/media",
                                    params={'token': media_token},
                                    headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Erro media ({titulo}): {e}")
            return None, False

        text = resp.text

        # 1) m3u8 direto no texto (sem DRM)
        m = re.search(r'(https?://[^\s\'"<>]+/master\.m3u8[^\s\'"<>]*)', text)
        if m: return m.group(1), False
        m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', text)
        if m: return m.group(1), False

        soup = BeautifulSoup(resp.content, 'html.parser')
        VIDEO_DOMAINS = ('videotecaead.com.br', 'embed.videotecaead.com.br',
                         'player.videotecaead.com.br', 'videoteca')

        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '')
            if not any(d in src for d in VIDEO_DOMAINS): continue
            try:
                vid_resp = self.session.get(src, headers=HEADERS, timeout=TIMEOUT)
                vid_soup = BeautifulSoup(vid_resp.content, 'html.parser')

                # m3u8 nos scripts
                for script in vid_soup.find_all('script'):
                    mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', script.string or '')
                    if mo: return mo.group(1), False

                # m3u8 no texto completo
                mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', vid_resp.text)
                if mo: return mo.group(1), False

                # Player DRM — extrai .mpd e tenta player antigo como fallback
                mo = re.search(r"const\s+manifestUrl\s*=\s*'(https?://[^']+\.mpd[^']*)'", vid_resp.text)
                if mo:
                    mpd_url = mo.group(1)
                    logger.debug(f"  .mpd DRM encontrado: {mpd_url[:80]}")

                    title_m = re.search(r'<title>([^<]+)</title>', vid_resp.text)
                    if title_m:
                        raw_title = title_m.group(1).replace('.mp4', '').strip()
                        slug = self._title_to_slug(raw_title)
                        old_url = f"https://embed.videotecaead.com.br/papaconcursos/{slug}"
                        logger.debug(f"  Tentando player antigo: {old_url}")
                        try:
                            old_resp = self.session.get(old_url, headers=HEADERS, timeout=TIMEOUT)
                            if old_resp.status_code == 200:
                                for s in BeautifulSoup(old_resp.content, 'html.parser').find_all('script'):
                                    moo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                                    s.string or '')
                                    if moo:
                                        logger.info(f"  ✓ Player antigo (sem DRM): {moo.group(1)[:60]}")
                                        return moo.group(1), False
                                moo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', old_resp.text)
                                if moo: return moo.group(1), False
                        except Exception as e2:
                            logger.debug(f"  Erro player antigo: {e2}")

                    logger.warning(f"  ⚠ DRM PlayReady — usará KeyOS: {mpd_url[:70]}")
                    return mpd_url, True

            except Exception as e:
                logger.warning(f"  Erro iframe ({titulo}): {e}")

        return None, False

    def _get_materials(self, topico_token: str, course_id: str) -> List[dict]:
        materials = []
        try:
            resp = self.session.get(
                f"{PORTAL}/getDocumentoTopico",
                params={'format': 'json', 'token': topico_token, 'tokenCurso': course_id,
                        'flagAI': '0-1-1-1-1-1-1-1-1-1-1-1-1-1-1-', 'flagTranscricao': '1'},
                headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            for doc in (resp.json() or []):
                tipo  = doc.get('tipo', '')
                token = doc.get('token')
                nome  = clear_name(doc.get('nome', tipo))
                token_invalido = not token or str(token).strip() in ('', 'null', 'None', tipo)

                if tipo in ('D', 'L') and token:
                    materials.append({
                        'url':  f"{PORTAL}/documento-online-key?idDocumento={token}&tipo=D&token={course_id}",
                        'nome': nome, 'tipo': tipo
                    })
                elif tipo == 'T' and token:
                    # FIX: format=json faz a API retornar {"url":"/path/to/file.pdf"}
                    # sem esse parâmetro a API retorna HTML ou redireciona incorretamente
                    materials.append({
                        'url':  f"{PORTAL}/getTranscricao?format=json&token={token}",
                        'nome': nome or 'Transcricao', 'tipo': tipo
                    })
                elif tipo == 'A-1':
                    if not token_invalido:
                        # FIX: transcrição IA já gerada usa getTranscricao com format=json
                        materials.append({
                            'url':  f"{PORTAL}/getTranscricao?format=json&token={token}",
                            'nome': nome or 'Transcricao IA', 'tipo': tipo
                        })
                    else:
                        materials.append({
                            'needs_generation': True,
                            'ai_type': 'transcricao',
                            'endpoint': f"{PORTAL}/gerarAITranscricao",
                            'nome': clear_name('Transcricao IA'), 'tipo': tipo,
                            'token': None
                        })
                        logger.debug(f"  A-1 pendente para {topico_token}")
                elif tipo == 'A-3':
                    if not token_invalido:
                        # FIX: ebook IA já gerado usa getEbookAI (endpoint diferente de transcrição)
                        materials.append({
                            'url':  f"{PORTAL}/getEbookAI?token={token}",
                            'nome': nome or 'Ebook IA', 'tipo': tipo
                        })
                    else:
                        materials.append({
                            'needs_generation': True,
                            'ai_type': 'ebook',
                            'endpoint': f"{PORTAL}/gerarAIEbook",
                            'nome': clear_name('Ebook IA'), 'tipo': tipo,
                            'token': None
                        })
                        logger.debug(f"  A-3 pendente para {topico_token}")
        except Exception as e:
            logger.debug(f"  Erro getDocumentoTopico ({topico_token}): {e}")
        return materials

    def _title_to_slug(self, title: str) -> str:
        nfkd   = unicodedata.normalize('NFKD', title)
        ascii_ = nfkd.encode('ASCII', 'ignore').decode().upper()
        clean  = re.sub(r'[^A-Z0-9\s\-]', '', ascii_)
        parts  = [p.strip() for p in clean.split('-') if p.strip()]
        return '-'.join(p.replace(' ', '_') for p in parts)

    def _get_topico(self, token: str) -> dict:
        resp = self.session.get(f"{PORTAL}/getTopico",
                                params={'format': 'json', 'token': token},
                                headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
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

    # ------------------------------------------------------------------
    def download_course(self, course_id: str, course_name: str):
        course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        collector  = LinkCollector(self.session)
        try:
            links = collector.collect_all_links(course_id, course_name, force_rebuild=False)
        except RuntimeError as e:
            logger.error(f"✗ {e}")
            return

        logger.info(f"Iniciando: {len(links)} aulas — {course_name}")

        video_tasks: List[Tuple[str, str, bool]] = []
        pdf_tasks:   List[Tuple[str, str]] = []
        ai_pending:  List[dict] = []

        for rel_path, data in links.items():
            lesson_dir = create_folder(os.path.join(course_dir, rel_path))

            if data.get('video'):
                vf = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(vf):
                    video_tasks.append((data['video'], vf, data.get('is_drm', False)))
                else:
                    logger.info(f"Vídeo OK: {rel_path}")

            mat_dir = create_folder(os.path.join(lesson_dir, 'material'))
            mat_idx = 1
            for mat in data.get('materials', []):
                if isinstance(mat, str):
                    mat_file = os.path.join(mat_dir, f"{mat_idx:03d} - material.pdf")
                    mat_idx += 1
                    mat_file = rename_if_needed(mat_file)
                    if is_valid_pdf(mat_file): continue
                    pdf_tasks.append((mat, mat_file))
                    continue

                if mat.get('needs_generation'):
                    if data.get('id_video'):
                        ai_pending.append({
                            'id_video':   data['id_video'],
                            'ai_type':    mat['ai_type'],
                            'endpoint':   mat['endpoint'],
                            'course_id':  course_id,
                            'lesson_dir': lesson_dir,
                            'mat_dir':    mat_dir,
                            'nome':       clear_name(mat['nome']),
                            'mat_idx':    mat_idx,
                            'token':      None,
                        })
                    mat_idx += 1
                    continue

                url  = mat.get('url', '')
                nome = clear_name(mat.get('nome', 'material'))
                mat_file = os.path.join(mat_dir, f"{mat_idx:03d} - {nome}.pdf")
                mat_idx += 1
                mat_file = rename_if_needed(mat_file)
                if is_valid_pdf(mat_file): continue
                if os.path.exists(mat_file): os.remove(mat_file)
                pdf_tasks.append((url, mat_file))

        # ── 1) Dispara geração IA em background ─────────────────────────
        if ai_pending:
            logger.info(f"Disparando geração IA para {len(ai_pending)} itens...")
            t = threading.Thread(target=self._fire_ai_generation, args=(ai_pending,), daemon=True)
            t.start()

        # ── 2) PDFs estáticos em paralelo ───────────────────────────────
        if pdf_tasks:
            logger.info(f"Baixando {len(pdf_tasks)} PDFs estáticos...")
            with ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pool:
                futs = {pool.submit(self._download_pdf, u, p): p for u, p in pdf_tasks}
                for f in as_completed(futs):
                    if f.result(): logger.info(f"PDF OK: {os.path.basename(futs[f])}")
                    time.sleep(BATCH_DELAY)

        # ── 3) Vídeos + polling IA a cada 5 min ─────────────────────────
        if video_tasks:
            logger.info(f"Baixando {len(video_tasks)} vídeos...")
            stop_poll = threading.Event()
            poll_t = threading.Thread(target=self._ai_poll_loop,
                                      args=(ai_pending, stop_poll), daemon=True)
            poll_t.start()

            with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as pool:
                futs = [pool.submit(self._download_video, u, p, drm)
                        for u, p, drm in video_tasks]
                for f in as_completed(futs): f.result()

            stop_poll.set()
            poll_t.join(timeout=10)

        # ── 4) Varredura final IA ────────────────────────────────────────
        if ai_pending:
            logger.info("Varredura final IA...")
            self._poll_and_download_ai(ai_pending)

        logger.info(f"✅ Concluído: {course_name}")

    # ------------------------------------------------------------------
    def _fire_ai_generation(self, ai_pending: List[dict]):
        for entry in ai_pending:
            id_video = entry['id_video']
            endpoint = entry['endpoint']
            nome     = entry['nome']
            try:
                resp = self.session.post(endpoint,
                                         data={'idVideo': id_video},
                                         headers=HEADERS, timeout=180)
                tok = resp.json().get('token')
                entry['token'] = tok
                logger.debug(f"  ⚙ {nome} gerado — token={tok} (id={id_video})")
            except Exception as e:
                logger.debug(f"  ⚠ Falha geração {nome}: {e}")
            time.sleep(0.5)

    def _ai_poll_loop(self, ai_pending: List[dict], stop: threading.Event):
        while not stop.wait(timeout=AI_POLL_INTERVAL):
            logger.info("⏱ Polling IA...")
            self._poll_and_download_ai(ai_pending)

    def _poll_and_download_ai(self, ai_pending: List[dict]):
        for entry in ai_pending:
            tok = entry.get('token')
            if not tok:
                try:
                    resp = self.session.post(entry['endpoint'],
                                             data={'idVideo': entry['id_video']},
                                             headers=HEADERS, timeout=180)
                    tok = resp.json().get('token')
                    if tok:
                        entry['token'] = tok
                        logger.debug(f"  ⚙ {entry['nome']} token obtido no polling: {tok}")
                except Exception:
                    pass
            if not tok:
                continue

            mat_dir  = entry.get('mat_dir') or create_folder(
                os.path.join(entry['lesson_dir'], 'material'))
            nome     = clear_name(entry.get('nome', 'IA'))
            idx      = entry.get('mat_idx', 99)
            mat_file = os.path.join(mat_dir, f"{idx:03d} - {nome}.pdf")
            mat_file = rename_if_needed(mat_file)
            if is_valid_pdf(mat_file): continue

            # FIX: ebook IA (A-3) usa getEbookAI; transcrição (A-1, T) usa getTranscricao
            ai_type = entry.get('ai_type', 'transcricao')
            if ai_type == 'ebook':
                url = f"{PORTAL}/getEbookAI?token={tok}"
            else:
                url = f"{PORTAL}/getTranscricao?format=json&token={tok}"
            if self._download_pdf(url, mat_file, retries=RETRY_ATTEMPTS_AI):
                logger.info(f"  ✓ IA: {nome} → {os.path.basename(entry['lesson_dir'])}")

    # ------------------------------------------------------------------
    def _download_video(self, manifest_url: str, output_path: str, is_drm: bool = False):
        import glob as _glob

        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_base = os.path.join(str(TEMP_DIR), rel_path)          # sem extensão
        temp_outtmpl = temp_base + '.%(ext)s'                       # ex: .../001 - aula.mp4
        os.makedirs(os.path.dirname(temp_base), exist_ok=True)

        try:
            ydl_opts = {
                'ffmpeg_location':               FFMPEG_DIR,
                'format':                        'bv*+ba/b',
                'outtmpl':                       temp_outtmpl,
                'quiet':                         True,
                'no_warnings':                   True,
                'retries':                       RETRY_ATTEMPTS,
                'concurrent_fragment_downloads': 4,
                'socket_timeout':                TIMEOUT,
                'merge_output_format':           'mp4',
            }

            if is_drm:
                ydl_opts['http_headers'] = {'x-keyos-authorization': KEYOS_AUTH}
                ydl_opts['allow_unplayable_formats'] = True

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([manifest_url])

            candidates = [
                f for f in _glob.glob(temp_base + '.*')
                if not f.endswith('.part') and not f.endswith('.ytdl')
                   and os.path.isfile(f)
            ]
            mp4_candidates = [f for f in candidates if f.lower().endswith('.mp4')]
            downloaded = (mp4_candidates or candidates or [None])[0]
            if downloaded and not mp4_candidates:
                downloaded = max(candidates, key=os.path.getsize)

            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 100 * 1024:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.move(downloaded, output_path)
                logger.info(f"✓ Vídeo: {os.path.basename(output_path)}")
                # Merge separate video/audio files if both exist
                try:
                    import video_merge_util
                    video_merge_util.merge_if_needed(os.path.dirname(output_path))
                except Exception as e:
                    logger.warning(f"Merge failed for {output_path}: {e}")
            else:
                logger.warning(f"⚠ Arquivo inválido ou não encontrado: {manifest_url[:60]}")
                if not candidates:
                    logger.debug(f"  Glob não encontrou nada em: {temp_base}.*")

        except Exception as e:
            err = str(e)[:120]
            if 'ffmpeg' in err.lower():
                logger.error(f"Falha ffmpeg (FFMPEG_DIR={FFMPEG_DIR}): {err}")
            elif 'drm' in err.lower() or 'encrypted' in err.lower() or 'widevine' in err.lower():
                logger.error(f"🔒 DRM ATIVO: {os.path.basename(output_path)}")
            else:
                logger.error(f"Falha vídeo: {err}")

    def _download_pdf(self, url: str, output_path: str, retries: int = RETRY_ATTEMPTS) -> bool:
        output_path = rename_if_needed(output_path)
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=TIMEOUT, verify=False)
                if resp.status_code != 200:
                    logger.debug(f"  PDF status {resp.status_code} (tentativa {attempt+1})")
                    time.sleep(2 ** attempt)
                    continue

                ct = resp.headers.get('content-type', '').lower()

                if 'application/json' in ct or 'text/plain' in ct or \
                   (resp.content[:1] in (b'{', b'[')):
                    try:
                        data = resp.json()
                    except Exception:
                        time.sleep(2 ** attempt)
                        continue

                    if not isinstance(data, dict):
                        time.sleep(2 ** attempt)
                        continue

                    status = str(data.get('status', '')).lower()
                    if status in ('processing', 'pending', 'gerando'):
                        logger.debug(f"  IA ainda processando (tentativa {attempt+1}): {os.path.basename(output_path)}")
                        time.sleep(max(10, 2 ** attempt))
                        continue

                    if not data.get('url'):
                        logger.debug(f"  JSON sem 'url': {str(data)[:80]}")
                        time.sleep(2 ** attempt)
                        continue

                    raw_url = data['url']
                    # FIX: data['url'] pode ser relativo ("/portal/...") ou absoluto
                    if raw_url.startswith('http'):
                        pdf_url = raw_url
                    else:
                        pdf_url = f"{BASE_URL}{quote(raw_url, safe=':/?=&')}"
                    pdf_resp = self.session.get(pdf_url, stream=True, timeout=60, verify=False,
                                                allow_redirects=True)
                    content  = pdf_resp.content
                    if pdf_resp.status_code == 200 and content.startswith(b'%PDF'):
                        with open(temp_path, 'wb') as f:
                            f.write(content)
                        shutil.move(temp_path, output_path)
                        return True
                    logger.debug(f"  PDF URL retornou {pdf_resp.status_code}, não é PDF: {pdf_url[:80]}")
                    time.sleep(2 ** attempt)

                elif resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    return True

                else:
                    preview = resp.content[:80]
                    logger.debug(f"  Resposta inesperada (tentativa {attempt+1}): ct={ct} preview={preview}")
                    time.sleep(2 ** attempt)

            except Exception as e:
                logger.warning(f"PDF tentativa {attempt+1}: {e}")
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
        jsessionid="DB7D627C2B33FB0F201B03B07EF14B4A",
        chave="3707c478ec414cb1f68e450ee08635d6a745eb8470d07937ff07f3ae9e90ff99421ac2b83469babfb5fcff2ea1214fe8b182dfba77df26614c95776aa27642a0"
    )

    courses = [

        # ISOLADAS
        {"id": "6f7a5134b435ccf84a972485bbd89fdf", "name": "isolada-direito-constitucional"},
        {"id": "927e039b5965fe89845e6468bd154b63", "name": "isolada-direito-administrativo"},
        {"id": "b2786a4a77348a6d85a3945a51fb6462", "name": "isolada-direito-civil"},
        {"id": "d46a1ccb6c598ce7124459cf9dccc613", "name": "isolada-direito--processual-do-trabalho"},
        {"id": "7a6d5a08cdda488baa1b56e8fbeb6ad0", "name": "isolada-administracao-financeira-e-orcamentaria"},
        {"id": "e8957556a65795d109fb499db86210ea", "name": "isolada-direito-ambiental"},
        {"id": "d18e8aff4db3e025a3596e42c1b3b1ed", "name": "isolada-direito-previdenciario"},
        {"id": "a0bbc1292e6ab81dabff5e2d1fbf83cf", "name": "isolada-direito-penal"},
        {"id": "1e00353aff5113662810e224c564c20c", "name": "isolada-direito-do-trabalho"},
        {"id": "6d4c35e2e7ea656cb4c5527fcbe0dcb5", "name": "isolada-direito-da-pessoa-com-deficiencia"},
        {"id": "f26285f6e7ba4844d3bc1783cb528f4b", "name": "isolada-direito-processual-penal"},
        {"id": "1786beaf94a74265bf5b6415efa40c63", "name": "isolada-direito-tributario"},
        {"id": "1ae41acd8ae3e1ebfdb202b7d80b7b41", "name": "isolada-direitos-humanos"},
        {"id": "0d495aa132cbc54bbfff254fcc3d6caa", "name": "isolada-informatica"},
        {"id": "75d383ec676afc3d6a5d6c57b0972d4b", "name": "isolada-lei-8112-90-novo"},
        {"id": "1556f7b5b9ac48e8f4000e78fc85149c", "name": "isolada-leis-penais-especiais"},
        {"id": "7eaa6558bfbd18f6f696484ad5b404e6", "name": "isolada-direito-processual-civil"},
        {"id": "73d8395bd4281fa19b9b1e7065dfb34a", "name": "isolada-raciocinio-logico-matematico"},

        # COMPLETOS
        {"id": "eb5b92c6017e8a03bce8ba532646be93", "name": "projeto-enam-2026"},
        {"id": "f98b2cf0852633f5b6f82aeb4bc6f046", "name": "novo-projeto-tj"},
        {"id": "469c72429c91520758f9e39f023c0c85", "name": "novo-projeto-tre"},
        {"id": "eba238e1bf1e1cc7ddec99284a011e85", "name": "novo-projeto-trf"},
        {"id": "d186b982663037d8d90b232c4cc257d1", "name": "projeto-trt-(novo)"},
    ]

    for course in courses:
        logger.info(f"\n{'='*60}\nINICIANDO: {course['name']}\n{'='*60}")
        downloader.download_course(course['id'], course['name'])


if __name__ == "__main__":
    main()