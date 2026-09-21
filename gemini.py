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
from pathlib import Path
from typing import List, Dict, Tuple, Optional

# ==============================================================================
# 1. INSTALAÇÃO AUTOMÁTICA DE DEPENDÊNCIAS E CONFIGURAÇÃO DO AMBIENTE
# ==============================================================================
def preparar_ambiente_python():
    """Garante a instalação de todos os pacotes Python necessários com privilégios corretos."""
    pacotes_requeridos = {
        'selenium': '4.18.1',
        'webdriver-manager': '4.0.1',
        'cloudscraper': '1.2.71',
        'requests': '2.31.0',
        'beautifulsoup4': '4.12.3',
        'yt-dlp': '2024.3.10',
        'imageio-ffmpeg': '0.4.9',
        'pywidevine': '1.8.0',
        'urllib3': '2.2.1',
        'pycryptodome': '3.20.0',
        'protobuf': '4.25.3'
    }

    # Substitui pythonw.exe por python.exe para permitir execução via subprocess sem janelas ocultas bloqueadas
    python_exe = sys.executable.replace("pythonw.exe", "python.exe")

    for pkg, ver in pacotes_requeridos.items():
        try:
            mod_name = 'Crypto' if pkg == 'pycryptodome' else pkg.replace('-', '_')
            __import__(mod_name)
        except ImportError:
            print(f"📦 Instalando dependência ausente: {pkg}=={ver}...")
            cmd = [python_exe, "-m", "pip", "install", f"{pkg}=={ver}", "--quiet"]
            try:
                # Tenta instalação local no perfil do usuário (evita erro de permissão em Program Files)
                subprocess.check_call(cmd + ["--user"])
            except subprocess.CalledProcessError:
                subprocess.check_call(cmd)

preparar_ambiente_python()

# Imports das bibliotecas instaladas
import requests
import urllib3
import cloudscraper
import imageio_ffmpeg
from bs4 import BeautifulSoup
import yt_dlp

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import Pssh
    WIDEVINE_AVAILABLE = True
except ImportError:
    WIDEVINE_AVAILABLE = False

class EnvironmentSetup:
    @staticmethod
    def get_env() -> str:
        if 'google.colab' in sys.modules or os.path.exists('/content'):
            return 'colab'
        elif sys.platform.startswith('win'):
            return 'windows'
        elif sys.platform.startswith('linux'):
            return 'linux'
        return 'unknown'

    @classmethod
    def install_system_dependencies(cls, env: str):
        """Instala os binários de sistema (FFmpeg, Chromium) no Linux/Colab se necessário."""
        if env in ['colab', 'linux']:
            try:
                subprocess.run(['sudo', 'apt-get', 'update', '-y'], check=False, stdout=subprocess.DEVNULL)
                subprocess.run([
                    'sudo', 'apt-get', 'install', '-y', 
                    'ffmpeg', 'wget', 'curl', 'unzip'
                ], check=False, stdout=subprocess.DEVNULL)
                
                if env == 'colab':
                    subprocess.run([
                        'apt-get', 'install', '-y', 
                        'chromium-chromedriver', 'google-chrome-stable'
                    ], check=False, stdout=subprocess.DEVNULL)
            except Exception as e:
                print(f"⚠️ Aviso ao instalar dependências de sistema: {e}")

CURRENT_ENV = EnvironmentSetup.get_env()
EnvironmentSetup.install_system_dependencies(CURRENT_ENV)

# ==============================================================================
# 2. DEFINIÇÃO DE DIRETÓRIOS E CONFIGURAÇÕES DE LOG
# ==============================================================================
if CURRENT_ENV == 'colab':
    BASE_DIR = Path('/content/PapaConcursos_Downloads')
elif CURRENT_ENV == 'windows':
    BASE_DIR = Path('C:/PapaConcursos_Downloads')
else:
    BASE_DIR = Path.home() / 'PapaConcursos_Downloads'

TEMP_DIR = BASE_DIR / 'temp'
CACHE_DIR = BASE_DIR / 'cache'
EBOOKS_DIR = BASE_DIR / 'ebooks'
LOGS_DIR = BASE_DIR / 'logs'

for d in [BASE_DIR, TEMP_DIR, CACHE_DIR, EBOOKS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOGS_DIR / "papa_downloader.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("PapaSantosManager")

ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_DIR = os.path.dirname(ffmpeg_exe)
os.environ['PATH'] = FFMPEG_DIR + os.pathsep + os.environ.get('PATH', '')

def clear_name(name: str, maxlen: int = 100) -> str:
    if not name:
        return 'recurso'
    try:
        name = name.encode('latin-1').decode('utf-8')
    except Exception:
        pass
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = nfkd.encode('ASCII', 'ignore').decode('ASCII')
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', ascii_name).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:maxlen].rstrip(' .') or 'recurso'

# ==============================================================================
# 3. GERENCIADOR SELENIUM CROSS-PLATFORM (RESOLVIDO WINERROR 193)
# ==============================================================================
class PapaSeleniumManager:
    BASE_URL = "https://www.papaconcursos.com.br"
    LOGIN_URL = "https://www.papaconcursos.com.br/login"
    DASHBOARD_URL = "https://www.papaconcursos.com.br/portal/meus-cursos"

    def __init__(self, email: str = "fabianass@hotma.com.br", password: str = "180574"):
        self.email = email
        self.password = password
        self.driver: Optional[webdriver.Chrome] = None
        self.cookies_file = TEMP_DIR / "netscape_cookies.txt"
        self.session = cloudscraper.create_scraper()
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        self.driver_lock = threading.Lock()

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
            if os.path.exists('/usr/bin/chromedriver'):
                service = Service('/usr/bin/chromedriver')
                self.driver = webdriver.Chrome(service=service, options=opts)
            else:
                self.driver = webdriver.Chrome(options=opts)
        else:
            chrome_bin = shutil.which('google-chrome') or shutil.which('chrome')
            if chrome_bin:
                opts.binary_location = chrome_bin

            # Tenta utilizar o gerenciador nativo do Selenium 4.x primeiro
            try:
                self.driver = webdriver.Chrome(options=opts)
            except Exception as e:
                logger.warning(f"⚠️ Erro ao usar Selenium Manager padrão: {e}. Recorrendo ao webdriver-manager com correção de caminho...")
                
                # Limpa cache corrompido do webdriver-manager se existir
                wdm_cache = os.path.expanduser("~/.wdm")
                if os.path.exists(wdm_cache):
                    shutil.rmtree(wdm_cache, ignore_errors=True)

                from webdriver_manager.chrome import ChromeDriverManager
                installed_path = ChromeDriverManager().install()

                # Correção do WinError 193: Garante localização do executável .exe real no Windows
                driver_path = installed_path
                if sys.platform.startswith('win') and not driver_path.lower().endswith('.exe'):
                    driver_dir = os.path.dirname(driver_path) if os.path.isfile(driver_path) else driver_path
                    for root, _, files in os.walk(driver_dir):
                        for file in files:
                            if file.lower() == 'chromedriver.exe':
                                driver_path = os.path.join(root, file)
                                break

                service = Service(executable_path=driver_path)
                self.driver = webdriver.Chrome(service=service, options=opts)

        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })

    def authenticate_and_sync_cookies(self) -> bool:
        """Efetua o login via Selenium, captura cookies e atualiza o estado de sessão."""
        with self.driver_lock:
            if not self.driver:
                self._init_driver()

            logger.info(f"🔐 Autenticando no PapaConcursos no ambiente [{CURRENT_ENV.upper()}]...")
            try:
                self.driver.get(self.LOGIN_URL)
                time.sleep(3)

                if "login" in self.driver.current_url.lower():
                    email_input = self.driver.find_element(By.NAME, 'email')
                    pass_input = self.driver.find_element(By.NAME, 'password')

                    email_input.clear()
                    email_input.send_keys(self.email)
                    pass_input.clear()
                    pass_input.send_keys(self.password)
                    pass_input.send_keys(Keys.RETURN)
                    time.sleep(5)

                if "login" in self.driver.current_url.lower():
                    logger.error("❌ Erro na autenticação. Verifique as credenciais.")
                    return False

                self.user_agent = self.driver.execute_script("return navigator.userAgent;")
                self.session.headers.update({
                    'User-Agent': self.user_agent,
                    'Referer': self.BASE_URL
                })

                cookies = self.driver.get_cookies()
                self._save_netscape_cookies(cookies)

                for ck in cookies:
                    self.session.cookies.set(ck['name'], ck['value'], domain=ck.get('domain', ''))

                logger.info("✅ Login autenticado e cookies exportados!")
                return True
            except Exception as e:
                logger.error(f"❌ Falha ao inicializar/autenticar com Selenium: {e}")
                return False

    def _save_netscape_cookies(self, cookies: list):
        with open(self.cookies_file, 'w', encoding='utf-8') as f:
            f.write('# Netscape HTTP Cookie File\n')
            for c in cookies:
                domain = c.get('domain', '')
                flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = c.get('path', '/')
                secure = 'TRUE' if c.get('secure', False) else 'FALSE'
                expiration = str(int(c.get('expiry', time.time() + 86400)))
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiration}\t{c.get('name')}\t{c.get('value')}\n")

    def ensure_valid_session(self) -> bool:
        """Verifica se a sessão expirou e renova automaticamente os cookies se necessário."""
        try:
            resp = self.session.get(f"{self.BASE_URL}/portal/api/user/info", timeout=10, verify=False)
            if resp.status_code in [200, 302] and "login" not in resp.url.lower():
                return True
        except Exception:
            pass

        logger.warning("⚠️ Sessão expirada. Renovando autenticação com Selenium...")
        return self.authenticate_and_sync_cookies()

    def get_available_courses(self) -> List[Dict[str, str]]:
        """Mapeia todos os cursos disponíveis na Área do Aluno."""
        if not self.ensure_valid_session():
            return []

        logger.info("📚 Mapeando cursos disponíveis na Área do Aluno...")
        courses = []
        with self.driver_lock:
            try:
                self.driver.get(self.DASHBOARD_URL)
                time.sleep(4)

                cards = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/portal/curso/'], a[href*='/aluno/curso/'], .card-curso, .course-card")
                for card in cards:
                    try:
                        href = card.get_attribute('href')
                        title = card.text.strip().split('\n')[0]
                        
                        if not href:
                            anchor = card.find_element(By.TAG_NAME, 'a')
                            href = anchor.get_attribute('href')

                        if href and href not in [c['url'] for c in courses]:
                            c_id = href.rstrip('/').split('/')[-1]
                            courses.append({
                                'id': c_id,
                                'title': clear_name(title or f"Curso_{c_id}"),
                                'url': href
                            })
                    except Exception:
                        continue

                logger.info(f"✅ Cursos localizados: {len(courses)}")
            except Exception as e:
                logger.error(f"❌ Erro ao listar cursos: {e}")

        return courses

    def capture_network_media(self, lesson_url: str) -> Tuple[Optional[str], List[str], Dict[str, str]]:
        """Intercepta manifestos MPD/M3U8 e extrai os metadados de IA da página."""
        mpd_url = None
        drm_keys = []
        ai_data = {'transcription': '', 'summary': '', 'flashcards': '', 'mindmap': ''}

        with self.driver_lock:
            if not self.driver:
                self._init_driver()

            try:
                logger.info(f"🌐 Interceptando mídias em: {lesson_url}")
                self.driver.get(lesson_url)
                time.sleep(5)

                logs = self.driver.get_log('performance')
                for entry in logs:
                    try:
                        msg = json.loads(entry['message'])['message']
                        if msg.get('method') == 'Network.requestWillBeSent':
                            req_url = msg.get('params', {}).get('request', {}).get('url', '')

                            if ('.mpd' in req_url or '.m3u8' in req_url) and not mpd_url:
                                mpd_url = req_url
                                logger.info(f"🎯 Stream interceptado: {mpd_url[:80]}...")

                            if any(k in req_url.lower() for k in ['widevine', 'license', 'key']):
                                logger.info(f"🔑 Servidor de Licença DRM: {req_url[:80]}...")
                    except Exception:
                        continue

                # Extração de Resumo IA e Transcrição do DOM
                try:
                    summary_btn = self.driver.find_element(By.ID, "div-resume")
                    summary_btn.click()
                    time.sleep(2)
                    ai_data['summary'] = self.driver.find_element(By.ID, "summaryContent").text.strip()
                except Exception:
                    pass

                try:
                    ai_data['transcription'] = self.driver.find_element(By.CSS_SELECTOR, "#chat-content, .transcription").text.strip()
                except Exception:
                    pass

                try:
                    ai_data['flashcards'] = self.driver.find_element(By.CSS_SELECTOR, "#flashcard-content .flashcard-wrapper").text.strip()
                except Exception:
                    pass

                try:
                    ai_data['mindmap'] = self.driver.find_element(By.CSS_SELECTOR, "#mapa-content .mapa-wrapper").text.strip()
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"⚠️ Erro ao capturar recursos na aula: {e}")

        return mpd_url, drm_keys, ai_data

    def close(self):
        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None

# ==============================================================================
# 4. RESOLVER DE CHAVES DRM WIDEVINE
# ==============================================================================
class KeyosResolver:
    def __init__(self, session: requests.Session, wvd_path: str = "device.wvd", token: str = ""):
        self.session = session
        self.wvd_path = Path(wvd_path)
        self.token = token
        self.key_cache = {}

    def fetch_drm_key(self, mpd_url: str) -> Optional[str]:
        if not WIDEVINE_AVAILABLE or not self.wvd_path.exists() or not self.token:
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
                'customdata': self.token
            }
            lic_resp = self.session.post("https://playready.keyos.com/api/v4/getLicense", data=challenge, headers=headers, verify=False, timeout=15)
            lic_resp.raise_for_status()

            cdm.parse_license(session_id, lic_resp.content)
            keys = [f"{k.kid.hex()}:{k.key.hex()}" for k in cdm.get_keys(session_id) if k.type == 'OPERATIONAL']
            cdm.close(session_id)

            if keys:
                self.key_cache[mpd_url] = keys[0]
                logger.info(f"🔑 Chave DRM Widevine obtida: {keys[0][:20]}...")
                return keys[0]
        except Exception as e:
            logger.error(f"❌ Falha ao resolver DRM Widevine: {e}")

        return None

# ==============================================================================
# 5. GERADOR DE E-BOOKS DE IA E TRANSCRIÇÕES
# ==============================================================================
class EbookAiGenerator:
    @staticmethod
    def generate_ebook(lesson_dir: Path, lesson_name: str, ai_data: Dict[str, str]):
        if not any(ai_data.values()):
            return

        ebook_file = lesson_dir / f"Ebook_IA_{clear_name(lesson_name)}.md"
        content = [
            f"# E-BOOK COMPLETO DA AULA: {lesson_name}\n",
            f"*Gerado em {time.strftime('%d/%m/%Y %H:%M:%S')}*\n",
            "---\n"
        ]

        if ai_data.get('summary'):
            content.append("## 📝 RESUMO DA AULA\n")
            content.append(f"{ai_data['summary']}\n\n---\n")

        if ai_data.get('flashcards'):
            content.append("## 🎴 FLASHCARDS DE REVISÃO\n")
            content.append(f"{ai_data['flashcards']}\n\n---\n")

        if ai_data.get('mindmap'):
            content.append("## 🧠 MAPA MENTAL\n")
            content.append(f"{ai_data['mindmap']}\n\n---\n")

        if ai_data.get('transcription'):
            content.append("## 🎙️ TRANSCRIÇÃO COMPLETA\n")
            content.append(f"{ai_data['transcription']}\n")

        ebook_file.write_text("\n".join(content), encoding='utf-8')
        logger.info(f"📘 E-book gerado com sucesso: {ebook_file.name}")

# ==============================================================================
# 6. EXECUTOR PRINCIPAL DO FLUXO DE DOWNLOAD
# ==============================================================================
class PapaConcursosDownloader:
    def __init__(self):
        self.selenium_mgr = PapaSeleniumManager()
        self.keyos_resolver = KeyosResolver(self.selenium_mgr.session)
        self.ebook_gen = EbookAiGenerator()

    def download_all_student_courses(self):
        if not self.selenium_mgr.authenticate_and_sync_cookies():
            logger.error("❌ Erro ao realizar login no PapaConcursos.")
            return

        courses = self.selenium_mgr.get_available_courses()
        if not courses:
            logger.warning("⚠️ Nenhum curso encontrado.")
            return

        for course in courses:
            logger.info(f"\n==================================================")
            logger.info(f"🚀 INICIANDO CURSO: {course['title']}")
            logger.info(f"==================================================")
            self.process_course(course)

    def process_course(self, course: Dict[str, str]):
        course_dir = BASE_DIR / clear_name(course['title'])
        course_dir.mkdir(parents=True, exist_ok=True)

        lesson_url = course['url']
        lesson_name = course['title']

        self.process_lesson(lesson_url, course_dir, lesson_name)

    def process_lesson(self, lesson_url: str, course_dir: Path, lesson_name: str):
        lesson_dir = course_dir / clear_name(lesson_name)
        lesson_dir.mkdir(parents=True, exist_ok=True)

        stream_url, drm_keys, ai_data = self.selenium_mgr.capture_network_media(lesson_url)
        self.ebook_gen.generate_ebook(lesson_dir, lesson_name, ai_data)

        if stream_url:
            video_output = lesson_dir / f"{clear_name(lesson_name)}.mp4"
            self.download_media_stream(stream_url, video_output)
        else:
            logger.warning(f"⚠️ Nenhum vídeo detectado para a aula {lesson_name}")

    def download_media_stream(self, stream_url: str, output_path: Path):
        """Baixa mídias com ou sem proteção Widevine DRM."""
        if output_path.exists() and output_path.stat().st_size > 2 * 1024 * 1024:
            logger.info(f"⏭️ Arquivo já baixado: {output_path.name}")
            return

        key_pair = self.keyos_resolver.fetch_drm_key(stream_url)

        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': str(output_path.with_suffix('')) + '.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4',
            'ffmpeg_location': FFMPEG_DIR,
            'concurrent_fragment_downloads': 5,
            'allow_unplayable_formats': True,
            'cookiefile': str(self.selenium_mgr.cookies_file)
        }

        if key_pair:
            ydl_opts['decryption_key'] = key_pair
            logger.info("🔓 Baixando fluxo com descriptografia DRM...")
        else:
            logger.info("▶️ Baixando fluxo padrão (sem DRM)...")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([stream_url])
            logger.info(f"✅ Download finalizado: {output_path.name}")
        except Exception as e:
            logger.error(f"❌ Erro ao baixar fluxo de vídeo: {e}")

    def close(self):
        self.selenium_mgr.close()

# ==============================================================================
# PONTO DE ENTRADA
# ==============================================================================
if __name__ == '__main__':
    logger.info(f"🖥️ Sistema Operacional detectado: {CURRENT_ENV.upper()}")
    logger.info(f"📁 Pasta de Saída dos Downloads: {BASE_DIR}")
    
    downloader = PapaConcursosDownloader()
    try:
        downloader.download_all_student_courses()
    finally:
        downloader.close()
