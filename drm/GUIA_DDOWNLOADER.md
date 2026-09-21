# 🚀 Guia Avançado - PapaSantos v2 com DDownloader

## O que é DDownloader?

**DDownloader** é uma biblioteca Python para gerenciamento **avançado e eficiente de downloads** com:

- ✅ Múltiplos workers paralelos
- ✅ Retry automático inteligente
- ✅ Progress callbacks
- ✅ Limite de taxa
- ✅ Gerenciamento de conexões
- ✅ Tratamento robusto de erros

📚 Documentação oficial: https://pypi.org/project/DDownloader/

---

## Instalação

### Automática
```bash
# Windows
setup_windows.bat

# Linux/Mac
bash setup_linux.sh
```

### Manual
```bash
pip install DDownloader
```

---

## Integração com PapaSantos v2

### Arquivo: `psantos_v2_advanced.py`

```python
# Detecta automaticamente se DDownloader está disponível
USE_DDOWNLOADER = DDOWNLOADER_AVAILABLE

# Configurações DDownloader
DDOWNLOADER_WORKERS = 4      # Número de workers paralelos
DDOWNLOADER_RETRIES = 3      # Tentativas de retry
```

### Modo de Funcionamento

```
┌─────────────────────┐
│  Download Iniciado  │
└──────────┬──────────┘
           │
           ▼
    ┌──────────────┐
    │DDownloader   │
    │ disponível?  │
    └─┬──────────┬─┘
      │          │
    Sim│         │Não
      │          │
      ▼          ▼
   ┌────────┐  ┌────────┐
   │Usar DD │  │requests│
   │(rápido)   │(fallback)
   └────────┘  └────────┘
```

---

## Configuração Avançada

### 1. Aumentar Workers (Mais Rápido)

```python
# Em psantos_v2_advanced.py
DDOWNLOADER_WORKERS = 8      # Aumenta paralelismo
```

⚠️ **Cuidado:** Não exagere, pode sobrecarregar a rede

**Recomendações:**
- Para conexão 100 Mbps: 4-6 workers
- Para conexão 50 Mbps: 2-4 workers
- Para conexão < 30 Mbps: 1-2 workers

### 2. Aumentar Retries (Mais Robusto)

```python
DDOWNLOADER_RETRIES = 5      # Mais tentativas
```

**Trade-off:**
- ✅ Mais tentativas = menos falhas
- ❌ Mais lento em case de falha

### 3. Callbacks de Progresso

```python
def meu_callback(downloaded, total):
    percentual = (downloaded / total) * 100
    print(f"Progresso: {percentual:.1f}% ({downloaded}/{total})")

# Use em código customizado:
wrapper.client.download_url(
    url="https://...",
    output_path="/path/to/file",
    progress_callback=meu_callback
)
```

---

## Exemplos Práticos

### Exemplo 1: Uso Automático (Recomendado)

```python
from psantos_v2_advanced import PapaDownloader

# Simplesmente usar - detecta DDownloader automaticamente
downloader = PapaDownloader()
downloader.login(email="...", jsessionid="...", chave="...")
downloader.download_course("course_id", "course_name")

# Se DDownloader está disponível, ele será usado
# Se não, fallback automático para yt-dlp/requests
```

### Exemplo 2: Forçar DDownloader

```python
from psantos_v2_advanced import PapaDownloader, DDownloaderWrapper

downloader = PapaDownloader()

# Verificar se disponível
if downloader.ddownloader.available:
    print("✓ DDownloader disponível")
else:
    print("✗ DDownloader não disponível, usando fallback")

downloader.login(...)
downloader.download_course(...)
```

### Exemplo 3: Download Manual com DDownloader

```python
from DDownloader import Downloader
import os

# Criar instância
dd = Downloader(
    output_dir="./downloads",
    max_workers=4,
    retry_attempts=3
)

# Download único
url = "https://cdn.example.com/file.mp4"
output = "./downloads/arquivo.mp4"

def progresso(downloaded, total):
    percent = (downloaded/total) * 100
    print(f"[{percent:05.1f}%] {downloaded}/{total}")

dd.download_url(
    url,
    output_path=output,
    progress_callback=progresso
)

print("✓ Download completo!")
```

### Exemplo 4: Múltiplos Downloads em Paralelo

```python
from DDownloader import Downloader
import concurrent.futures

dd = Downloader(
    output_dir="./videos",
    max_workers=4,
    retry_attempts=3
)

urls = [
    ("video1.mp4", "https://..."),
    ("video2.mp4", "https://..."),
    ("video3.mp4", "https://..."),
]

def baixar(nome, url):
    path = f"./videos/{nome}"
    dd.download_url(url, output_path=path)
    print(f"✓ {nome}")

# Downloads paralelos
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    for nome, url in urls:
        executor.submit(baixar, nome, url)

print("✓ Todos os downloads completos!")
```

### Exemplo 5: Com Gerenciamento de Erros

```python
from DDownloader import Downloader

dd = Downloader(
    output_dir="./downloads",
    max_workers=4,
    retry_attempts=5  # Mais tentativas para robustez
)

url = "https://cdn.example.com/file.mp4"
output = "./downloads/arquivo.mp4"

try:
    dd.download_url(url, output_path=output)
    print("✓ Download sucesso!")
    
except Exception as e:
    print(f"✗ Erro ao baixar: {e}")
    # Tentar fallback manual
    import requests
    resp = requests.get(url, timeout=30)
    with open(output, 'wb') as f:
        f.write(resp.content)
    print("✓ Fallback manual bem-sucedido")
```

---

## Comparação: yt-dlp vs DDownloader

| Aspecto | yt-dlp | DDownloader |
|---------|--------|-------------|
| **Streams DASH/HLS** | ✅ Nativo | ⚠️ Manual |
| **Paralelismo** | ✅ Integrado | ✅ Configurável |
| **DRM Widevine** | ✅ Suporta | ❌ Não |
| **Simplicicidade** | ❌ Complexo | ✅ Simples |
| **Performance Bruta** | ⚠️ Médio | ✅ Excelente |
| **Uso de Memória** | ⚠️ Médio | ✅ Baixo |
| **Compatibilidade** | ✅ Ampla | ⚠️ Limitada |

**Conclusão:** 
- Use **yt-dlp** para streams complexos e DRM
- Use **DDownloader** para downloads diretos de arquivos

---

## Otimizações

### 1. Para Conexão Lenta

```python
# Em psantos_v2_advanced.py
DDOWNLOADER_WORKERS = 1       # Menos paralelo
TIMEOUT = 60                  # Mais tolerância
RETRY_ATTEMPTS = 5            # Mais tentativas
BATCH_DELAY = 1.0             # Mais espaçamento
```

### 2. Para Conexão Rápida

```python
# Em psantos_v2_advanced.py
DDOWNLOADER_WORKERS = 8       # Mais paralelo
TIMEOUT = 15                  # Menos tolerância
RETRY_ATTEMPTS = 2            # Menos tentativas
BATCH_DELAY = 0.1             # Menos espaçamento
```

### 3. Para Servidor com Rate Limit

```python
# Respeitar rate limits
BATCH_DELAY = 2.0             # Esperar 2s entre requisições
DDOWNLOADER_WORKERS = 1       # Apenas 1 worker
```

---

## Troubleshooting

### Erro: "DDownloader não encontrado"

**Causa:** DDownloader não instalado

**Solução:**
```bash
pip install DDownloader
```

### Erro: "Max retries exceeded"

**Causa:** Servidor recusando conexões

**Soluções:**
```python
# 1. Aumentar timeout
TIMEOUT = 60

# 2. Reduzir workers
DDOWNLOADER_WORKERS = 1

# 3. Adicionar delays
BATCH_DELAY = 2.0

# 4. Aumentar retries
DDOWNLOADER_RETRIES = 5
```

### Erro: "Connection reset by peer"

**Causa:** Instabilidade de rede

**Soluções:**
```python
# Configurar retry automático
dd = Downloader(
    output_dir="./downloads",
    max_workers=2,              # Reduzir paralelismo
    retry_attempts=10,          # Muitas tentativas
    timeout=60                  # Timeout longo
)
```

### Downloads são lentos

**Causas Comuns:**
1. Workers insuficientes
2. Timeout muito curto
3. Rate limit do servidor
4. Conexão de internet lenta

**Diagnosticar:**
```python
# Ver logs detalhados
import logging
logging.basicConfig(level=logging.DEBUG)

downloader.download_course(...)
```

---

## Métricas e Monitoramento

### Logs Automáticos

Todos os downloads são registrados em:
```
D:/Papa Concursos 2026/logs/downloader_YYYYMMDD_HHMMSS.log
```

### Estatísticas

```python
# No final da execução
print(f"Chamadas KeyOS: {downloader.keyos_resolver.api_calls}")
print(f"Cache de chaves: {len(downloader.keyos_resolver.key_cache)}")
print(f"Tempo total: {elapsed:.1f}s")
print(f"Vídeos: {downloader.successful_videos}/{downloader.successful_videos + downloader.failed_videos}")
print(f"PDFs: {downloader.successful_pdfs}/{downloader.successful_pdfs + downloader.failed_pdfs}")
```

---

## Casos de Uso Ideais

### ✅ Use DDownloader Para:
- Arquivos diretos (PDFs, ZIP, etc)
- CDNs com conexões estáveis
- Downloads simples sem DRM
- Alta performance em rede boa

### ❌ Não Use DDownloader Para:
- Streams DASH/HLS
- Conteúdo com Widevine DRM
- Navegação de websites
- Conteúdo que requer JavaScript

---

## Combinação Ideal: yt-dlp + DDownloader

**PapaSantos v2 Advanced usa o melhor dos dois mundos:**

```
Vídeos DRM    → yt-dlp (com Widevine)
Vídeos simples → DDownloader (rápido)
PDFs diretos  → DDownloader (rápido)
Fallback      → requests/yt-dlp
```

---

## Performance Esperada

### Com DDownloader (4 workers)

| Tipo | Velocidade |
|------|-----------|
| PDF (5 MB) | 1-2s |
| Vídeo (500 MB, CDN rápido) | 30-60s |
| Vídeo com DRM | 2-5min |

### Sem DDownloader (yt-dlp)

| Tipo | Velocidade |
|------|-----------|
| PDF (5 MB) | 2-3s |
| Vídeo (500 MB, CDN rápido) | 60-120s |
| Vídeo com DRM | 2-5min |

---

## Dicas Finais

1. **Comece com valores padrão** - Eles já são otimizados
2. **Monitore os logs** - Veja em `LOGS_DIR`
3. **Teste com um arquivo** - Antes de tudo
4. **Respeite rate limits** - Não sobrecarregue servidores
5. **Use HTTPS sempre** - Quando possível

---

## Links Úteis

- 📦 **DDownloader PyPI:** https://pypi.org/project/DDownloader/
- 📖 **yt-dlp Docs:** https://github.com/yt-dlp/yt-dlp
- 🔐 **pywidevine:** https://github.com/devine-dl/pywidevine
- 📝 **FFmpeg Wiki:** https://trac.ffmpeg.org/wiki

---

**Versão:** Advanced 2.0  
**Com DDownloader integrado:** ✅ Sim  
**Status:** ✅ Produção

Bom uso! 🚀
