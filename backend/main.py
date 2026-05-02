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
    "Sen uzman bir yazılım mimarı ve teknik yazarsın. Sana bir projenin tam klasör yapısını ve "
    "kaynak kodlarını veriyorum. Lütfen bu kodları derinlemesine analiz et ve GitHub projesi için "
    "son derece profesyonel, kapsamlı bir README.md dosyası oluştur. Bu dosya; Proje Özeti, "
    "Kullanılan Teknolojiler, Klasör Yapısı, Kurulum ve Çalıştırma Adımlarını içermelidir. "
    "Kesinlikle sadece Markdown formatında çıktı ver, ekstra sohbet metni ekleme."
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
        log("[2/5] Uyarı: Çok büyük repo; bağlam güvenli karakter limitine göre kısaltıldı.")

    log(f"[2/5] {len(blobs)} kaynak dosya birleştirildi (~{total_chars} karakter).")

    structure_lines = sorted({p for p, _ in blobs})
    structure_block = "\n".join(structure_lines[:5000])
    if len(structure_lines) > 5000:
        structure_block += "\n… (liste kesildi)"

    parts: list[str] = [
        "Aşağıda proje dosya listesi ve kaynak kod içerikleri birleştirilmiştir.\n\n",
        "## Dosya listesi\n",
        structure_block,
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


@app.get("/")
def root_manifest():
    """Tarayıcıda kök adres — doğru uygulama mı hemen görülür."""
    return {
        "app": "derinnlp-docs-agent",
        "health": "/health",
        "meta": "/api/meta",
        "meta_alt": "/meta",
        "analyze": "/api/analyze",
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
