"""
Otonom GitHub Dokümantasyon Ajanı — FastAPI backend.
SSE ile canlı log akışı; PyGithub ile repo analizi ve PR oluşturma; Gemini ile README üretimi.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Callable
from urllib.parse import urlparse

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from github import Github, GithubException
from github.Repository import Repository
from pydantic import BaseModel, Field

_BACKEND_ROOT = Path(__file__).resolve().parent
# Windows'ta kullanıcı ortamında boş/wrong GITHUB_TOKEN varsa varsayılan load_dotenv bunu ezmez; .env öncelikli olsun.
load_dotenv(_BACKEND_ROOT / ".env", override=True)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Örn: gemini-2.5-flash, gemini-flash-latest — boşsa aşağıdaki yedek sıra kullanılır
GEMINI_MODEL_PREF = os.getenv("GEMINI_MODEL", "").strip()


def reload_local_env_into_globals() -> None:
    """`.env` dosyası güncellendiğinde uvicorn sürecinin eski token ile kalmasını önler."""
    load_dotenv(_BACKEND_ROOT / ".env", override=True)
    global GITHUB_TOKEN, GEMINI_API_KEY, GEMINI_MODEL_PREF
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL_PREF = os.getenv("GEMINI_MODEL", "").strip()

# Eski gemini-1.5-flash kimliği birçok projede 404 veriyor; sırayla güncel adlar denenir
_GEMINI_MODEL_FALLBACKS = (
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash",
    "gemini-1.5-flash-002",
)

ALLOWED_EXTENSIONS = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".html", ".css", ".json", ".md", ".java", ".cpp"}
)
BINARY_EXTENSIONS = frozenset({".exe", ".dll", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".pdf", ".zip"})
SKIP_PATH_SEGMENTS = frozenset(
    {"node_modules", "venv", ".venv", "env", ".git", "dist", "build", "__pycache__", ".idea", ".vscode"}
)

MAX_CONTEXT_CHARS = 900_000
FRONTEND_ORIGINS = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:5174,http://127.0.0.1:5174,"
    "http://localhost:5175,http://127.0.0.1:5175",
).split(",")

SYSTEM_PROMPT = (
    "Sen uzman bir yazılım mimarı ve teknik yazarsın. Sana bir projenin dosya yolları özeti ve kaynak kodlarını "
    "veriyorum. GitHub için profesyonel, okunaklı bir README.md üret.\n\n"
    "DİL: Tüm README baştan sona Türkçe olmalı; İngilizce başlık veya paragraf karıştırma.\n\n"
    "Aşağıdaki ## bölümlerini bu sırayla kullan. Depoda ilgili iz yoksa o bölümde kısaça 'Bu depoda doğrulanamadı; "
    "eklenmesi önerilir.' de ve abartılı varsayım yapma. Uydurma komut, sürüm veya URL kullanma; yalnızca bağlamda "
    "görünen dosya ve içeriklere dayan. composer.json, package.json, pyproject.toml, Dockerfile, docker-compose "
    "dosyaları bağlamda varsa oradaki gerçek script adları, bağımlılık sürümleri ve komutları aynen yansıt.\n\n"
    "GÜVENLİK: .env içeriği, API anahtarı, token, parola veya gizli değer örneği yazma; yer tutucu kullan "
    "(ör. YOUR_API_KEY). Gizli dosya adlarını övünerek listeleme.\n\n"
    "## İçindekiler\n"
    "Aşağıdaki her ## başlığı için bir satır Markdown bağlantısı ver (ör. [Özet](#özet)).\n\n"
    "## Özet\n"
    "Projenin ne işe yaradığını 4–7 tam cümleyle anlat: hedef kullanıcı, çözdüğü problem, ana işlevler. "
    "Teknik detayı burada hafif tut; Kurulum ve Mimari’de derinleştir.\n\n"
    "## Özellikler\n"
    "8–14 madde; her madde kullanıcıya veya geliştiriciye dokunan somut bir kazanım yazsın (teknik jargonsa kısa "
    "açılım parantez içinde). Bağlamda net kanıt yoksa genel ifade yerine 'önerilir / doğrulanmalı' dili kullan.\n\n"
    "## Gereksinimler\n"
    "Bağlamdan çıkarılabilen çalışma zamanı (ör. PHP, Node, Python sürümü), veritabanı, önbellek veya diğer "
    "dış servis ihtiyaçlarını madde veya kısa tablo ile listele. Kesin sürüm yoksa 'minimum önerilen' ifadesiyle "
    "dürüst ol.\n\n"
    "## Kurulum ve çalıştırma\n"
    "Numaralı adımlar (1. 2. 3. …). Her adımda mümkünse bir komut veya dosya yolu; bağlamda geçen gerçek "
    "komutları kullan (composer install, npm ci, php artisan migrate, docker compose up vb.). "
    "Klonlama ve .env örneği için: 'cp .env.example .env' gibi ifadeler yalnızca depoda .env.example veya benzeri "
    "varsa veya standart Laravel/Node kalıbı bağlamda açıkça görülüyorsa yaz; yoksa genel olarak "
    "'Ortam değişkenlerini yapılandırın' de.\n\n"
    "## Yapılandırma\n"
    "Markdown tablosu: sütunlar tam olarak 'Değişken', 'Açıklama', 'Zorunlu' (Evet/Hayır/İsteğe bağlı). "
    "Bağlamda .env.example veya dokümante edilmiş anahtarlar varsa onları özetle. Yoksa: hangi tür anahtarların "
    "genelde gerekli olabileceğini kategorisel anlat; gerçek değer veya örnek sır verme.\n\n"
    "## Kullanılan teknolojiler\n"
    "Kısa liste veya tablo: dil, framework, önemli paketler (bağlamdaki manifest dosyalarından).\n\n"
    "## Mimari ve klasör yapısı\n"
    "Önce 'hangi üst klasör ne işe yarıyor' özeti (2–4 paragraf). Ardından aşağıdaki GÖRÜNÜM kurallarına uy:\n"
    "- Uzun ASCII ağaç (├──, └──, │) kullanma.\n"
    "- Markdown tablosu: sütunlar tam olarak 'Bölüm / klasör' ve 'Kısa açıklama' (Türkçe, tek satır).\n"
    "- İsteğe bağlı: tek bir ```mermaid fenced blok. Mermaid 11 kurallarına kesin uy:\n"
    "  * flowchart TB veya graph LR kullan; subgraph kullanma (hata riski yüksek).\n"
    "  * Her düğüm kimliği yalnız harf/rakam/alt çizgi (ör. ds, lab, lib1); nokta veya '/' kimlik olamaz.\n"
    "  * Kök için asla tek başına '.' kullanma; anlamı köşeli parantez içinde yaz: ör. root_node[\"Depo kökü\"].\n"
    "  * Düğüm yazımı: kimlik[\"Görünen kısa metin\"] veya kimlik[metin]; bağlantı: A --> B.\n"
    "  * Mermaid bloğunun içine HTML, <style>, %%{init: ...}%% veya satır dışı CSS ekleme; bunlar parse hatası "
    "üretir. classDef kullanma (model hatalarına açık).\n"
    "  * En fazla 18 düğüm ve 24 ok; sade yapı.\n"
    "  * Geçerli mini örnek (uyarlama için):\n"
    "```mermaid\nflowchart TB\n  root_node[\"Depo\"]\n  lib_node[\"lib\"]\n  root_node --> lib_node\n```\n"
    "- Mermaid emin değilsen bloğu tamamen atla; tabloyu genişleterek anlat.\n"
    "- Uzun liste için yalnızca şu HTML kalıbını kullan: <details><summary>Detaylı yapı</summary> … </details>.\n"
    "- Flutter vb. için ios/android/macos/web/windows tabloda kısa satırlar.\n"
    "- Bağlamdaki 'Depo yapısı özeti' verisi ile çelişme.\n\n"
    "## API veya uç noktalar\n"
    "routes, OpenAPI, controller isimleri bağlamda seçilebiliyorsa yüksek seviye gruplar (3–8 madde veya kısa "
    "tablo). Her endpoint uydurma; isimler dosyalardan gelmeli veya genel modül adıyla sınırlı kalmalı.\n\n"
    "## Test ve kalite\n"
    "package.json scripts, composer scripts, Makefile veya CI yapılandırması bağlamda varsa gerçek test/lint "
    "komutlarını yaz. Yoksa hangi tür komutların eklenebileceğini öner.\n\n"
    "## Dağıtım ve üretim notları\n"
    "Dockerfile, compose, Procfile, nginx örneği bağlamda varsa özetle (2–6 paragraf). Yoksa bölümü tek cümleyle "
    "atlat: üretim için nelerin dokümante edilmesi gerektiğini belirt.\n\n"
    "## Katkıda bulunma\n"
    "Dal politikası, issue/PR kısa davet (1–3 paragraf). Repoda CONTRIBUTING yoksa nötr ve kısa tut.\n\n"
    "## Lisans\n"
    "LICENSE veya benzeri dosya adı bağlamda görünüyorsa kısaca belirt. Görünmüyorsa: 'Lisans dosyası "
    "belirtilmemiştir; LICENSE eklenmesi önerilir.' yaz; uydurma lisans adı verme.\n\n"
    "Çıktı yalnızca README içeriği; ekstra sohbet metni veya 'tabii işte' gibi ön ek yok."
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Hangi main.py'nin yüklendiğini terminale yazar (yanlış klasör / eski süreç ayıklaması için)."""
    print(f"[DerinNLP] Aktif backend dosyası: {Path(__file__).resolve()}", flush=True)
    yield


app = FastAPI(
    title="Otonom GitHub Dokümantasyon Ajanı",
    version="1.0.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in FRONTEND_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-DerinNLP-Docs-Agent"],
)


@app.middleware("http")
async def derinnlp_response_marker(request, call_next):
    """Başka bir süreç 8000'i dinliyorsa ayırt etmek için başlık ekler."""
    response = await call_next(request)
    response.headers["X-DerinNLP-Docs-Agent"] = "1"
    return response


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(..., min_length=10, description="GitHub repository HTTPS URL")


class ProfileReposRequest(BaseModel):
    profile_url: str = Field(..., min_length=8, description="GitHub kullanıcı profil URL'si (örn. https://github.com/kullaniciadi)")


# github.com/<tek_segment> — repo değil, ayırıcı veya rezerve yollar
_GITHUB_PROFILE_RESERVED = frozenset(
    {
        "about",
        "collections",
        "customer-stories",
        "enterprise",
        "explore",
        "features",
        "git-guides",
        "issues",
        "login",
        "marketplace",
        "orgs",
        "pricing",
        "pulls",
        "readme",
        "security",
        "settings",
        "signup",
        "sponsors",
        "team",
        "topics",
    }
)
_GITHUB_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")


def sse_pack(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def parse_github_repo(full_url: str) -> tuple[str, str]:
    raw = full_url.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]

    slash_parts = [p for p in raw.replace("\\", "/").split("/") if p]
    if (
        len(slash_parts) == 2
        and "github.com" not in slash_parts[0].lower()
        and "." not in slash_parts[0]
    ):
        return slash_parts[0], slash_parts[1]

    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    if not host or "github.com" not in host:
        raise ValueError("Yalnızca github.com adresleri destekleniyor.")

    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if len(path_parts) < 2:
        raise ValueError("owner/repo çözümlenemedi; tam GitHub HTTPS URL'si veya owner/repo girin.")
    return path_parts[0], path_parts[1]


def parse_github_user_profile_url(full_url: str) -> str:
    """
    Örnek: https://github.com/Burakgul3085 veya github.com/Burakgul3085 → login.
    owner/repo veya tek segment rezerve kelime ise ValueError.
    """
    raw = full_url.strip().rstrip("/")
    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    if not host or "github.com" not in host:
        raise ValueError("Yalnızca github.com profil adresleri destekleniyor.")

    path_parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if len(path_parts) != 1:
        raise ValueError(
            "Profil adresi tek kullanıcı adı içermeli (örn. https://github.com/kullaniciadi). "
            "Depo için mevcut «Repo URL» alanını kullanın."
        )
    login = path_parts[0]
    if login.lower() in _GITHUB_PROFILE_RESERVED:
        raise ValueError("Bu yol bir kullanıcı profili değil; github.com/kullaniciadi biçiminde girin.")
    if not _GITHUB_LOGIN_RE.match(login):
        raise ValueError("Geçersiz GitHub kullanıcı adı.")
    return login


def path_should_skip(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    base = os.path.basename(normalized)
    if base == ".env" or base.startswith(".env."):
        return True
    lower = normalized.lower()
    for seg in SKIP_PATH_SEGMENTS:
        if f"/{seg}/" in f"/{lower}/" or lower.startswith(f"{seg}/") or lower.endswith(f"/{seg}") or lower == seg:
            return True
    ext = os.path.splitext(base)[1].lower()
    if ext and ext in BINARY_EXTENSIONS:
        return True
    return False


def build_compact_structure_markdown(paths: list[str]) -> str:
    """README tablosu ve Mermaid için modele üst-seviye özet (dosya sayıları)."""
    root_total: dict[str, int] = defaultdict(int)
    root_sub: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in paths:
        segs = [s for s in p.replace("\\", "/").split("/") if s]
        if not segs:
            continue
        r = segs[0]
        root_total[r] += 1
        if len(segs) >= 2:
            root_sub[r][segs[1]] += 1

    lines: list[str] = [
        "| Üst klasör / kök dosya | Bu dalda yaklaşık kaynak dosya sayısı |",
        "|---|---:|",
    ]
    for r in sorted(root_total.keys(), key=lambda k: (-root_total[k], k))[:22]:
        safe = r.replace("|", "/")
        lines.append(f"| `{safe}` | {root_total[r]} |")
    if len(root_total) > 22:
        lines.append(f"| … | (+{len(root_total) - 22} diğer) |")

    lines.append("")
    lines.append("Öne çıkan alt klasörler (üst klasör başına en fazla 6 alt öğe):")
    for r in sorted(root_total.keys(), key=lambda k: (-root_total[k], k))[:10]:
        subs = root_sub.get(r, {})
        if not subs:
            continue
        lines.append(f"- **`{r}/`**")
        for s in sorted(subs.keys(), key=lambda k: (-subs[k], k))[:6]:
            lines.append(f"  - `{s}/` — {subs[s]} dosya")
        if len(subs) > 6:
            lines.append(f"  - … (+{len(subs) - 6} alt öğe)")
    return "\n".join(lines)


def collect_context_from_repo(g: Github, owner: str, repo_name: str, log: Callable[[str], None]) -> str:
    repo = g.get_repo(f"{owner}/{repo_name}")
    default_branch = repo.default_branch
    log(f"[2/5] Varsayılan dal: {default_branch}; içerik ağacı alınıyor…")

    branch_ref = repo.get_branch(default_branch)
    tree = repo.get_git_tree(branch_ref.commit.sha, recursive=True)

    blobs: list[tuple[str, str]] = []
    total_chars = 0
    truncated = False

    for item in tree.tree:
        if item.type != "blob":
            continue
        path = item.path
        if path_should_skip(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        try:
            content_file = repo.get_contents(path, ref=default_branch)
        except GithubException:
            continue
        if isinstance(content_file, list):
            continue
        if getattr(content_file, "type", "") == "dir":
            continue
        if getattr(content_file, "size", 0) > 512_000:
            continue
        raw_b64 = getattr(content_file, "content", None)
        if not raw_b64:
            continue
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
        except Exception:
            continue
        chunk = f"\n\n===== DOSYA: {path} =====\n{decoded}"
        if total_chars + len(chunk) > MAX_CONTEXT_CHARS:
            truncated = True
            remaining = MAX_CONTEXT_CHARS - total_chars
            if remaining > 500:
                blobs.append((path, decoded[: max(0, remaining - 200)] + "\n… [dosya bağlam limiti nedeniyle kesildi]"))
                total_chars = MAX_CONTEXT_CHARS
            break
        blobs.append((path, decoded))
        total_chars += len(chunk)

    if truncated:
        log(
            "[2/5] Bilgi: Kod ve yapı özeti, yapılandırılmış bağlam kotası çerçevesinde derlenerek "
            "dokümantasyon modeline aktarıldı."
        )

    log(f"[2/5] {len(blobs)} kaynak dosya birleştirildi (~{total_chars} karakter).")

    structure_lines = sorted({p for p, _ in blobs})
    structure_compact = build_compact_structure_markdown(structure_lines)
    sample_lines = structure_lines[:150]
    sample_flat = "\n".join(sample_lines)
    if len(structure_lines) > 150:
        sample_flat += f"\n… (düz yol listesi kısaltıldı; toplam {len(structure_lines)} yol)"

    parts: list[str] = [
        "README'de klasör yapısını tablo + ```mermaid``` bloğu ile sun. Aşağıda özet tablo, örnek yollar "
        "ve kaynak kodlar var.\n\n",
        "## Depo yapısı özeti (tablo ve Mermaid için referans)\n",
        structure_compact,
        "\n\n## Dosya yolları (örnek)\n",
        sample_flat,
        "\n\n## Kaynak içerikler\n",
    ]
    for path, body in blobs:
        parts.append(f"\n\n===== DOSYA: {path} =====\n{body}")
    return "".join(parts)


def user_has_repo_push(repo: Repository) -> bool:
    """Token sahibinin bu repoda doğrudan push (dal/commit) yetkisi var mı?"""
    perms = getattr(repo, "permissions", None)
    if perms is None:
        return False
    return bool(
        getattr(perms, "admin", False)
        or getattr(perms, "maintain", False)
        or getattr(perms, "push", False)
    )


def wait_until_fork_ready(g: Github, fork: Repository, log: Callable[[str], None]) -> Repository:
    """
    GitHub fork işlemi sunucuda asenkron tamamlanır; ref/dal erişilebilir olana kadar bekler.
    """
    full = fork.full_name
    delays = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0] + [5.0] * 12

    for attempt, delay in enumerate(delays):
        try:
            r = g.get_repo(full)
            db = r.default_branch
            r.get_branch(db)
            r.get_git_ref(f"heads/{db}")
            return r
        except GithubException:
            log(f"[4/5] Fork hazırlanıyor… ({attempt + 1}/{len(delays)})")
            time.sleep(delay)

    raise RuntimeError(
        "Fork zaman aşımı: GitHub fork işlemi tamamlanmadı. Bir süre sonra tekrar deneyin."
    )


def get_or_create_user_fork(
    g: Github,
    upstream: Repository,
    log: Callable[[str], None],
) -> Repository:
    """
    Upstream için token sahibinin hesabında fork oluşturur veya mevcut fork'u kullanır.
    """
    user = g.get_user()
    login = user.login
    upstream_full = upstream.full_name

    try:
        cand = g.get_repo(f"{login}/{upstream.name}")
        parent = getattr(cand, "parent", None)
        if cand.fork and parent is not None and parent.full_name.lower() == upstream_full.lower():
            log(f"[4/5] Bu repoya ait mevcut fork bulundu: {cand.full_name}")
            return wait_until_fork_ready(g, cand, log)
    except GithubException:
        pass

    log("[4/5] Upstream henüz fork değil; GitHub üzerinde fork oluşturuluyor…")
    try:
        fork = upstream.create_fork()
    except GithubException as ex:
        msg = ""
        if isinstance(ex.data, dict):
            msg = str(ex.data.get("message", ""))
        errs = ex.data.get("errors") if isinstance(ex.data, dict) else None
        if errs:
            msg += " " + str(errs)
        low = msg.lower()
        if ex.status == 422 and (
            "already exists" in low
            or "already been forked" in low
            or "name already exists" in low
        ):
            fork = g.get_repo(f"{login}/{upstream.name}")
        else:
            raise RuntimeError(
                f"Fork oluşturulamadı: {ex.data.get('message', str(ex)) if isinstance(ex.data, dict) else ex}"
            ) from ex

    log(f"[4/5] Fork kaydı: {fork.full_name}; sunucu senkronu bekleniyor…")
    return wait_until_fork_ready(g, fork, log)


def commit_readme_on_branch(
    repo: Repository,
    branch: str,
    readme_markdown: str,
    commit_message: str,
) -> None:
    try:
        existing = repo.get_contents("README.md", ref=branch)
        sha_existing = existing.sha
        repo.update_file(
            path="README.md",
            message=commit_message,
            content=readme_markdown,
            sha=sha_existing,
            branch=branch,
        )
    except GithubException:
        repo.create_file(
            path="README.md",
            message=commit_message,
            content=readme_markdown,
            branch=branch,
        )


def create_branch_from_default(repo: Repository, new_branch: str, log: Callable[[str], None]) -> str:
    base_branch = repo.default_branch
    log(f"[4/5] Varsayılan dal: {base_branch}; yeni dal: {new_branch}")
    base_ref = repo.get_branch(base_branch)
    repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_ref.commit.sha)
    return base_branch


def create_docs_pr(
    g: Github,
    owner: str,
    repo_name: str,
    readme_markdown: str,
    log: Callable[[str], None],
) -> str:
    """
    Yazma yetkisi varsa doğrudan repoda dal+commit+PR.
    Yoksa (tipik: başkasının public reposu) fork üzerinde dal+commit, PR upstream'e head=login:branch.
    """
    upstream = g.get_repo(f"{owner}/{repo_name}")
    if upstream.private and not user_has_repo_push(upstream):
        raise RuntimeError(
            "Bu depo özel ve token ile doğrudan yazma yetkiniz yok; fork akışı yalnızca "
            "herkese açık (public) repolar için kullanılabilir."
        )

    token_user = g.get_user().login
    upstream_owner = upstream.owner.login if upstream.owner else ""
    if (
        not user_has_repo_push(upstream)
        and upstream_owner
        and upstream_owner.lower() == token_user.lower()
    ):
        raise RuntimeError(
            "Bu depo sizin hesabınıza ait ancak token ile yazma (push/PR) yetkisi algılanmadı veya "
            "GitHub API bu işlemi token kapsamına izin vermiyor. Kendi reponuzu «fork» edemezsiniz; "
            "Fine-grained PAT kullanıyorsanız «Public repositories» yalnızca OKUMA içindir — dal/commit/PR "
            "için «All repositories» veya «Only select repositories» ile bu repoyu ekleyin ve "
            "Contents + Pull requests için Read and write verin. Classic PAT için repo kapsamı gerekir."
        )

    ts = int(time.time() * 1000)
    new_branch = f"ai-docs-update-{ts}"
    commit_message = "Ajan: Otonom dokümantasyon oluşturuldu"
    pr_body = (
        "Bu PR, Yapay Zeka Ajanı tarafından repodaki kaynak kodlar analiz edilerek otonom olarak oluşturulmuştur."
    )

    if user_has_repo_push(upstream):
        log("[4/5] Doğrudan yazma yetkisi tespit edildi; dal repoda oluşturuluyor…")
        create_branch_from_default(upstream, new_branch, log)
        commit_readme_on_branch(upstream, new_branch, readme_markdown, commit_message)
        log("[5/5] Pull Request açılıyor (aynı depo)…")
        pr = upstream.create_pull(
            title="docs: Otonom README güncellemesi (AI)",
            body=pr_body,
            head=new_branch,
            base=upstream.default_branch,
        )
        return pr.html_url

    log("[4/5] Doğrudan yazma yetkisi yok; public repo için fork akışına geçiliyor…")
    fork_repo = get_or_create_user_fork(g, upstream, log)
    fork_owner_login = fork_repo.owner.login if fork_repo.owner else g.get_user().login

    create_branch_from_default(fork_repo, new_branch, log)
    commit_readme_on_branch(fork_repo, new_branch, readme_markdown, commit_message)

    log("[5/5] Pull Request açılıyor (fork → upstream)…")
    head = f"{fork_owner_login}:{new_branch}"
    pr = upstream.create_pull(
        title="docs: Otonom README güncellemesi (AI)",
        body=pr_body,
        base=upstream.default_branch,
        head=head,
    )
    return pr.html_url


def _gemini_model_candidates() -> list[str]:
    out: list[str] = []
    if GEMINI_MODEL_PREF:
        out.append(GEMINI_MODEL_PREF)
    out.extend(_GEMINI_MODEL_FALLBACKS)
    seen: set[str] = set()
    uniq: list[str] = []
    for m in out:
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def _looks_like_model_missing_error(err: BaseException) -> bool:
    msg = str(err).lower()
    return "404" in str(err) or "not found" in msg or "is not supported" in msg


def sanitize_readme_mermaid_blocks(readme: str) -> str:
    """Model üretiminde sızan HTML/style Mermaid 11'de parse hatasına yol açar; temizler."""

    def _clean_body(body: str) -> str:
        b = body
        b = re.sub(r"<style[\s\S]*?</style>", "", b, flags=re.IGNORECASE)
        b = re.sub(r"</?style[^>]*>", "", b, flags=re.IGNORECASE)
        b = re.sub(r"%%\{[\s\S]*?\}%%", "", b)
        b = re.sub(r"<[^>\n]{1,200}>", "", b)
        return b.strip()

    def _sub(m: re.Match) -> str:
        return f"```mermaid\n{_clean_body(m.group(1))}\n```"

    return re.sub(r"```mermaid\s*\n([\s\S]*?)```", _sub, readme, flags=re.IGNORECASE)


def generate_readme_with_gemini(context: str, log: Callable[[str], None]) -> str:
    log("[3/5] Gemini ile README üretiliyor…")
    genai.configure(api_key=GEMINI_API_KEY)
    user_payload = (
        "Projeye ait birleştirilmiş bağlam:\n\n"
        + context
        + "\n\nYukarıdaki bilgilere göre tek bir README.md çıktısı üret."
    )

    candidates = _gemini_model_candidates()
    last_err: BaseException | None = None
    chosen: str | None = None
    resp = None

    for model_name in candidates:
        try:
            log(f"[3/5] Denenen model: {model_name}")
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PROMPT,
            )
            resp = model.generate_content(user_payload)
            chosen = model_name
            break
        except Exception as ex:
            last_err = ex
            if _looks_like_model_missing_error(ex):
                log(f"[3/5] Model kullanılamadı ({model_name}), sıradaki deneniyor…")
                continue
            raise

    if resp is None or chosen is None:
        raise RuntimeError(
            "Uygun Gemini modeli bulunamadı. GEMINI_MODEL ortam değişkenini güncelleyin "
            "(ör. gemini-2.5-flash) veya google-generativeai paketini güncelleyin."
        ) from last_err

    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini boş yanıt döndü.")
    fence = re.match(r"^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    text = sanitize_readme_mermaid_blocks(text)
    log(f"[3/5] README taslağı hazır (model: {chosen}).")
    return text


async def run_analyze_pipeline(repo_url: str) -> AsyncGenerator[str, None]:
    reload_local_env_into_globals()

    thread_logs: list[str] = []

    def slog(msg: str) -> None:
        thread_logs.append(msg)

    if not GITHUB_TOKEN or not GEMINI_API_KEY:
        yield sse_pack(
            {
                "type": "error",
                "message": "Sunucu yapılandırması eksik: GITHUB_TOKEN ve GEMINI_API_KEY .env içinde tanımlı olmalı.",
            }
        )
        return

    try:
        owner, name = parse_github_repo(repo_url)
    except ValueError as e:
        yield sse_pack({"type": "error", "message": str(e)})
        return

    g = Github(GITHUB_TOKEN)

    try:

        def connect():
            slog("[1/5] GitHub bağlantısı ve repo erişimi doğrulanıyor…")
            r = g.get_repo(f"{owner}/{name}")
            _ = r.full_name
            slog(f"[1/5] Repo erişildi: {r.full_name} ({'özel' if r.private else 'herkese açık'})")

        await asyncio.to_thread(connect)
    except GithubException as ex:
        api_msg = (
            ex.data.get("message", str(ex)) if isinstance(getattr(ex, "data", None), dict) else str(ex)
        )
        hint = ""
        if getattr(ex, "status", None) == 401:
            hint = (
                " — Token geçersiz veya süresi dolmuş: `backend/.env` içindeki GITHUB_TOKEN’ı yenileyin "
                "(https://github.com/settings/tokens), ardından backend’i yeniden başlatın."
            )
        yield sse_pack({"type": "error", "message": f"Repoya erişilemedi: {api_msg}{hint}"})
        return
    except Exception as ex:
        yield sse_pack({"type": "error", "message": str(ex)})
        return

    for line in thread_logs:
        yield sse_pack({"type": "log", "message": line})
    thread_logs.clear()

    context: str | None = None
    readme: str | None = None

    try:

        def build_context():
            return collect_context_from_repo(g, owner, name, slog)

        context = await asyncio.to_thread(build_context)
    except Exception as ex:
        yield sse_pack({"type": "error", "message": f"Kaynak toplama hatası: {ex}"})
        return

    for line in thread_logs:
        yield sse_pack({"type": "log", "message": line})
    thread_logs.clear()

    try:

        def gen():
            return generate_readme_with_gemini(context or "", slog)

        readme = await asyncio.to_thread(gen)
    except Exception as ex:
        yield sse_pack({"type": "error", "message": f"Gemini hatası: {ex}"})
        return

    for line in thread_logs:
        yield sse_pack({"type": "log", "message": line})
    thread_logs.clear()

    yield sse_pack({"type": "readme", "content": readme})

    try:

        def pr_action():
            return create_docs_pr(g, owner, name, readme or "", slog)

        pr_url = await asyncio.to_thread(pr_action)
    except GithubException as ex:
        yield sse_pack({"type": "error", "message": f"GitHub PR hatası: {ex.data.get('message', str(ex))}"})
        return
    except Exception as ex:
        yield sse_pack({"type": "error", "message": f"PR oluşturma hatası: {ex}"})
        return

    for line in thread_logs:
        yield sse_pack({"type": "log", "message": line})
    thread_logs.clear()

    yield sse_pack({"type": "success", "pr_url": pr_url, "message": "Pull Request başarıyla açıldı."})


@app.post("/api/analyze")
async def analyze_repo(body: AnalyzeRequest):
    if not body.repo_url.strip():
        raise HTTPException(status_code=400, detail="repo_url boş olamaz.")

    async def event_stream() -> AsyncGenerator[str, None]:
        async for chunk in run_analyze_pipeline(body.repo_url.strip()):
            yield chunk

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _collect_user_repos_with_readme_flag(g: Github, login: str) -> dict:
    """Profil sahibinin GitHub API ile listelenen depoları; kök README varlığı tek tek doğrulanır."""
    user = g.get_user(login)
    _ = user.login
    repos: list[dict] = []
    for r in user.get_repos():
        readme_present = False
        try:
            r.get_readme()
            readme_present = True
        except GithubException:
            pass
        pushed = getattr(r, "pushed_at", None)
        repos.append(
            {
                "name": r.name,
                "full_name": r.full_name,
                "html_url": r.html_url,
                "description": (r.description or "").strip() or None,
                "private": bool(r.private),
                "fork": bool(r.fork),
                "archived": bool(getattr(r, "archived", False)),
                "default_branch": r.default_branch or "main",
                "language": getattr(r, "language", None),
                "readme_present": readme_present,
                "pushed_at": pushed.isoformat() if pushed else None,
            }
        )
    repos.sort(key=lambda row: row.get("pushed_at") or "", reverse=True)
    return {"login": user.login, "total": len(repos), "repos": repos}


@app.post("/api/profile/repos")
async def list_profile_repositories(body: ProfileReposRequest):
    """
    Kullanıcı profil URL'si verildiğinde o hesaba ait depoları listeler (README.md vb. kök readme varlığı dahil).
    Mevcut `/api/analyze` akışına dokunmaz; dönen html_url doğrudan repo analizinde kullanılabilir.
    """
    reload_local_env_into_globals()
    if not GITHUB_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_TOKEN tanımlı değil; profil depo listesi için backend .env içinde token gerekir.",
        )
    try:
        login = parse_github_user_profile_url(body.profile_url.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    g = Github(GITHUB_TOKEN)

    try:

        def run():
            return _collect_user_repos_with_readme_flag(g, login)

        data = await asyncio.to_thread(run)
    except GithubException as ex:
        api_msg = (
            ex.data.get("message", str(ex)) if isinstance(getattr(ex, "data", None), dict) else str(ex)
        )
        hint = ""
        if getattr(ex, "status", None) == 404:
            hint = " — Kullanıcı bulunamadı veya bu token ile görülemiyor."
        if getattr(ex, "status", None) == 401:
            hint = " — GITHUB_TOKEN geçersiz veya süresi dolmuş olabilir."
        raise HTTPException(status_code=400, detail=f"GitHub API: {api_msg}{hint}") from ex
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex)) from ex

    return data


@app.get("/")
def root_manifest():
    """Tarayıcıda kök adres — doğru uygulama mı hemen görülür."""
    return {
        "app": "derinnlp-docs-agent",
        "health": "/health",
        "meta": "/api/meta",
        "meta_alt": "/meta",
        "analyze": "/api/analyze",
        "profile_repos": "/api/profile/repos",
    }


@app.get("/meta")
@app.get("/api/meta")
def api_meta():
    """Bu adres doğru backend mi? Başka bir uygulama 8000'i kullanıyorsa burası 404 döner."""
    return {"app": "derinnlp-docs-agent", "title": app.title}


@app.get("/health")
def health():
    """Yerel tanı: doğru süreç çalışıyor mu ve anahtarlar yüklü mü (değer döndürülmez)."""
    load_dotenv(_BACKEND_ROOT / ".env", override=True)
    gh = os.getenv("GITHUB_TOKEN", "").strip()
    gm = os.getenv("GEMINI_API_KEY", "").strip()
    return {
        "status": "ok",
        "service": "derinnlp-docs-agent",
        "github_token_configured": bool(gh),
        "gemini_key_configured": bool(gm),
    }
