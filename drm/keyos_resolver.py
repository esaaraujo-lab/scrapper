import re
import requests
from bs4 import BeautifulSoup
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import Pssh

KEYOS_LICENSE_URL = "https://playready.keyos.com/api/v4/getLicense"  # URL do servidor de licença KeyOS do portal

def get_pssh_from_mpd(mpd_url: str, session: requests.Session) -> str:
    """Baixa o MPD e extrai a tag PSSH."""
    resp = session.get(mpd_url, verify=False)
    soup = BeautifulSoup(resp.content, 'xml')
    pssh_element = soup.find('cenc:pssh')
    if pssh_element:
        return pssh_element.text.strip()
    
    # Fallback: regex no texto do MPD caso não ache via XML
    match = re.search(r'<cenc:pssh[^>]*>(.*?)</cenc:pssh>', resp.text)
    if match:
        return match.group(1).strip()
        
    raise ValueError("PSSH não encontrado no arquivo MPD.")

def fetch_drm_key_automatically(mpd_url: str, keyos_token: str, session: requests.Session, wvd_path: str = "device.wvd") -> str:
    """
    Simula o CDM Widevine, envia o desafio ao KeyOS e retorna a chave 'KID:KEY'.
    """
    # 1. Extrair PSSH do MPD
    pssh_str = get_pssh_from_mpd(mpd_url, session)
    pssh = Pssh(pssh_str)

    # 2. Carregar dispositivo L3 e iniciar sessão CDM
    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    challenge = cdm.get_license_challenge(session_id, pssh)

    # 3. Fazer requisição POST para o servidor de licença do KeyOS
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Content-Type': 'application/octet-stream',
        'customdata': keyos_token  # Token XML em Base64 configurado no seu script
    }
    
    license_resp = session.post(KEYOS_LICENSE_URL, data=challenge, headers=headers, verify=False)
    license_resp.raise_for_status()

    # 4. Processar a licença retornada e extrair o par KID:KEY
    cdm.parse_license(session_id, license_resp.content)
    
    keys = []
    for key in cdm.get_keys(session_id):
        if key.type == 'OPERATIONAL':
            keys.append(f"{key.kid.hex}:{key.key.hex()}")
            
    cdm.close(session_id)

    if not keys:
        raise RuntimeError("Nenhuma chave operacional encontrada na resposta do servidor de licença.")

    # Retorna a primeira chave no formato KID:KEY
    return keys[0]