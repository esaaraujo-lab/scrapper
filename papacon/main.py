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

BATCH_DELAY       = 0.3   # delay entre PDFs (paralelo, pode ser menor)
RETRY_ATTEMPTS    = 3
CHUNK_SIZE        = 1024 * 256
TIMEOUT           = 30
MAX_VIDEO_WORKERS = 2  # 1 por vez: papa_capture precisa de Chrome exclusivo
MAX_PDF_WORKERS   = 5     # PDFs em paralelo
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
        """Coleta links. Se on_item_found for passado, chama callback
        pra cada item encontrado (permite escanear + baixar em paralelo).
        """
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
                    # Se tem callback, chama pra cada item do cache (download imediato)
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

        # ---- DOMINIOS DE PLAYER (expandido) ----
        # Papa Concursos usa MULTIPLOS players:
        #   - videotecaead.com.br → player antigo (HLS .m3u8, sem DRM)
        #   - player.vdocipher.com → VdoCipher (DRM Widevine)
        #   - player.sambatech.com.br → Samba Player (HLS com/sem DRM)
        #   - cdn.plyr.io → Plyr (HLS .m3u8, sem DRM)
        # ---- PLAYER DETECTION (corrigido) ----
        # O iframe src ja tem a URL correta:
        # player.videotecaead.com.br/embed/{ACCOUNT_ID}/{VIDEO_ID}?
        # NAO precisamos adivinhar slug! Fetch direto do iframe.
        VIDEO_DOMAINS = (
            'videotecaead.com.br',
            'player.videotecaead.com.br',
            'embed.videotecaead.com.br',
            'player.vdocipher.com',
            'player.sambatech.com.br',
            'cdn.plyr.io',
            'vdocipher.com',
            'sambatech.com.br',
        )

        for iframe in soup.find_all('iframe'):
            iframe_src = (iframe.get('src') or '').strip()
            if not iframe_src or not any(d in iframe_src for d in VIDEO_DOMAINS):
                continue
            try:
                vid_resp = self.session.get(iframe_src, headers=HEADERS, timeout=TIMEOUT)

                # 1) manifestUrl (Shaka Player config)
                m = re.search(r"manifestUrl\s*=\s*['\"]([^'\"]+\.mpd[^'\"]*)", vid_resp.text)
                if m:
                    mpd_url = m.group(1)
                    # FALLBACK: substituir /drm/dash/master.mpd por /hls/master.m3u8
                    # A Azion CDN tem versao SEM DRM em /hls/master.m3u8
                    m3u8_url = re.sub(
                        r'/drm/dash/master\.mpd.*$',
                        '/hls/master.m3u8',
                        mpd_url
                    )
                    # Testa se o .m3u8 existe (200 OK)
                    try:
                        m3u8_resp = self.session.get(m3u8_url, headers=HEADERS, timeout=10)
                        if m3u8_resp.status_code == 200 and '#EXTM3U' in m3u8_resp.text[:20]:
                            video_url = m3u8_url
                            logger.info(f"  .m3u8 sem DRM (fallback): {video_url[:80]}")
                            license_m = None
                            break
                    except Exception:
                        pass
                    # Se .m3u8 nao existe, usa .mpd (precisa de DRM)
                    # FIX #5: Tenta player antigo (embed.videotecaead.com.br/papaconcursos/{SLUG})
                    # antes de aceitar que tem que fazer DRM — bypassa DRM pra conteúdo legacy
                    if not video_url:
                        mpd_url_str = mpd_url
                        # Extrai slug do título da página
                        title_m = re.search(r'<title>([^<]+)</title>', vid_resp.text)
                        if title_m:
                            raw_title = title_m.group(1).replace('.mp4', '').strip()
                            slug = self._title_to_slug(raw_title)
                            if slug:
                                old_player_url = f"https://embed.videotecaead.com.br/papaconcursos/{slug}"
                                logger.info(f"  Tentando player antigo: {old_player_url[:80]}")
                                try:
                                    old_resp = self.session.get(old_player_url, headers=HEADERS, timeout=TIMEOUT)
                                    if old_resp.status_code == 200 and len(old_resp.text) > 500:
                                        # Procura .m3u8 no player antigo
                                        old_soup = BeautifulSoup(old_resp.content, 'html.parser')
                                        for script in old_soup.find_all('script'):
                                            mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
                                                           script.string or '')
                                            if mo:
                                                video_url = mo.group(1)
                                                logger.info(f"  ✓ m3u8 via player antigo: {video_url[:80]}")
                                                break
                                        if not video_url:
                                            mo = re.search(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', old_resp.text)
                                            if mo:
                                                video_url = mo.group(1)
                                                logger.info(f"  ✓ m3u8 via player antigo (texto): {video_url[:80]}")
                                    else:
                                        logger.info(f"  Player antigo: HTTP {old_resp.status_code} (não disponível)")
                                except Exception as e:
                                    logger.info(f"  Player antigo erro: {e}")

                    # Se ainda não achou .m3u8 no player antigo, usa .mpd (precisa de DRM)
                    if not video_url:
                        video_url = mpd_url_str
                        logger.info(f"  .mpd via manifestUrl (DRM): {video_url[:80]}")
                    license_m = re.search(r'widevine[^"\n]*keyos\.com([^"\n]*)', vid_resp.text, re.I)
                    if license_m:
                        logger.info("  Widevine license: widevine.keyos.com" + license_m.group(1)[:50])
                    break

                # 2) .m3u8 (sem DRM)
                m = re.search(r"(https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*)", vid_resp.text)
                if m:
                    video_url = m.group(1)
                    logger.info(f"  .m3u8 (sem DRM): {video_url[:80]}")
                    break

                # 3) .mpd direto no texto
                m = re.search(r"(https?://[^\s'\"<>]+\.mpd[^\s'\"<>]*)", vid_resp.text)
                if m:
                    video_url = m.group(1)
                    logger.info(f"  .mpd direto: {video_url[:80]}")
                    break

            except Exception as e:
                logger.warning(f"  Erro iframe {iframe_src[:60]}: {e}")

        # Fallback: .m3u8 ou .mpd direto no /media response
        if not video_url:
            m = re.search(r"(https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*)", media_resp.text)
            if m: video_url = m.group(1)
        if not video_url:
            m = re.search(r"(https?://[^\s'\"<>]+\.mpd[^\s'\"<>]*)", media_resp.text)
            if m: video_url = m.group(1)

        # ---- MATERIAIS via API getDocumentoTopico ----
        # Inclui PDFs, transcrições, resumos de IA (A-1, A-2, A-3), imagens
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
                    # PDF / Documento
                    url = f"{portal_base}/documento-online-key?idDocumento={doc_token}&tipo=D&token={course_id}"
                    materials.append({"url": url, "nome": nome, "tipo": "PDF"})
                    logger.info(f"    PDF: {nome[:50]}")

                elif tipo == "I":
                    # Imagem / Material interativo
                    url = f"{portal_base}/documento-online-key?idDocumento={doc_token}&tipo=I&token={course_id}"
                    materials.append({"url": url, "nome": nome, "tipo": "PDF"})
                    logger.info(f"    IMG: {nome[:50]}")

                elif tipo == "T":
                    # Transcrição
                    url = f"{portal_base}/getTranscricao?format=json&token={doc_token}"
                    materials.append({"url": url, "nome": nome or "Transcricao", "tipo": "TXT"})
                    logger.info(f"    Transc: {nome[:50]}")

                elif tipo in ("A-1", "A-2", "A-3", "A-4"):
                    # Resumo / Ebook de IA — getEbookAI retorna HTML (conteúdo do ebook)
                    # não PDF! Vamos salvar como .html (pode abrir no browser)
                    url = f"{portal_base}/getEbookAI?token={doc_token}"
                    materials.append({"url": url, "nome": nome or "Resumo IA", "tipo": "HTML"})
                    logger.info(f"    AI: {nome[:50]}")

            # Se nao achou nenhum resumo de IA, tenta gerar
            if not any(m["nome"].startswith("Resumo IA") or m["nome"].startswith("Ebook") for m in materials):
                try:
                    logger.info(f"    AI: tentando gerar resumo...")
                    resp_ai = self.session.post(
                        f"{portal_base}/gerarAIEbook",
                        json={"token": item["token"], "tokenCurso": course_id},
                        headers=HEADERS, timeout=30)
                    if resp_ai.status_code == 200:
                        try:
                            ai_data = resp_ai.json()
                            ai_token = ai_data.get("token") or ai_data.get("id")
                            if ai_token:
                                url = f"{portal_base}/getEbookAI?token={ai_token}"
                                materials.append({"url": url, "nome": "Resumo IA", "tipo": "PDF"})
                                logger.info(f"    AI: resumo gerado OK")
                        except Exception:
                            pass
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"    getDocumentoTopico falhou: {e}")
            # Fallback: botoes HTML
            for btn in soup.find_all("button", id=re.compile(r"btnMaterialDownload[a-f0-9]+")):
                doc_id = btn.get("data-value")
                tok = btn.get("data-token")
                if doc_id and tok:
                    url = f"{portal_base}/documento-online-key?idDocumento={doc_id}&tipo=D&token={tok}"
                    materials.append({"url": url, "nome": "material", "tipo": "PDF"})

        lesson_key = os.path.join(path, item_title)
        # Detecta DRM: se video_url contém .mpd e não foi substituído por .m3u8
        is_drm = False
        if video_url and '.mpd' in video_url:
            # .mpd é sempre DRM (se .m3u8 fallback funcionou, video_url seria .m3u8)
            is_drm = True
        elif video_url and ('/drm/' in video_url or 'widevine' in video_url.lower()):
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

        # Chama callback (download imediato — escaneamento + download em paralelo)
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
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def login(self, email: str, jsessionid: str, chave: str):
        self._email = email
        self._jsessionid = jsessionid
        self._chave = chave
        # FIX #6: Seta cookies em AMBOS subdomínios (www + portal2025)
        # pra sessão funcionar nos 2 (Papa usa www p/ login e portal2025 p/ curso)
        for domain in ['.papaconcursos.com.br', 'www.papaconcursos.com.br',
                       'portal2025.papaconcursos.com.br']:
            try:
                self.session.cookies.set('email', email, domain=domain, path='/')
                self.session.cookies.set('JSESSIONID', jsessionid, domain=domain, path='/')
                self.session.cookies.set('chave', chave, domain=domain, path='/')
            except Exception:
                pass
        logger.info(f"Sessão iniciada — cookies setados em 3 domínios (wildcard + www + portal2025)")

    def _refresh_session(self):
        """Re-seta cookies na session (chamar antes de cada DRM download
        pra evitar sessão expirada no meio do curso)."""
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
        """Escaneia tópicos em sequência e baixa IMEDIATAMENTE cada item.
        NÃO pre-scanneia tudo — evita que links morram antes do download.
        """
        self.course_dir = create_folder(os.path.join(str(BASE_DIR), clear_name(course_name)))
        collector  = LinkCollector(self.session)

        # SEM cache — escaneia fresh, baixa imediatamente, próxima, etc.
        # (cache pode ter links mortos se sessão expirou)
        try:
            logger.info(f"🔍 Iniciando escaneamento + download SEQUENCIAL: {course_name}")
            collector.collect_all_links(course_id, course_name,
                                          force_rebuild=True,  # ignora cache
                                          on_item_found=self._download_item_inline)
        except RuntimeError as e:
            logger.error(f"✗ {e}")
            logger.error("Renove os cookies no main() e rode novamente.")
        except Exception as e:
            logger.error(f"✗ Erro no escaneamento: {e}")

    def _download_item_inline(self, rel_path: str, data: dict):
        """Callback chamado pra cada item encontrado — baixa IMEDIATAMENTE.
        Não enfileira — executa síncrono (sequencial).
        """
        try:
            lesson_dir = create_folder(os.path.join(self.course_dir, rel_path))

            # Verifica disco: se vídeo já existe, pula (skip)
            video_file = os.path.join(lesson_dir, '001 - aula.mp4')
            mat_dir = create_folder(os.path.join(lesson_dir, 'material'))

            # AUTO-CONVERSÃO: converte .txt e .html antigos pra .md
            # (se já existem de runs anteriores, converte e apaga os originais)
            self._convert_existing_to_md(mat_dir)

            # Baixa vídeo imediatamente (DRM primeiro pela ordem de scan — DRM aparece primeiro nos cursos)
            if data.get('video') and not is_video_complete(video_file):
                is_drm = data.get('is_drm', False)
                logger.info(f"📥 Baixando {'🔒DRM' if is_drm else 'regular'}: {rel_path}")
                # FIX #6: Refresh session antes de cada vídeo (cookies podem ter expirado)
                self._refresh_session()
                try:
                    self._download_video(data['video'], video_file, data.get('media_token', ''))
                except Exception as e:
                    err_str = str(e)
                    if '404' in err_str or 'not found' in err_str.lower():
                        logger.error(f"  ❌ Link morreu (404) — vídeo: {rel_path}")
                        logger.error(f"     Isso geralmente acontece quando o cache tem links antigos.")
                        logger.error(f"     Cache foi limpo, próxima execução vai funcionar.")
                    else:
                        logger.error(f"  ❌ Erro download vídeo: {err_str[:120]}")
            elif data.get('video'):
                logger.info(f"✓ Vídeo já existe: {rel_path}")

            # Baixa PDFs imediatamente (depois do vídeo)
            # FIX: Usa nome original do material (em vez de "001 - material.pdf")
            # pra ficar mais legível. Ex: "001 - Direito Processual do Trabalho - Aula 01.pdf"
            for idx, mat in enumerate(data.get('materials', []), 1):
                mat_name = mat.get('nome', '') or 'material'
                # Sanitiza nome pra usar como filename (remove chars inválidos Windows)
                safe_name = re.sub(r'[<>:"/\\|?*]', '', mat_name).strip()[:80]
                if not safe_name:
                    safe_name = 'material'
                # Tipo do material determina extensão
                mat_tipo = mat.get('tipo', 'PDF')
                if mat_tipo == 'TXT' or 'transcri' in mat_name.lower():
                    ext = '.md'      # transcrição salva como Markdown (mais limpo)
                elif mat_tipo == 'HTML' or 'ebook' in mat_name.lower() or 'ebook' in mat_tipo.lower():
                    ext = '.md'      # AI ebook convertido de HTML pra Markdown
                else:
                    ext = '.pdf'
                pdf_file = os.path.join(mat_dir, f"{idx:03d} - {safe_name}{ext}")
                # Checa se já existe (qualquer extensão)
                stem = pdf_file.rsplit('.', 1)[0]
                existing_pdf = stem + '.pdf'
                existing_txt = stem + '.txt'
                existing_html = stem + '.html'
                existing_md = stem + '.md'
                if is_valid_pdf(existing_pdf):
                    continue
                if os.path.exists(existing_md) and os.path.getsize(existing_md) > 100:
                    continue
                if os.path.exists(existing_txt) and os.path.getsize(existing_txt) > 100:
                    continue  # .txt antigo — vai ser convertido pelo _convert_existing_to_md
                if os.path.exists(existing_html) and os.path.getsize(existing_html) > 100:
                    continue  # .html antigo — vai ser convertido
                # Limpa arquivos velhos/parciais
                for old_file in [pdf_file, existing_pdf, existing_txt, existing_html, existing_md]:
                    if os.path.exists(old_file):
                        try: os.remove(old_file)
                        except Exception: pass
                try:
                    self._download_pdf(mat['url'], pdf_file, mat_name=mat_name)
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
                # ---- FIX AAC AUDIO (HLS .m3u8 streams) ----
                # Papa Concursos usa HLS .m3u8 com AAC em formato ADTS que
                # as vezes e incompativel com container MP4 (sample rate
                # divergente, AAC-LC vs HE-AAC v2, channel layout mismatch).
                # Forcamos merge_output_format=mp4 + re-encode de audio
                # para AAC 192kbps/44.1kHz/stereo durante o merge.
                'merge_output_format': 'mp4',
                'postprocessor_args': {
                    'FFmpegMerger': [
                        '-c:v', 'copy',                # video: copy (rapido)
                        '-c:a', 'aac',                 # audio: re-encode AAC
                        '-b:a', '192k',                # bitrate: 192 kbps
                        '-ar', '44100',                # sample rate: 44.1 kHz
                        '-ac', '2',                    # canais: estereo (2)
                        '-movflags', '+faststart',     # MP4 faststart
                    ],
                },
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
                logger.warning(f"🔒 DRM detectado — tentando papa_capture via Selenium...")
                # FALLBACK FINAL: chama papa_capture (abre Chrome, captura licença)
                if _DRM_CAPTURE_AVAILABLE:
                    try:
                        cookies_path = str(pathlib.Path.home() / 'PapaConcursos_Downloads' / 'cookies_papa.json')
                        success = _drm_capture(
                            mpd_url=manifest_url,
                            output_path=output_path,
                            media_token=media_token,
                            cookies_file=cookies_path,
                        )
                        if success:
                            logger.info(f"✅ DRM baixado via papa_capture!")
                            return
                        logger.error(f"❌ papa_capture também falhou")
                    except Exception as e2:
                        logger.error(f"❌ papa_capture erro: {e2}")
                else:
                    logger.error(f"🔒 DRM: {os.path.basename(output_path)} (papa_capture não disponível)")
            else:
                logger.error(f"Falha vídeo: {err[:120]}")

    def _download_material(self, url: str, output_path: str, mat_tipo: str = 'PDF') -> bool:
        """Baixa material. mat_tipo pode ser PDF, TXT (transcrição) ou IMG.
        Para TXT: response JSON → extrai texto → salva .txt
        Para PDF/IMG: baixa direto ou segue redirect do JSON
        """
        rel_path = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
                # Log diagnóstico do status e content-type
                ct = resp.headers.get('content-type', '?')
                if attempt == 0:
                    logger.info(f"  HTTP {resp.status_code} | CT={ct} | bytes={len(resp.content)} | url={url[:80]}")
                if resp.status_code != 200:
                    # Loga o erro específico pra debug
                    if attempt == 0:
                        body_preview = resp.text[:200] if hasattr(resp, 'text') else ''
                        logger.warning(f"  ⚠ HTTP {resp.status_code} (tentativa {attempt+1}/{RETRY_ATTEMPTS}): {body_preview[:100]}")
                    time.sleep(2 ** attempt)
                    continue
                # Pula se vier login page (403 disfarçado de 200)
                text_start = resp.text[:300] if hasattr(resp, 'text') else ''
                if '<title>Login' in text_start or 'faça login' in text_start.lower():
                    logger.warning(f"  ⚠ Recebeu página de login em vez do material: {url[:80]}")
                    continue
                if 'access denied' in text_start.lower() or '403' in text_start[:50]:
                    logger.warning(f"  ⚠ Access denied: {url[:80]}")
                    continue

                content_type = resp.headers.get('content-type', '').lower()

                # JSON com URL dentro (documento-online-key retorna JSON com URL do PDF)
                if 'json' in content_type or resp.content.startswith(b'{'):
                    try:
                        data = resp.json()
                    except Exception:
                        time.sleep(2 ** attempt)
                        continue
                    target_url = None
                    if isinstance(data, dict):
                        target_url = data.get('url') or data.get('link') or data.get('path')
                    if target_url:
                        if not target_url.startswith('http'):
                            target_url = f"https://portal2025.papaconcursos.com.br{target_url}"
                        pdf_resp = self.session.get(target_url, stream=True, timeout=60, verify=False)
                        if pdf_resp.status_code == 200 and pdf_resp.content.startswith(b'%PDF'):
                            with open(temp_path, 'wb') as f:
                                f.write(pdf_resp.content)
                            shutil.move(temp_path, output_path)
                            logger.info(f"  PDF OK: {os.path.basename(output_path)}")
                            return True
                    # Se JSON não tem URL, pode ser a transcrição em texto
                    if mat_tipo == 'TXT' and isinstance(data, dict):
                        # Extrai texto da transcrição
                        txt_content = data.get('transcricao') or data.get('texto') or data.get('content') or ''
                        if not txt_content:
                            # Tenta extrair de campos aninhados
                            txt_content = json.dumps(data, ensure_ascii=False, indent=2)
                        with open(temp_path, 'w', encoding='utf-8') as f:
                            f.write(txt_content)
                        shutil.move(temp_path, output_path)
                        logger.info(f"  Transcrição OK: {os.path.basename(output_path)}")
                        return True
                    time.sleep(2 ** attempt)

                # PDF direto
                elif resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    logger.info(f"  PDF OK: {os.path.basename(output_path)}")
                    return True

                # Texto direto (transcrição em texto puro)
                elif mat_tipo == 'TXT' and len(resp.text) > 100:
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        f.write(resp.text)
                    shutil.move(temp_path, output_path)
                    logger.info(f"  Transcrição OK: {os.path.basename(output_path)}")
                    return True

                else:
                    time.sleep(2 ** attempt)

            except Exception as e:
                logger.warning(f"  Tentativa {attempt+1}: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"  Falha: {os.path.basename(output_path)}")
        return False

    def _download_pdf(self, url: str, output_path: str, mat_name: str = '') -> bool:
        """Baixa PDF. Lida com: PDF direto, JSON com campo url/link/path,
        HTML de login, redirecionamentos. Logging diagnóstico completo.
        """
        rel_path  = os.path.relpath(output_path, str(BASE_DIR))
        temp_path = os.path.join(str(TEMP_DIR), rel_path)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        portal_base = "https://portal2025.papaconcursos.com.br"

        for attempt in range(RETRY_ATTEMPTS):
            try:
                # FIX #4: Refresh session antes de cada tentativa
                self._refresh_session()
                resp = self.session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)

                # Log diagnóstico (só na 1a tentativa pra não floodar)
                if attempt == 0:
                    ct = resp.headers.get('content-type', '?')
                    logger.info(f"  HTTP {resp.status_code} | CT={ct} | bytes={len(resp.content)} | url={url[:80]}")

                if resp.status_code == 404:
                    # Pode ser que o AI ebook ainda não foi gerado
                    if 'getEbookAI' in url:
                        logger.warning(f"  ⚠ 404 em getEbookAI — ebook ainda não gerado")
                        # Tenta POST gerarAIEbook pra gerar
                        self._try_generate_ai_ebook(url)
                        time.sleep(2 ** attempt)
                        continue
                    logger.warning(f"  ⚠ HTTP 404 — link morto: {url[:80]}")
                    time.sleep(2 ** attempt)
                    continue

                if resp.status_code == 403:
                    logger.warning(f"  ⚠ HTTP 403 — acesso negado (sessão expirou?): {url[:80]}")
                    # Tenta refresh session e re-tenta
                    self._refresh_session()
                    time.sleep(2 ** attempt)
                    continue

                if resp.status_code != 200:
                    body_preview = resp.text[:200] if hasattr(resp, 'text') else ''
                    logger.warning(f"  ⚠ HTTP {resp.status_code} (tentativa {attempt+1}/{RETRY_ATTEMPTS}): {body_preview[:100]}")
                    time.sleep(2 ** attempt)
                    continue

                # Detecta página de login disfarçada de 200
                text_start = resp.text[:500] if hasattr(resp, 'text') else ''
                if '<title>Login' in text_start or 'faça login' in text_start.lower():
                    logger.warning(f"  ⚠ Recebeu página de login em vez do material: {url[:80]}")
                    self._refresh_session()
                    time.sleep(2 ** attempt)
                    continue

                content_type = resp.headers.get('content-type', '').lower()

                # Caso 1: JSON com campo url/link/path (documento-online-key retorna isso)
                if 'application/json' in content_type or resp.content.startswith(b'{'):
                    try:
                        data = resp.json()
                    except Exception as e:
                        logger.warning(f"  ⚠ JSON inválido: {e}")
                        time.sleep(2 ** attempt)
                        continue
                    if not isinstance(data, dict):
                        time.sleep(2 ** attempt)
                        continue
                    # FIX: Tenta campos url, link, path, file (alguns endpoints usam nomes diferentes)
                    target_url = data.get('url') or data.get('link') or data.get('path') or data.get('file')
                    if target_url:
                        if not target_url.startswith('http'):
                            target_url = f"{portal_base}{quote(target_url, safe=':/')}"
                        logger.info(f"  📄 JSON redirect → {target_url[:80]}")
                        pdf_resp = self.session.get(target_url, stream=True, timeout=60, verify=False)
                        if pdf_resp.status_code == 200 and pdf_resp.content.startswith(b'%PDF'):
                            with open(temp_path, 'wb') as f:
                                f.write(pdf_resp.content)
                            shutil.move(temp_path, output_path)
                            logger.info(f"  ✓ PDF OK: {os.path.basename(output_path)} ({len(pdf_resp.content)} bytes)")
                            return True
                        elif pdf_resp.status_code == 200:
                            # Veio 200 mas não é PDF — talvez seja HTML de login
                            body_preview = pdf_resp.text[:200] if hasattr(pdf_resp, 'text') else ''
                            logger.warning(f"  ⚠ Redirect retornou 200 mas não é PDF: {body_preview[:100]}")
                        else:
                            logger.warning(f"  ⚠ Redirect falhou: HTTP {pdf_resp.status_code}")
                    else:
                        # JSON não tem campo URL — pode ser texto da transcrição
                        if mat_name and 'transcri' in mat_name.lower():
                            # Papa Concursos retorna: {"transcripts": [{"transcript": "texto...", "words": [...], ...}]}
                            # O campo "words" tem timestamps palavra-por-palavra = ENORME (2MB+ pra aula de 1h)
                            # Só queremos o campo "transcript" (texto puro da fala)
                            txt_content = ''
                            if 'transcripts' in data and isinstance(data['transcripts'], list):
                                # Extrai só o texto de cada bloco (ignora words/confidence/idVideo)
                                parts = []
                                for t in data['transcripts']:
                                    if isinstance(t, dict) and t.get('transcript'):
                                        parts.append(t['transcript'])
                                txt_content = '\n\n'.join(parts)
                                logger.info(f"  📝 Extraído texto de {len(parts)} blocos (sem timestamps/words)")
                            elif data.get('transcricao'):
                                txt_content = data['transcricao']
                            elif data.get('texto'):
                                txt_content = data['texto']
                            elif data.get('content'):
                                txt_content = data['content']
                            elif data.get('transcript'):
                                txt_content = data['transcript']

                            if not txt_content:
                                # Último recurso: salva JSON pra debug (mas NÃO é o texto)
                                logger.warning(f"  ⚠ JSON sem campo transcripts/transcricao/texto — salvando raw JSON pra debug")
                                txt_content = f"// DEBUG: JSON sem campos de transcrição conhecidos\n// Chaves: {list(data.keys())}\n\n" + json.dumps(data, ensure_ascii=False, indent=2)[:5000]

                            # Se tem HTML dentro do texto, converte pra MD
                            if '<p>' in txt_content[:500] or '<strong>' in txt_content[:500] or '<html' in txt_content[:500].lower():
                                md_content = self._html_to_markdown(txt_content)
                            else:
                                md_content = txt_content

                            md_path = output_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                            md_temp = temp_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                            with open(md_temp, 'w', encoding='utf-8') as f:
                                f.write(md_content)
                            shutil.move(md_temp, md_path)
                            logger.info(f"  ✓ MD OK: {os.path.basename(md_path)} ({len(md_content)} chars)")
                            return True
                        logger.warning(f"  ⚠ JSON sem campo url/link/path/file: {str(data)[:100]}")
                    time.sleep(2 ** attempt)

                # Caso 2: PDF direto (content começa com %PDF)
                elif resp.content.startswith(b'%PDF'):
                    with open(temp_path, 'wb') as f:
                        f.write(resp.content)
                    shutil.move(temp_path, output_path)
                    logger.info(f"  ✓ PDF OK: {os.path.basename(output_path)} ({len(resp.content)} bytes)")
                    return True

                # Caso 3: Texto direto (transcrição em texto puro OU JSON)
                # Salva como .md (Markdown) — pode ter HTML dentro, converte
                elif mat_name and ('transcri' in mat_name.lower() or 'txt' in content_type):
                    if len(resp.text) > 100:
                        # Detecta se tem HTML dentro do texto
                        text_content = resp.text
                        # Se tem tags HTML, converte pra MD
                        if '<p>' in text_content[:500] or '<strong>' in text_content[:500] or '<html' in text_content[:500].lower():
                            md_content = self._html_to_markdown(text_content)
                        else:
                            # Texto puro — só adiciona quebras de linha decentes
                            md_content = text_content
                        md_path = output_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                        md_temp = temp_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                        with open(md_temp, 'w', encoding='utf-8') as f:
                            f.write(md_content)
                        shutil.move(md_temp, md_path)
                        logger.info(f"  ✓ MD OK: {os.path.basename(md_path)} ({len(md_content)} chars)")
                        return True

                # Caso 4: HTML content (AI ebook do Papa retorna isso!)
                # Body começa com "<p>" ou "<html" ou tem tags HTML
                # Agora converte pra Markdown (mais limpo que .html)
                elif (resp.content.startswith(b'<') or
                      b'<p>' in resp.content[:200] or
                      b'<html' in resp.content[:500].lower() or
                      'html' in content_type):
                    # Tenta UTF-8 primeiro, fallback ISO-8859-1 (Papa usa ISO-8859-1)
                    try:
                        html_content = resp.content.decode('utf-8')
                    except UnicodeDecodeError:
                        html_content = resp.content.decode('iso-8859-1', errors='replace')
                    # Converte HTML pra Markdown (limpo, sem tags)
                    md_content = self._html_to_markdown(html_content)
                    # Salva como .md
                    md_path = output_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                    md_temp = temp_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                    with open(md_temp, 'w', encoding='utf-8') as f:
                        f.write(md_content)
                    shutil.move(md_temp, md_path)
                    logger.info(f"  ✓ MD OK (HTML→MD): {os.path.basename(md_path)} ({len(md_content)} chars)")
                    return True

                # Caso 5: Texto puro (AI ebook do Papa às vezes vem como plain text, sem HTML)
                # Content-Type: text/plain, body NÃO começa com < nem { nem %PDF
                # Salva direto como .md (é texto limpo)
                elif ('text/plain' in content_type or 'text/' in content_type) and len(resp.text) > 100:
                    text_content = resp.text
                    # Se acidentalmente tem HTML dentro do texto, converte
                    if '<p>' in text_content[:500] or '<strong>' in text_content[:500] or '<html' in text_content[:500].lower():
                        md_content = self._html_to_markdown(text_content)
                    else:
                        md_content = text_content
                    md_path = output_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                    md_temp = temp_path.replace('.pdf', '.md').replace('.html', '.md').replace('.txt', '.md')
                    with open(md_temp, 'w', encoding='utf-8') as f:
                        f.write(md_content)
                    shutil.move(md_temp, md_path)
                    logger.info(f"  ✓ MD OK (texto puro): {os.path.basename(md_path)} ({len(md_content)} chars)")
                    return True

                else:
                    # Resposta não-JSON não-PDF não-HTML não-texto — pode ser erro
                    body_preview = resp.text[:200] if hasattr(resp, 'text') else ''
                    logger.warning(f"  ⚠ Resposta não-JSON não-PDF não-HTML não-texto: CT={content_type} body={body_preview[:100]}")
                    time.sleep(2 ** attempt)

            except Exception as e:
                logger.warning(f"  Tentativa {attempt+1} PDF: {e}")
                time.sleep(2 ** attempt)

        logger.error(f"✗ Falha PDF: {os.path.basename(output_path)} ({RETRY_ATTEMPTS} tentativas)")
        return False

    def _html_to_markdown(self, html_content: str) -> str:
        """Converte HTML pra Markdown limpo (sem tags, com formatação MD).

        Suporta: <p>, <strong>/<b>, <em>/<i>, <h1>-<h6>, <ul>/<li>,
        <ol>/<li>, <br>, <hr>, <blockquote>, <code>, <a href>.
        """
        if not html_content:
            return ""

        # Decodifica entidades HTML básicas
        html_content = html_content.replace('&nbsp;', ' ').replace('&amp;', '&')
        html_content = html_content.replace('&lt;', '<').replace('&gt;', '>')
        html_content = html_content.replace('&quot;', '"').replace('&#39;', "'")

        # Remove scripts e styles
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.S | re.I)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.S | re.I)

        # Converte blocos especiais primeiro (antes de strip tags)
        # Headers h1-h6
        for level in range(1, 7):
            prefix = '#' * level
            html_content = re.sub(
                rf'<h{level}[^>]*>(.*?)</h{level}>',
                rf'\n\n{prefix} \1\n\n',
                html_content, flags=re.S | re.I)

        # <hr> → ---
        html_content = re.sub(r'<hr[^>]*>', '\n\n---\n\n', html_content, flags=re.I)

        # <br> e <br/> → newline
        html_content = re.sub(r'<br\s*/?>', '\n', html_content, flags=re.I)

        # <p>...</p> → \n\n...\n\n
        html_content = re.sub(r'<p[^>]*>(.*?)</p>', r'\n\n\1\n\n',
                              html_content, flags=re.S | re.I)

        # <strong>/<b> → **...**
        html_content = re.sub(r'<(strong|b)[^>]*>(.*?)</\1>', r'**\2**',
                              html_content, flags=re.S | re.I)

        # <em>/<i> → *...*
        html_content = re.sub(r'<(em|i)[^>]*>(.*?)</\1>', r'*\2*',
                              html_content, flags=re.S | re.I)

        # <code> → `...`
        html_content = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`',
                              html_content, flags=re.S | re.I)

        # <blockquote> → > ...
        html_content = re.sub(r'<blockquote[^>]*>(.*?)</blockquote>',
                              lambda m: '\n\n' + '\n'.join(
                                  f'> {line}' for line in m.group(1).strip().split('\n')
                              ) + '\n\n',
                              html_content, flags=re.S | re.I)

        # <a href="URL">TEXT</a> → [TEXT](URL)
        html_content = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            r'[\2](\1)',
            html_content, flags=re.S | re.I)

        # <ul><li>...</li></ul> → - ... (bullet list)
        def convert_ul(m):
            items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), flags=re.S | re.I)
            return '\n\n' + '\n'.join(f'- {item.strip()}' for item in items) + '\n\n'
        html_content = re.sub(r'<ul[^>]*>(.*?)</ul>', convert_ul,
                              html_content, flags=re.S | re.I)

        # <ol><li>...</li></ol> → 1. ... (numbered list)
        def convert_ol(m):
            items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), flags=re.S | re.I)
            return '\n\n' + '\n'.join(
                f'{i+1}. {item.strip()}' for i, item in enumerate(items)
            ) + '\n\n'
        html_content = re.sub(r'<ol[^>]*>(.*?)</ol>', convert_ol,
                              html_content, flags=re.S | re.I)

        # Remove qualquer tag HTML restante
        html_content = re.sub(r'<[^>]+>', '', html_content)

        # Limpa whitespace excessivo
        # Múltiplos \n → máximo 2
        html_content = re.sub(r'\n{3,}', '\n\n', html_content)
        # Espaços no fim das linhas
        html_content = re.sub(r'[ \t]+\n', '\n', html_content)
        # Espaços duplicados (mas preserva newlines e indentação de listas)
        html_content = re.sub(r'(?<!\n)[ \t]{2,}', ' ', html_content)

        return html_content.strip() + '\n'

    def _convert_existing_to_md(self, material_dir: str):
        """Escaneia pasta material/ e converte/corrige arquivos antigos.

        Pra cada .txt:
        - Se é JSON com "transcripts" array → extrai só o texto
        - Se é HTML → converte pra MD
        - Senão → renomeia pra .md

        Pra cada .html:
        - Converte pra MD

        Pra cada .md (NOVO!):
        - Se é grande (>200KB) e tem JSON/words → extrai só o texto
        - Se tem HTML tags → limpa pra MD puro
        - Se tem timestamps ([00:01:23]) → remove (opcional)
        - Senão → mantém como está (já é MD limpo)

        Depois apaga os .txt e .html originais.
        """
        try:
            mat_path = Path(material_dir)
            if not mat_path.is_dir():
                return
            converted = 0
            # 1. Converte .txt e .html antigos
            for ext in ['.txt', '.html']:
                for old_file in mat_path.glob(f'*{ext}'):
                    try:
                        content = old_file.read_text(encoding='utf-8', errors='replace')
                        # Detecta se é JSON com transcripts (Papa Concursos format)
                        if content.lstrip().startswith('{') and '"transcripts"' in content[:500]:
                            try:
                                data = json.loads(content)
                                if 'transcripts' in data and isinstance(data['transcripts'], list):
                                    parts = [t.get('transcript', '') for t in data['transcripts']
                                             if isinstance(t, dict)]
                                    content = '\n\n'.join(parts)
                                    logger.info(f"  📝 JSON com transcripts → texto puro ({len(parts)} blocos, {len(content)} chars)")
                            except Exception:
                                pass
                        # Detecta se é HTML
                        is_html = (content.lstrip().startswith('<') or
                                   '<p>' in content[:200] or
                                   '<html' in content[:500].lower())
                        if is_html:
                            md_content = self._html_to_markdown(content)
                        else:
                            md_content = content
                        md_file = old_file.with_suffix('.md')
                        if md_file.exists() and md_file.stat().st_mtime > old_file.stat().st_mtime:
                            logger.info(f"  📝 .md já existe (mais novo): {md_file.name} — apagando .{ext}")
                            old_file.unlink()
                            converted += 1
                            continue
                        md_file.write_text(md_content, encoding='utf-8')
                        old_file.unlink()
                        converted += 1
                        logger.info(f"  📝 Convertido: {old_file.name} → {md_file.name} ({len(md_content)} chars)")
                    except Exception as e:
                        logger.warning(f"  ⚠ Erro ao converter {old_file.name}: {e}")

            # 2. Corrige .md antigos (sem rebaixar!)
            for md_file in mat_path.glob('*.md'):
                try:
                    content = md_file.read_text(encoding='utf-8', errors='replace')
                    original_size = len(content)
                    if original_size < 50:
                        continue  # Arquivo vazio/muito pequeno, ignora

                    needs_fix = False
                    md_content = content

                    # Caso A: .md que é na verdade JSON com transcripts (words array)
                    if content.lstrip().startswith('{') and '"transcripts"' in content[:500]:
                        try:
                            data = json.loads(content)
                            if 'transcripts' in data and isinstance(data['transcripts'], list):
                                parts = [t.get('transcript', '') for t in data['transcripts']
                                         if isinstance(t, dict)]
                                md_content = '\n\n'.join(parts)
                                needs_fix = True
                                logger.info(f"  📝 .md com JSON/transcripts → texto puro ({len(parts)} blocos)")
                        except Exception:
                            pass

                    # Caso B: .md que tem HTML tags (não foi limpo direito)
                    if not needs_fix and ('<p>' in content or '<strong>' in content or
                                          '<html' in content.lower() or '<br' in content or
                                          '<div' in content or '<span' in content or
                                          '<a href' in content.lower()):
                        md_content = self._html_to_markdown(content)
                        needs_fix = True
                        logger.info(f"  📝 .md com HTML tags → MD limpo")

                    # Caso C: .md muito grande (>200KB) que pode ter timestamps
                    # Remove timecodes como [00:01:23] ou 00:00:10→
                    if not needs_fix and original_size > 200000:
                        # Procura padrões de timestamp
                        has_timestamps = bool(re.search(r'\[\d{2}:\d{2}:\d{2}\]', content[:5000]) or
                                            re.search(r'\d{2}:\d{2}:\d{2}[→\-]', content[:5000]))
                        if has_timestamps:
                            # Remove timestamps no formato [HH:MM:SS] ou [MM:SS]
                            cleaned = re.sub(r'\[\d{1,2}:\d{2}(:\d{2})?\]', '', md_content)
                            # Remove timestamps no formato 00:00:00→ ou 00:00:00 -
                            cleaned = re.sub(r'\d{1,2}:\d{2}:\d{2}[→\-\s]*', '', cleaned)
                            if len(cleaned) < len(md_content) * 0.95:  # só se removeu pelo menos 5%
                                md_content = cleaned
                                needs_fix = True
                                logger.info(f"  📝 .md com timestamps → removido (redução {original_size - len(md_content)} chars)")

                    if needs_fix:
                        md_file.write_text(md_content, encoding='utf-8')
                        converted += 1
                        reduction = 100 - len(md_content) * 100 // original_size if original_size > 0 else 0
                        logger.info(f"  📝 Corrigido: {md_file.name} ({original_size} → {len(md_content)} chars, -{reduction}%)")
                except Exception as e:
                    logger.warning(f"  ⚠ Erro ao corrigir {md_file.name}: {e}")

            if converted > 0:
                logger.info(f"  ✅ {converted} arquivo(s) processados")
        except Exception as e:
            logger.warning(f"  ⚠ Erro ao escanear pasta material/: {e}")

    def _try_generate_ai_ebook(self, ebook_url: str):
        """Tenta gerar AI ebook via POST gerarAIEbook quando getEbookAI retorna 404."""
        # Extrai token do URL: getEbookAI?token=XXX
        m = re.search(r'token=([^&]+)', ebook_url)
        if not m:
            return
        token = m.group(1)
        portal_base = "https://portal2025.papaconcursos.com.br"
        try:
            logger.info(f"  🔧 Tentando gerar AI ebook via POST gerarAIEbook (token={token[:20]}...)...")
            resp = self.session.post(
                f"{portal_base}/portal/gerarAIEbook",
                json={"token": token, "tokenCurso": ""},
                headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    new_token = data.get('token') or data.get('id') or token
                    logger.info(f"  ✓ AI ebook gerado: {new_token[:20]}...")
                except Exception:
                    logger.info(f"  ✓ AI ebook gerado (sem JSON response)")
            else:
                logger.warning(f"  ⚠ gerarAIEbook HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"  ⚠ gerarAIEbook erro: {e}")

# ============================================================================
# MAIN
# ============================================================================
def main():
    downloader = PapaDownloader()
    downloader.login(
        email="kaique.novaes.wf923@mailinator.com",
        jsessionid="30F512A9E035170C684EC7125FD74D06",
        chave="l02sMWcNdb_cq-OCH8eLgbKgDavxyawqCNoMIZH6xXU"
    )

    courses = [
    
        {"id": "0ae01a07cff6960f557c76020cbb289d", "name": "novo-projeto-tj-tre-trt-trf"},
        {"id": "7e58074e929525b657a17f4faff99b9c", "name": "novo-projeto-tj-tre-trt-trf"},
        {"id": "837d3cd7a5c6d917e601229daee32ed5", "name": "novo-projeto-tj-tre-trt-trf"},
        {"id": "27fc49e447f028c71940dbb7939b3e84", "name": "novo-projeto-tj-tre-trt-trf"},        
        

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
