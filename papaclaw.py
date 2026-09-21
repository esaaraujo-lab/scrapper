# -*- coding: utf-8 -*-
"""
================================================================================
 PAPA CONCURSOS DOWNLOADER — VERSÃO CONSOLIDADA (main.py)
================================================================================
Combina as MELHORES funções dos 6 scripts analisados:

  • psantos.py            -> Selenium + cloudscraper, login automático,
                             renovação de sessão, Widevine DRM (KeyOS),
                             e-book IA em Markdown, cross-platform
                             (Colab/Windows/Linux), cookies Netscape.
  • gemini.py             -> Encoding UTF-8 robusto (charset-normalizer),
                             coleta via API getDocumentoTopico, threading
                             de geração IA com polling.
  • papa_concursos.2026.py-> Mesma base do gemini.py (v3) com IA threading.
  • ISOLADAS 2026.py      -> Fallback de player DRM antigo via slug, lista
                             de cursos isolados, detecção dupla de root items.
  • papa.py               -> Base simples, formato Colab, fallback .mpd.
  • papa2026.py           -> Downloads paralelos (vídeos + PDFs),
                             yt-dlp com ffmpeg_location, código estável.

CORREÇÕES IMPORTANTES EM RELAÇÃO AOS ORIGINAIS:
  1. normalize_text() NÃO destrói mais acentos (bug do v3/v4 que removia
     combining marks e transformava "Transcrição" em "Transcrieco").
  2. Detecção dupla de root items: tenta `header-wrapper primary` (layout novo)
     e depois `<li class="item-tree">` (layout antigo) — funciona nos dois.
  3. DRM é OPCIONAL: só ativa se `device.wvd` + token KeyOS existirem.
  4. Autenticação DUPLA: cookies manuais (JSESSIONID/chave) OU Selenium.
  5. yt-dlp sempre com `ffmpeg_location` (evita warnings AAC/MPEG-TS).

USO:
  python main.py                       # baixa cursos listados em COURSES
  python main.py --selenium           # login via Selenium (renova cookies)
  python main.py --course ID NOME      # baixa um curso específico
  python main.py --list-courses        # lista cursos da Área do Aluno
  python main.py --force-rebuild       # ignora cache de links
================================================================================
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import unicodedata
import subprocess
import threading
import pathlib
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================================
# 1. INSTALAÇÃO AUTOMÁTICA DE DEPENDÊNCIAS
#    (unificado de psantos.py + gemini.py + ISOLADAS 2026.py)
# ============================================================================
def instalar_dependencias():
    """Instala silenciosamente todos os pacotes necessários."""
    pacotes = [
        'beautifulsoup4',
        'requests',
        'urllib3',
        'yt-dlp',
        'imageio-ffmpeg',
        'charset-normalizer',
    ]
    # Opcionais (pesados) — só importam para Selenium/DRM
    pacotes_opcionais = {
        'selenium':           'selenium',
        'webdriver-manager':  'webdriver_manager',
        'cloudscraper':       'cloudscraper',
        'pycryptodome':       'Crypto',
        'protobuf':           'google.protobuf',
        'pywidevine':         'pywidevine',
    }

    def _install(pkg):
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"  ⚠ Falha ao instalar {pkg}: {e}")

    print("📦 Verificando dependências...")
    for pkg in pacotes:
        _install(pkg)

    # Tenta instalar os opcionais sem abortar se falhar
    for pkg, mod_name in pacotes_opcionais.items():
        try:
            __import__(mod_name)
        except ImportError:
            try:
                _install(pkg)
            except Exception:
                pass

instalar_dependencias()

# Imports principais
import requests
import yt_dlp
import urllib3
import imageio_ffmpeg
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Imports opcionais (Selenium, DRM)
try:
    import cloudscraper
    CLOUDSCRAPER_AVAILABLE = True
except ImportError:
    CLOUDSCRAPER_AVAILABLE = False

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.service import Service
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import Pssh
    WIDEVINE_AVAILABLE = True
except ImportError:
    WIDEVINE_AVAILABLE = False

# Garante ffmpeg no PATH (do ISOLADAS 2026.py + papa2026.py)
FFMPEG_DIR = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
os.environ['PATH'] = FFMPEG_DIR + os.pathsep + os.environ.get('PATH', '')

# ============================================================================
# 2. CROSS-PLATFORM ENVIRONMENT (de psantos.py)
# ============================================================================
class EnvironmentSetup:
    @staticmethod
    def get_env() -> str:
        if 'google.colab' in sys.modules or os.path.exists('/content'):
            return 'colab'
        elif sys.platform.startswith('win'):
            return 'windows'
        elif sys.platform.startswith('linux'):
            return 'linux'
        elif sys.platform.startswith('darwin'):
            return 'macos'
        return 'unknown'

    @classmethod
    def install_system_dependencies(cls, env: str):
        """Instala binários de sistema (FFmpeg/Chromium) no Linux/Colab."""
        if env in ('colab', 'linux'):
            try:
                subprocess.run(['sudo', 'apt-get', 'update', '-y'],
                               check=False, stdout=subprocess.DEVNULL)
                subprocess.run(
                    ['sudo', 'apt-get', 'install', '-y',
                     'ffmpeg', 'wget', 'curl', 'unzip'],
                    check=False, stdout=subprocess.DEVNULL
                )
                if env == 'colab':
                    subprocess.run(
                        ['apt-get', 'install', '-y',
                         'chromium-chromedriver', 'google-chrome-stable'],
                        check=False, stdout=subprocess.DEVNULL
                    )
            except Exception as e:
                print(f"⚠ Aviso ao instalar deps de sistema: {e}")

CURRENT_ENV = EnvironmentSetup.get_env()
EnvironmentSetup.install_system_dependencies(CURRENT_ENV)

# ============================================================================
# 3. CONFIGURAÇÕES E DIRETÓRIOS
# ============================================================================
# Diretório base por ambiente (de psantos.py)
if CURRENT_ENV == 'colab':
    BASE_DIR = Path('/content/PapaConcursos_Downloads')
elif CURRENT_ENV == 'windows':
    BASE_DIR = Path.cwd() / 'PapaConcursos_Downloads'
else:
    BASE_DIR = Path.home() / 'PapaConcursos_Downloads'

TEMP_DIR  = BASE_DIR / 'temp'
CACHE_DIR = BASE_DIR / 'cache'
EBOOKS_DIR = BASE_DIR / 'ebooks'
LOGS_DIR  = BASE_DIR / 'logs'

for d in (BASE_DIR, TEMP_DIR, CACHE_DIR, EBOOKS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# URLs
BASE_URL = "https://www.papaconcursos.com.br"
PORTAL_NEW = "https://portal2025.papaconcursos.com.br/portal"   # layout novo
PORTAL_OLD = f"{BASE_URL}/portal"                                # layout antigo

# Constantes operacionais (de papa2026.py + ISOLADAS 2026.py)
BATCH_DELAY        = 0.3
RETRY_ATTEMPTS     = 3
CHUNK_SIZE         = 1024 * 256
TIMEOUT            = 30
MAX_VIDEO_WORKERS  = 2
MAX_PDF_WORKERS    = 5
AI_POLL_INTERVAL   = 300   # 5 min

HEADERS = {
    'user-agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/125.0.0.0 Safari/537.36'),
    'accept-charset': 'utf-8,iso-8859-1;q=0.9,*;q=0.8',
    'referer': f'{BASE_URL}/portal',
    'x-requested-with': 'XMLHttpRequest',
}

# ============================================================================
# 4. LOGGING COM UTF-8 (de papa_concursos.2026.py)
# ============================================================================
class UTF8Formatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, str):
            try:
                record.msg = record.msg.encode('utf-8', errors='replace').decode('utf-8')
            except Exception:
                pass
        return super().format(record)

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(UTF8Formatter(
    fmt='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

_file_handler = logging.FileHandler(LOGS_DIR / "papa_downloader.log", encoding='utf-8')
_file_handler.setFormatter(UTF8Formatter(
    fmt='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

logger = logging.getLogger("PapaConcursos")
logger.setLevel(logging.INFO)
logger.addHandler(_log_handler)
logger.addHandler(_file_handler)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ============================================================================
# 5. UTILITÁRIOS DE ENCODING — VERSÃO CORRIGIDA
#    (de gemini.py/papa_concursos.2026.py, com bug do normalize_text corrigido)
# ============================================================================
def safe_decode(data, fallback: str = 'utf-8') -> str:
    """Decodifica bytes testando múltiplos encodings."""
    if isinstance(data, str):
        return data
    if not isinstance(data, bytes):
        return str(data)
    for enc in ('utf-8', 'utf-8-sig', 'windows-1252', 'iso-8859-1', 'cp1252'):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return data.decode('utf-8', errors='replace')


def fix_mojibake(text: str) -> str:
    """Corrige dupla codificação UTF-8 (mojibake) tipo 'Ã©' -> 'é'."""
    if not isinstance(text, str):
        return text
    try:
        # Tenta re-interpretar como latin-1 e decodificar como UTF-8
        fixed = text.encode('latin-1', errors='ignore').decode('utf-8', errors='ignore')
        # Heurística: se a versão corrigida tem mais chars acentuados válidos, use-a
        if fixed and fixed != text:
            accented_orig = sum(1 for c in text if unicodedata.category(c).startswith('L') and ord(c) > 127)
            accented_fixed = sum(1 for c in fixed if unicodedata.category(c).startswith('L') and ord(c) > 127)
            if accented_fixed >= accented_orig:
                return fixed
    except Exception:
        pass
    return text


def normalize_text(text) -> str:
    """
    Normaliza Unicode preservando acentos do português.

    CORREÇÃO: ao contrário do v3/v4 original, NÃO removemos combining marks
    (Mn, Mc) — isso destruía acentos ("Transcrição" virava "Transcrieco").
    Em vez disso: aplica NFC direto + corrige mojibake.
    """
    if not isinstance(text, str):
        text = str(text)

    # Corrige mojibake primeiro (dupla codificação)
    text = fix_mojibake(text)

    # Normaliza para forma canônica composta (preserva acentos)
    text = unicodedata.normalize('NFC', text)

    # Remove apenas caracteres de controle invisíveis (preserva acentos)
    text = ''.join(
        c for c in text
        if unicodedata.category(c) not in ('Cc', 'Cf') or c in '\t\n\r'
    )

    return unicodedata.normalize('NFC', text)


def clear_name(name: str, maxlen: int = 120) -> str:
    """Limpa nomes de arquivos/pastas mantendo acentos."""
    if not name:
        return 'recurso'
    if isinstance(name, bytes):
        name = safe_decode(name)
    name = normalize_text(name)
    # Remove extensões conhecidas
    name = re.sub(r'\.(pdf|docx?|xlsx?|pptx?|mp4|mkv|webm)$', '', name, flags=re.IGNORECASE)
    # Remove apenas caracteres inválidos em filesystem
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()
    name = re.sub(r'\s+', ' ', name)
    return name[:maxlen].rstrip(' .') or 'recurso'


def create_folder(path: str) -> str:
    path = normalize_text(str(path))
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


def is_video_complete(path: str, min_size: int = 1024 * 1024) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > min_size


# ============================================================================
# 6. GERENCIADOR DE SESSÃO / AUTENTICAÇÃO
#    (unifica cookies manuais de papa2026.py + Selenium de psantos.py)
# ============================================================================
class AuthManager:
    """
    Autenticação dupla:
      • Modo cookies: JSESSIONID + chave (rápido, mas expira)
      • Modo Selenium: login via navegador headless (renova cookies)
    """

    def __init__(self, use_selenium: bool = False,
                 email: str = None, password: str = None,
                 jsessionid: str = None, chave: str = None):
        self.use_selenium = use_selenium or (SELENIUM_AVAILABLE and email and password)
        self.email = email
        self.password = password
        self.jsessionid = jsessionid
        self.chave = chave

        if CLOUDSCRAPER_AVAILABLE:
            self.session = cloudscraper.create_scraper()
        else:
            self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update(HEADERS)

        self.driver = None
        self.driver_lock = threading.Lock()
        self.cookies_file = TEMP_DIR / 'netscape_cookies.txt'

    # ---- Login por cookies -------------------------------------------------
    def login_cookies(self, email: str, jsessionid: str, chave: str):
        self.session.cookies.set('email', email)
        self.session.cookies.set('JSESSIONID', jsessionid)
        self.session.cookies.set('chave', chave)
        logger.info(f"✓ Sessão iniciada via cookies: {email}")

    # ---- Login via Selenium (de psantos.py) --------------------------------
    def login_selenium(self) -> bool:
        if not SELENIUM_AVAILABLE:
            logger.error("❌ Selenium não disponível. Instale: pip install selenium webdriver-manager")
            return False

        with self.driver_lock:
            self._init_driver()
            try:
                logger.info(f"🔐 Login via Selenium em [{CURRENT_ENV.upper()}]...")
                self.driver.get(f"{BASE_URL}/login")
                time.sleep(3)

                if 'login' in self.driver.current_url.lower():
                    email_input = self.driver.find_element(By.NAME, 'email')
                    pass_input = self.driver.find_element(By.NAME, 'password')
                    email_input.clear()
                    email_input.send_keys(self.email)
                    pass_input.clear()
                    pass_input.send_keys(self.password)
                    pass_input.send_keys(Keys.RETURN)
                    time.sleep(5)

                if 'login' in self.driver.current_url.lower():
                    logger.error("❌ Falha no login Selenium. Verifique credenciais.")
                    return False

                # Sincroniza User-Agent
                ua = self.driver.execute_script("return navigator.userAgent;")
                self.session.headers.update({'User-Agent': ua, 'Referer': BASE_URL})

                # Copia cookies para a requests.Session
                cookies = self.driver.get_cookies()
                self._save_netscape_cookies(cookies)
                for ck in cookies:
                    self.session.cookies.set(ck['name'], ck['value'], domain=ck.get('domain', ''))

                logger.info("✅ Login Selenium OK e cookies sincronizados.")
                return True
            except Exception as e:
                logger.error(f"❌ Erro login Selenium: {e}")
                return False

    def _init_driver(self):
        opts = Options()
        opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=1920,1080')
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

        if CURRENT_ENV == 'colab':
            opts.binary_location = '/usr/bin/google-chrome'
            self.driver = webdriver.Chrome(options=opts)
        else:
            chrome_bin = shutil.which('google-chrome') or shutil.which('chrome')
            if chrome_bin:
                opts.binary_location = chrome_bin
            try:
                self.driver = webdriver.Chrome(options=opts)
            except Exception as e:
                logger.warning(f"⚠ Selenium Manager falhou ({e}); tentando webdriver-manager...")
                from webdriver_manager.chrome import ChromeDriverManager
                driver_path = ChromeDriverManager().install()
                if sys.platform.startswith('win') and not driver_path.lower().endswith('.exe'):
                    base_dir = (os.path.dirname(driver_path) if os.path.isfile(driver_path)
                                else driver_path)
                    for root, _, files in os.walk(base_dir):
                        for f in files:
                            if f.lower() == 'chromedriver.exe':
                                driver_path = os.path.join(root, f)
                                break
                self.driver = webdriver.Chrome(service=Service(driver_path), options=opts)

        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })

    def _save_netscape_cookies(self, cookies: list):
        """Salva cookies em formato Netscape (necessário para yt-dlp)."""
        with open(self.cookies_file, 'w', encoding='utf-8') as f:
            f.write('# Netscape HTTP Cookie File\n')
            for c in cookies:
                domain = c.get('domain', '')
                flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = c.get('path', '/')
                secure = 'TRUE' if c.get('secure', False) else 'FALSE'
                exp = str(int(c.get('expiry', time.time() + 86400)))
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{exp}\t{c.get('name')}\t{c.get('value')}\n")

    def ensure_valid_session(self) -> bool:
        """Verifica/renova sessão (de psantos.py)."""
        try:
            resp = self.session.get(f"{BASE_URL}/portal/api/user/info",
                                    timeout=10, verify=False, allow_redirects=False)
            if resp.status_code in (200, 302) and 'login' not in resp.headers.get('Location', '').lower():
                return True
        except Exception:
            pass

        if self.use_selenium and self.email and self.password:
            logger.warning("⚠ Sessão expirada. Renovando via Selenium...")
            return self.login_selenium()

        logger.error("❌ Sessão expirada. Renove os cookies manualmente.")
        return False

    def close(self):
        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None


# ============================================================================
# 7. COLETOR DE LINKS — DETECÇÃO DUPLA + FALLBACK PLAYER ANTIGO
#    (une gemini.py + ISOLADAS 2026.py)
# ============================================================================
class LinkCollector:
    def __init__(self, session: requests.Session, auth: AuthManager = None):
        self.session = session
        self.auth = auth

    # ---- Entrada ----------------------------------------------------------
    def collect_all_links(self, course_id: str, course_name: str,
                          force_rebuild: bool = False) -> Dict:
        cache_file = CACHE_DIR / f"{course_id}_links.json"
        if cache_file.exists():
            if force_rebuild:
                logger.warning(f"force_rebuild=True — descartando cache: {course_name}")
                cache_file.unlink()
            else:
                try:
                    cached = json.loads(cache_file.read_text(encoding='utf-8'))
                    if cached:
                        logger.info(f"📦 Cache: {course_name} ({len(cached)} aulas)")
                        return cached
                except Exception:
                    cache_file.unlink()

        logger.info(f"🔍 Coletando links: {course_name}")
        root_items = self._get_root_items(course_id, course_name)

        if not root_items:
            raise RuntimeError(
                f"Sessão expirada ou course_id/name incorreto: '{course_name}'. "
                f"Renove os cookies/Selenium."
            )

        all_links: Dict = {}
        for item_name, item_token in root_items.items():
            self._recurse(item_token, course_id, clear_name(item_name), all_links)

        if not all_links:
            raise RuntimeError(f"Nenhuma aula coletada para '{course_name}'.")

        cache_file.write_text(
            json.dumps(all_links, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        logger.info(f"💾 Cache salvo: {len(all_links)} aulas")
        return all_links

    # ---- Detecta root items (duas estratégias) ----------------------------
    def _get_root_items(self, course_id: str, course_name: str) -> Dict[str, str]:
        url = f"{PORTAL_NEW}/curso-aula/produto-pacote/{course_id}/{course_name}"
        resp = self.session.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        resp.raise_for_status()

        # Detecção inteligente de encoding (de papa_concursos.2026.py)
        if not resp.encoding or resp.encoding.lower() == 'iso-8859-1':
            try:
                from charset_normalizer import from_bytes
                detected = from_bytes(resp.content).best()
                resp.encoding = str(detected.encoding) if detected else 'utf-8'
            except Exception:
                resp.encoding = 'utf-8'

        html = safe_decode(resp.content, resp.encoding or 'utf-8')
        html = normalize_text(html)

        if '<title>Login' in html[:500]:
            raise RuntimeError(
                "SESSÃO EXPIRADA — cookies inválidos ou vencidos! "
                "Renove JSESSIONID/chave ou use --selenium."
            )

        soup = BeautifulSoup(html, 'html.parser')
        items: Dict[str, str] = {}

        # Estratégia 1: layout novo (de papa_concursos.2026.py)
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
            name = normalize_text(h3.get_text(strip=True))
            if name:
                items[clear_name(name)] = token

        # Estratégia 2: layout antigo (de ISOLADAS 2026.py + papa.py)
        if not items:
            for li in soup.find_all('li', class_=lambda c: c and 'item-tree' in c):
                text = li.get_text(strip=True).split('Disponível')[0].strip()
                onclick = li.get('onclick', '')
                parts = onclick.split("'")
                token = parts[1] if len(parts) > 1 else None
                if token:
                    items[clear_name(normalize_text(text))] = token

        logger.info(f"  Itens raiz: {len(items)} — {list(items.keys())[:5]}")
        return items

    # ---- Recursão (token dinâmico do v3/v4) ------------------------------
    def _recurse(self, token: str, course_id: str, path: str, all_links: Dict):
        logger.info(f"📂 Explorando: {path}")
        try:
            data = self._get_topico(token)
        except Exception as e:
            logger.warning(f"  ⚠ Erro getTopico ({token}): {e}")
            return

        parent_token = data.get('token', token)

        # Sub-tópicos intermediários
        for sub in data.get('listTopics', []):
            sub_token = f"{parent_token}-{sub['token']}"
            sub_name = normalize_text(sub.get('nome', ''))
            self._recurse(sub_token, course_id,
                          f"{path}/{clear_name(sub_name)}", all_links)

        # Aulas com mídia
        for item in data.get('listTopicsMedia', []):
            self._process_media_item(item, parent_token, course_id, path, all_links)

    def _process_media_item(self, item: dict, parent_token: str,
                            course_id: str, path: str, all_links: Dict):
        titulo = normalize_text(item.get('titulo', 'sem-titulo'))
        item_title = clear_name(titulo)
        media_token = f"{parent_token}-{item['token']}"

        # idVideo para geração IA (de papa_concursos.2026.py)
        id_video = None
        video_to = item.get('videoTO') or {}
        if video_to:
            id_video = video_to.get('param') or video_to.get('id')

        video_url = self._get_video_url(media_token, titulo)
        materials = self._get_materials(item['token'], course_id)

        lesson_key = f"{path}/{item_title}"
        all_links[lesson_key] = {
            'video': video_url,
            'materials': materials,
            'id_video': id_video,
        }
        tipos = [m['tipo'] for m in materials]
        logger.info(f"  ✓ {lesson_key} | vídeo={'✓' if video_url else '✗'} | "
                    f"{len(materials)} mat {tipos}")

    # ---- Busca de URL de vídeo (de ISOLADAS 2026.py com fallback slug) ----
    def _get_video_url(self, media_token: str, titulo: str) -> Optional[str]:
        try:
            resp = self.session.get(
                f"{PORTAL_NEW}/media",
                params={'token': media_token}, headers=HEADERS,
                timeout=TIMEOUT, verify=False
            )
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"    ⚠ Erro media ({titulo}): {e}")
            return None

        text = safe_decode(resp.content, resp.encoding or 'utf-8')
        text = normalize_text(text)
        soup = BeautifulSoup(text, 'html.parser')

        # 1) master.m3u8 direto no HTML
        m = re.search(r'(https?://[^\s\'"<>]+/master\.m3u8[^\s\'"<>]*)', text)
        if m:
            return m.group(1)

        # 2) .m3u8 genérico
        m = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', text)
        if m:
            return m.group(1)

        # 3) iframes da videotecaead (com fallback player antigo via slug)
        VIDEO_DOMAINS = ('videotecaead.com.br', 'embed.videotecaead.com.br',
                         'player.videotecaead.com.br')
        candidate_iframes = [
            f for f in soup.find_all('iframe')
            if any(d in (f.get('src') or '') for d in VIDEO_DOMAINS)
        ]

        for iframe in candidate_iframes:
            iframe_src = (iframe.get('src') or '').strip()
            if not iframe_src:
                continue
            try:
                vid_resp = self.session.get(iframe_src, headers=HEADERS,
                                            timeout=TIMEOUT, verify=False)
                vid_text = safe_decode(vid_resp.content, vid_resp.encoding or 'utf-8')

                # 3a) m3u8 em <script>
                for script in BeautifulSoup(vid_text, 'html.parser').find_all('script'):
                    mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                   script.string or '')
                    if mo:
                        return mo.group(1)

                # 3b) m3u8 no texto
                mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', vid_text)
                if mo:
                    return mo.group(1)

                # 3c) Player DRM (.mpd) -> tenta player antigo via slug
                mpd_m = re.search(
                    r"const\s+manifestUrl\s*=\s*'(https?://[^']+\.mpd[^']*)'", vid_text
                )
                if mpd_m:
                    mpd_url = mpd_m.group(1)
                    title_m = re.search(r'<title>([^<]+)</title>', vid_text)
                    if title_m:
                        raw_title = title_m.group(1).replace('.mp4', '').strip()
                        slug = self._title_to_slug(raw_title)
                        old_url = f"https://embed.videotecaead.com.br/papaconcursos/{slug}"
                        try:
                            old_resp = self.session.get(old_url, headers=HEADERS,
                                                        timeout=TIMEOUT, verify=False)
                            if old_resp.status_code == 200:
                                for script in BeautifulSoup(old_resp.content, 'html.parser').find_all('script'):
                                    mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                                   script.string or '')
                                    if mo:
                                        logger.info(f"  ✓ m3u8 via player antigo: {mo.group(1)[:80]}")
                                        return mo.group(1)
                                mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                               old_resp.text)
                                if mo:
                                    return mo.group(1)
                        except Exception:
                            pass
                    # Último recurso: .mpd mesmo (yt-dlp + DRM resolver)
                    logger.warning(f"  ⚠ Usando .mpd (DRM): {mpd_url[:80]}")
                    return mpd_url
            except Exception as e:
                logger.debug(f"    ⚠ Erro iframe {iframe_src[:60]}: {e}")

        return None

    @staticmethod
    def _title_to_slug(title: str) -> str:
        """Converte título em slug do player antigo (de ISOLADAS 2026.py)."""
        nfkd = unicodedata.normalize('NFKD', title)
        ascii_str = nfkd.encode('ASCII', 'ignore').decode('ASCII').upper()
        clean = re.sub(r'[^A-Z0-9\s\-]', '', ascii_str)
        parts = [p.strip() for p in clean.split('-') if p.strip()]
        return '-'.join(p.replace(' ', '_') for p in parts)

    # ---- Materiais via getDocumentoTopico (de papa_concursos.2026.py) ------
    def _get_materials(self, topico_token: str, course_id: str) -> List[Dict]:
        materials = []
        try:
            resp = self.session.get(
                f"{PORTAL_NEW}/getDocumentoTopico",
                params={
                    'format': 'json',
                    'token': topico_token,
                    'tokenCurso': course_id,
                    'flagAI': '0-1-1-1-1-1-1-1-1-1-1-1-1-1-1-',
                    'flagTranscricao': '1',
                },
                headers=HEADERS, timeout=TIMEOUT, verify=False
            )
            resp.raise_for_status()
            docs = resp.json()
            if not docs:
                return []

            for doc in docs:
                tipo = doc.get('tipo', '')
                token = doc.get('token')
                nome = clear_name(normalize_text(doc.get('nome', tipo)))

                # Documentos (D, L)
                if tipo in ('D', 'L') and token:
                    url = (f"{PORTAL_NEW}/documento-online-key"
                           f"?idDocumento={token}&tipo=D&token={course_id}")
                    materials.append({'url': url, 'nome': nome, 'tipo': tipo})

                # Transcrições/ebooks IA já gerados (T, A-1, A-3)
                elif tipo in ('T', 'A-1', 'A-3') and token and token not in ('A-1', 'A-3'):
                    url = f"{PORTAL_NEW}/getTranscricao?token={token}&format=json"
                    materials.append({'url': url, 'nome': nome or tipo, 'tipo': tipo})
        except Exception as e:
            logger.debug(f"    ⚠ Erro getDocumentoTopico ({topico_token}): {e}")
        return materials

    # ---- getTopico com POST (de papa_concursos.2026.py) -------------------
    def _get_topico(self, token: str) -> Dict:
        resp = self.session.post(
            f"{PORTAL_NEW}/getTopico",
            data={'token': token}, headers=HEADERS, timeout=TIMEOUT, verify=False
        )
        resp.raise_for_status()
        return resp.json()


# ============================================================================
# 8. RESOLVER DRM WIDEVINE (KeyOS) — de psantos.py
# ============================================================================
class KeyosResolver:
    """Resolve chaves DRM Widevine via servidor KeyOS (opcional)."""

    def __init__(self, session: requests.Session,
                 wvd_path: str = 'device.wvd', token: str = ''):
        self.session = session
        self.wvd_path = Path(wvd_path)
        self.token = token
        self.key_cache: Dict[str, str] = {}

    def available(self) -> bool:
        return (WIDEVINE_AVAILABLE
                and self.wvd_path.exists()
                and bool(self.token))

    def fetch_drm_key(self, mpd_url: str) -> Optional[str]:
        if not self.available():
            return None
        if mpd_url in self.key_cache:
            return self.key_cache[mpd_url]

        try:
            resp = self.session.get(mpd_url, verify=False, timeout=15)
            soup = BeautifulSoup(resp.content, 'xml')
            pssh_elem = soup.find('cenc:pssh')
            if not pssh_elem:
                return None

            pssh = Pssh(pssh_elem.text.strip())
            device = Device.load(str(self.wvd_path))
            cdm = Cdm.from_device(device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh)

            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Content-Type': 'application/octet-stream',
                'customdata': self.token,
            }
            lic_resp = self.session.post(
                'https://playready.keyos.com/api/v4/getLicense',
                data=challenge, headers=headers, verify=False, timeout=15
            )
            lic_resp.raise_for_status()

            cdm.parse_license(session_id, lic_resp.content)
            keys = [f"{k.kid.hex()}:{k.key.hex()}"
                    for k in cdm.get_keys(session_id) if k.type == 'OPERATIONAL']
            cdm.close(session_id)

            if keys:
                self.key_cache[mpd_url] = keys[0]
                logger.info(f"🔑 Chave DRM: {keys[0][:20]}...")
                return keys[0]
        except Exception as e:
            logger.error(f"❌ Falha ao resolver DRM: {e}")
        return None


# ============================================================================
# 9. GERADOR DE E-BOOK IA (Markdown) — de psantos.py
# ============================================================================
class EbookAiGenerator:
    @staticmethod
    def generate_ebook(lesson_dir: Path, lesson_name: str,
                       ai_data: Dict[str, str]):
        if not any(ai_data.values()):
            return
        ebook_file = lesson_dir / f"Ebook_IA_{clear_name(lesson_name)}.md"
        content = [
            f"# E-BOOK COMPLETO DA AULA: {lesson_name}\n",
            f"*Gerado em {time.strftime('%d/%m/%Y %H:%M:%S')}*\n",
            "---\n",
        ]
        if ai_data.get('summary'):
            content += ["## 📝 RESUMO DA AULA\n", f"{ai_data['summary']}\n\n---\n"]
        if ai_data.get('flashcards'):
            content += ["## 🎴 FLASHCARDS DE REVISÃO\n", f"{ai_data['flashcards']}\n\n---\n"]
        if ai_data.get('mindmap'):
            content += ["## 🧠 MAPA MENTAL\n", f"{ai_data['mindmap']}\n\n---\n"]
        if ai_data.get('transcription'):
            content += ["## 🎙️ TRANSCRIÇÃO COMPLETA\n", f"{ai_data['transcription']}\n"]
        ebook_file.write_text('\n'.join(content), encoding='utf-8')
        logger.info(f"📘 E-book gerado: {ebook_file.name}")


# ============================================================================
# 10. DOWNLOADER PRINCIPAL — unifica papa2026.py + papa_concursos.2026.py
# ============================================================================
class PapaDownloader:
    def __init__(self, auth: AuthManager,
                 wvd_path: str = None, keyos_token: str = None):
        self.auth = auth
        self.session = auth.session
        self.cookies_file = auth.cookies_file
        self.drm_resolver = KeyosResolver(
            self.session, wvd_path or 'device.wvd', keyos_token or ''
        )
        self.ebook_gen = EbookAiGenerator()
        self.generate_ai = False   # habilitar via flag

    # ---- Loop principal de curso ------------------------------------------
    def download_course(self, course_id: str, course_name: str,
                        force_rebuild: bool = False):
        collector = LinkCollector(self.session, self.auth)
        all_links = collector.collect_all_links(course_id, course_name, force_rebuild)

        course_dir = create_folder(str(BASE_DIR / clear_name(course_name)))

        video_tasks: List[Tuple[str, str, str]] = []   # (url, output, lesson_key)
        pdf_tasks:   List[Tuple[str, str]] = []
        ai_pending:  List[Dict] = []

        # Organiza downloads
        for lesson_name, lesson_data in all_links.items():
            lesson_dir = create_folder(os.path.join(course_dir, lesson_name))

            # Vídeo
            if lesson_data.get('video'):
                video_file = os.path.join(lesson_dir, '001 - aula.mp4')
                if not is_video_complete(video_file):
                    video_tasks.append((lesson_data['video'], video_file, lesson_name))
                else:
                    logger.info(f"Vídeo já existe: {lesson_name}")

            # Materiais estáticos
            mat_idx = 1
            for mat in lesson_data.get('materials', []):
                url  = mat.get('url', '')
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

            # Registro para geração IA (se habilitado)
            id_video = lesson_data.get('id_video')
            if id_video and self.generate_ai:
                tipos_existentes = {m.get('tipo') for m in lesson_data.get('materials', [])}
                needs_transcricao = 'T' not in tipos_existentes
                needs_ebook = 'A-3' not in tipos_existentes
                if needs_transcricao or needs_ebook:
                    ai_pending.append({
                        'id_video': id_video,
                        'lesson_dir': lesson_dir,
                        'lesson_name': lesson_name,
                        'mat_idx': mat_idx,
                        'needs_transcricao': needs_transcricao,
                        'needs_ebook': needs_ebook,
                        'token_transcricao': None,
                        'token_ebook': None,
                    })

        logger.info(f"\n⬇️  {course_name}: "
                    f"{len(video_tasks)} vídeos | {len(pdf_tasks)} PDFs | "
                    f"{len(ai_pending)} IA pendentes")

        # 1) Dispara geração IA em background
        if ai_pending:
            logger.info(f"⚙ Disparando geração IA para {len(ai_pending)} aulas...")
            threading.Thread(target=self._fire_ai_generation,
                             args=(ai_pending,), daemon=True).start()

        # 2) PDFs estáticos em paralelo
        if pdf_tasks:
            with ThreadPoolExecutor(max_workers=MAX_PDF_WORKERS) as pool:
                futures = {pool.submit(self._download_pdf, u, p): p for u, p in pdf_tasks}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.debug(f"  PDF error: {e}")
                    time.sleep(BATCH_DELAY)

        # 3) Vídeos em paralelo (com polling IA opcional)
        if video_tasks:
            poll_stop = None
            poll_thread = None
            if ai_pending:
                poll_stop = threading.Event()
                poll_thread = threading.Thread(
                    target=self._ai_poll_loop, args=(ai_pending, poll_stop), daemon=True
                )
                poll_thread.start()

            with ThreadPoolExecutor(max_workers=MAX_VIDEO_WORKERS) as pool:
                futures = [pool.submit(self._download_video, u, p, k)
                           for u, p, k in video_tasks]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.debug(f"  Vídeo error: {e}")

            if poll_stop:
                poll_stop.set()
                if poll_thread:
                    poll_thread.join(timeout=10)

        # 4) Varredura final IA
        if ai_pending:
            logger.info("🔄 Varredura final de materiais IA...")
            self._poll_and_download_ai(ai_pending)

        logger.info(f"✅ Curso concluído: {course_name}")

    # ---- Geração IA (de papa_concursos.2026.py) ---------------------------
    def _fire_ai_generation(self, ai_pending: List[dict]):
        for entry in ai_pending:
            if entry.get('needs_transcricao'):
                tok = self._call_gerar_ai('transcricao', entry['id_video'])
                if tok:
                    entry['token_transcricao'] = tok
            if entry.get('needs_ebook'):
                tok = self._call_gerar_ai('ebook', entry['id_video'])
                if tok:
                    entry['token_ebook'] = tok
            time.sleep(0.5)

    def _call_gerar_ai(self, tipo: str, id_video: str) -> Optional[str]:
        endpoint = {
            'transcricao': f"{PORTAL_NEW}/gerarAITranscricao",
            'ebook':       f"{PORTAL_NEW}/gerarAIEbook",
        }[tipo]
        try:
            resp = self.session.post(endpoint, data={'idVideo': id_video},
                                     headers=HEADERS, timeout=180, verify=False)
            resp.raise_for_status()
            data = resp.json()
            tok = data.get('token')
            if tok:
                logger.debug(f"  ⚙ {tipo} disparado id={id_video} → {str(tok)[:16]}")
                return tok
        except Exception as e:
            logger.debug(f"  ⚠ Falha ao disparar {tipo}: {e}")
        return None

    def _ai_poll_loop(self, ai_pending: List[dict], stop: threading.Event):
        while not stop.wait(timeout=AI_POLL_INTERVAL):
            logger.info("⏱ Polling IA — verificando materiais prontos...")
            self._poll_and_download_ai(ai_pending)

    def _poll_and_download_ai(self, ai_pending: List[dict]):
        for entry in ai_pending:
            lesson_dir = Path(entry['lesson_dir'])
            mat_dir = create_folder(os.path.join(str(lesson_dir), 'material'))
            mat_idx = entry.get('mat_idx', 99)

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
                url = f"{PORTAL_NEW}/getTranscricao?token={tok}&format=json"
                if self._download_pdf(url, mat_file):
                    logger.info(f"  ✓ IA pronto: {nome_arquivo} → {lesson_dir.name}")
                    entry['mat_idx'] = mat_idx + 1
                    mat_idx = entry['mat_idx']

    # ---- Download vídeo (de papa2026.py + psantos.py para DRM) ------------
    def _download_video(self, manifest_url: str, output_path: str, lesson_key: str):
        rel_path = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        # Tenta resolver DRM se for .mpd
        key_pair = None
        if manifest_url.endswith('.mpd') or '.mpd' in manifest_url:
            key_pair = self.drm_resolver.fetch_drm_key(manifest_url)
            if key_pair:
                logger.info(f"  🔓 DRM resolvido para: {lesson_key}")
            else:
                logger.warning(f"  ⚠ DRM sem chave — tentando mesmo assim: {lesson_key}")

        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': temp_path,
            'quiet': True,
            'no_warnings': True,
            'retries': RETRY_ATTEMPTS,
            'concurrent_fragment_downloads': 5,
            'socket_timeout': TIMEOUT,
            'ffmpeg_location': FFMPEG_DIR,
            'merge_output_format': 'mp4',
        }

        # Cookies Netscape para yt-dlp (de psantos.py)
        if self.cookies_file.exists():
            ydl_opts['cookiefile'] = str(self.cookies_file)

        if key_pair:
            ydl_opts['decryption_key'] = key_pair
            ydl_opts['allow_unplayable_formats'] = True

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([manifest_url])

            # yt-dlp pode adicionar extensão
            downloaded = temp_path
            if not os.path.exists(downloaded):
                for ext in ('.mp4', '.mkv', '.webm', '.m4v'):
                    if os.path.exists(downloaded + ext):
                        downloaded = downloaded + ext
                        break

            if os.path.exists(downloaded) and os.path.getsize(downloaded) > 100 * 1024:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.move(downloaded, output_path)
                logger.info(f"  ✓ Vídeo: {os.path.basename(output_path)}")
            else:
                logger.warning(f"  ⚠ Arquivo inválido: {lesson_key}")
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ('drm', 'encrypted', 'widevine')):
                logger.error(f"  🔒 DRM ativo (sem chave): {lesson_key}")
            else:
                logger.error(f"  ✗ Vídeo {lesson_key}: {str(e)[:120]}")

    # ---- Download PDF (de papa2026.py) ------------------------------------
    def _download_pdf(self, url: str, output_path: str) -> bool:
        rel_path = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
                if resp.status_code != 200:
                    time.sleep(2 ** attempt)
                    continue

                content_type = resp.headers.get('content-type', '').lower()

                # JSON com URL interna
                if 'application/json' in content_type or resp.content.startswith(b'{'):
                    try:
                        data = resp.json()
                        target_url = None
                        if isinstance(data, dict):
                            target_url = (data.get('url') or data.get('link')
                                         or data.get('path'))
                        if target_url:
                            if not target_url.startswith('http'):
                                target_url = f"{BASE_URL}{quote(target_url, safe=':/')}"
                            pdf_resp = self.session.get(target_url, stream=True,
                                                        timeout=60, verify=False)
                            if (pdf_resp.status_code == 200
                                    and pdf_resp.content.startswith(b'%PDF')):
                                with open(temp_path, 'wb') as f:
                                    f.write(pdf_resp.content)
                                shutil.move(temp_path, output_path)
                                return True
                    except Exception:
                        pass

                # PDF direto
                if resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    return True

                time.sleep(2 ** attempt)
            except Exception as e:
                logger.debug(f"  PDF retry {attempt + 1}: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"  ✗ PDF: {os.path.basename(output_path)}")
        # Registra falha
        with open(LOGS_DIR / 'failed_downloads.log', 'a', encoding='utf-8') as f:
            f.write(f"{url}\n")
        return False

    def close(self):
        self.auth.close()


# ============================================================================
# 11. CLI / MAIN
# ============================================================================
def parse_args():
    p = argparse.ArgumentParser(description='Papa Concursos Downloader Consolidado')
    p.add_argument('--selenium', action='store_true',
                   help='Usa login via Selenium (renova cookies automaticamente)')
    p.add_argument('--email', help='Email (modo Selenium)')
    p.add_argument('--password', help='Senha (modo Selenium)')
    p.add_argument('--jsessionid', help='Cookie JSESSIONID (modo cookies)')
    p.add_argument('--chave', help='Cookie chave (modo cookies)')
    p.add_argument('--course-id', help='ID de um curso específico')
    p.add_argument('--course-name', help='Nome (slug) de um curso específico')
    p.add_argument('--list-courses', action='store_true',
                   help='Lista cursos da Área do Aluno (requer Selenium)')
    p.add_argument('--force-rebuild', action='store_true',
                   help='Ignora cache de links')
    p.add_argument('--generate-ai', action='store_true',
                   help='Dispara geração IA (transcrição/ebook) em background')
    p.add_argument('--wvd', help='Caminho do arquivo device.wvd (DRM Widevine)')
    p.add_argument('--keyos-token', help='Token KeyOS para DRM')
    return p.parse_args()


# Lista padrão de cursos (de ISOLADAS 2026.py + papa.py)
DEFAULT_COURSES = [
    # Completos / Projetos
    {"id": "f98b2cf0852633f5b6f82aeb4bc6f046", "name": "novo-projeto-tj"},
    {"id": "469c72429c91520758f9e39f023c0c85", "name": "novo-projeto-tre"},
    {"id": "eba238e1bf1e1cc7ddec99284a011e85", "name": "novo-projeto-trf"},
    {"id": "d186b982663037d8d90b232c4cc257d1", "name": "projeto-trt-(novo)"},
    {"id": "8305cb076c642a7161be4948951797be", "name": "projeto-enam-2026"},
    # Isoladas
    {"id": "7eaa6558bfbd18f6f696484ad5b404e6", "name": "isolada-direito-processual-civil"},
    {"id": "acb8c21948c052842af01f106ee5c872", "name": "isolada-direito-constitucional"},
    {"id": "927e039b5965fe89845e6468bd154b63", "name": "isolada-direito-administrativo"},
    {"id": "b2786a4a77348a6d85a3945a51fb6462", "name": "isolada-direito-civil"},
    {"id": "a0bbc1292e6ab81dabff5e2d1fbf83cf", "name": "isolada-direito-penal"},
    {"id": "1786beaf94a74265bf5b6415efa40c63", "name": "isolada-direito-tributario"},
    {"id": "1ae41acd8ae3e1ebfdb202b7d80b7b41", "name": "isolada-direitos-humanos"},
    {"id": "0d495aa132cbc54bbfff254fcc3d6caa", "name": "isolada-informatica"},
    {"id": "75d383ec676afc3d6a5d6c57b0972d4b", "name": "isolada-lei-8112-90-novo"},
]


def main():
    args = parse_args()

    logger.info(f"🖥️ Ambiente: {CURRENT_ENV.upper()} | Base: {BASE_DIR}")

    # ---- Monta AuthManager --------------------------------------------------
    auth = AuthManager(
        use_selenium=args.selenium,
        email=args.email, password=args.password,
        jsessionid=args.jsessionid, chave=args.chave
    )

    # Credenciais padrão (edite conforme sua conta)
    EMAIL = args.email or 'psantos@gmail.com.br'
    JSESSIONID = args.jsessionid or 'COLE_AQUI_SEU_JSESSIONID'
    CHAVE = args.chave or 'COLE_AQUI_SUA_CHAVE'

    if args.selenium:
        if not (args.email and args.password):
            logger.error("❌ Modo Selenium requer --email e --password")
            sys.exit(1)
        if not auth.login_selenium():
            logger.error("❌ Falha no login Selenium. Abortando.")
            sys.exit(1)
    else:
        if 'COLE_AQUI' in JSESSIONID or 'COLE_AQUI' in CHAVE:
            logger.error("❌ Credenciais não configuradas! Edite main.py ou use --selenium.")
            logger.error("   Para obter cookies: Chrome → F12 → Application → Cookies → "
                         "portal2025.papaconcursos.com.br")
            sys.exit(1)
        auth.login_cookies(EMAIL, JSESSIONID, CHAVE)

    # ---- Lista cursos (Selenium) -------------------------------------------
    if args.list_courses:
        if not SELENIUM_AVAILABLE:
            logger.error("❌ --list-courses requer Selenium.")
            sys.exit(1)
        if not auth.ensure_valid_session():
            sys.exit(1)
        try:
            auth.driver.get(f"{BASE_URL}/portal/meus-cursos")
            time.sleep(4)
            cards = auth.driver.find_elements(
                By.CSS_SELECTOR,
                "a[href*='/portal/curso/'], a[href*='/aluno/curso/'], .card-curso"
            )
            for card in cards:
                href = card.get_attribute('href')
                title = card.text.strip().split('\n')[0]
                if href:
                    print(f"  {title}  →  {href}")
        except Exception as e:
            logger.error(f"❌ Erro ao listar cursos: {e}")
        finally:
            auth.close()
        return

    # ---- Downloader --------------------------------------------------------
    downloader = PapaDownloader(
        auth,
        wvd_path=args.wvd, keyos_token=args.keyos_token
    )
    downloader.generate_ai = args.generate_ai

    # Define lista de cursos
    if args.course_id and args.course_name:
        courses = [{"id": args.course_id, "name": args.course_name}]
    else:
        courses = DEFAULT_COURSES

    try:
        for course in courses:
            logger.info(f"\n{'=' * 70}\n📚 {course['name'].upper()}\n{'=' * 70}")
            try:
                downloader.download_course(
                    course['id'], course['name'],
                    force_rebuild=args.force_rebuild
                )
            except RuntimeError as e:
                logger.error(f"❌ {course['name']}: {e}")
                logger.error("   Renove cookies/Selenium e rode novamente.")
                break
            except Exception as e:
                logger.error(f"❌ Erro inesperado em {course['name']}: {e}")
    finally:
        downloader.close()


if __name__ == '__main__':
    main()
