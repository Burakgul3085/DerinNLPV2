"""
Otonom GitHub Dokümantasyon Ajanı — tool-calling agent.

Ajan döngüsü:
- google-genai SDK (function calling) üzerinden çalışır.
- Her turda model YA bir araç çağırır YA da kısa bir düşünme metni döndürür.
- Sırayı, hangi dosyaları okuyacağını, hangi bölümleri yazacağını AJAN seçer.
- Bütçe (tur, dosya okuma, dosya boyutu) ve güvenlik (.env yasakları, sonsuz
  döngü engeli) yalnızca "kalkış pisti çiti" olarak vardır; iş akışı kuralı yok.

Mevcut /api/analyze (klasik pipeline) akışına bu modül DOKUNMAZ; sadece yeni
endpoint /api/agent-analyze tarafından kullanılır.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from github import Github, GithubException
from github.Repository import Repository

from google import genai
from google.genai import types as genai_types


# --- Bütçe ve güvenlik çitleri --------------------------------------------------
MAX_TURNS = 20
MAX_FILE_READS = 50
MAX_FILE_BYTES = 256 * 1024
MAX_SEARCH_HITS = 30
AGENT_MODEL_FALLBACKS: Tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash",
)


AGENT_SYSTEM_PROMPT = (
    "Sen otonom bir GitHub dokümantasyon ajanısın.\n"
    "HEDEF: Verilen depo için Türkçe, profesyonel bir README üretmek ve hazır olduğunda "
    "'finish' aracını çağırmak.\n\n"
    "KARAR SENDE: Hangi dosyayı okuyacağına, hangi sırayla ilerleyeceğine, hangi bölümleri "
    "yazacağına ve nasıl yapılandıracağına sen karar verirsin. Sabit bir pipeline yoktur; "
    "araçları sen kullanırsın.\n\n"
    "GÜVENLİK VE KALİTE KURALLARI (iş akışı kuralı değil, sınır çiti):\n"
    "- Tüm çıktı Türkçe; İngilizce başlık veya cümle karıştırma.\n"
    "- Bağlamda görmediğin komut, sürüm veya URL UYDURMA. Bilgi yoksa kısaca 'doğrulanmalı' de.\n"
    "- .env, API anahtarı, token, parola değerlerini yazma; yalnızca yer tutucu (örn. YOUR_API_KEY).\n"
    "- Mermaid kullanırsan flowchart TB; düğüm kimlikleri yalnız harf/rakam/alt çizgi; "
    "HTML, <style>, %%{init}%% veya classDef yok; en fazla 18 düğüm ve 24 ok.\n"
    "- Aynı dosyayı tekrar tekrar okumaya çalışma; bir kez yeterli (önbelleğe alınır).\n\n"
    "ARAÇ KULLANIMI:\n"
    "- Önce keşfet (repo_tree, manifest_summary, file_read, search_repo).\n"
    "- Sonra README bölümlerini section_write ile biriktir; gerekirse section_revise ile düzelt.\n"
    "- Bölüm isimleri sana bağlı (örn. 'Özet', 'Özellikler', 'Gereksinimler', 'Kurulum', "
    "'Yapılandırma', 'Teknolojiler', 'Mimari', 'API', 'Test', 'Dağıtım', 'Katkı', 'Lisans'). "
    "İhtiyaca göre yeni bölüm ekle ya da gereksizini atla.\n"
    "- Hazır olduğunda 'finish' çağır. 'finish' çağırılmadan akış kapanmaz; ondan sonra ajan "
    "kodu nihai README'yi birleştirip GitHub'a PR olarak gönderir.\n\n"
    "BİÇİM:\n"
    "- Her turda YA bir tool çağrısı YA da en fazla 1-2 cümlelik kısa düşünme metni döndür.\n"
    "- Nihai README metnini sadece section_write/section_revise ile biriktir; serbest metin "
    "olarak büyük README dökme.\n"
)


# --- Bütçe ve durum -----------------------------------------------------------


@dataclass
class AgentBudget:
    turn: int = 0
    file_reads: int = 0
    max_turns: int = MAX_TURNS
    max_file_reads: int = MAX_FILE_READS

    def consume_turn(self) -> None:
        self.turn += 1

    def consume_read(self) -> None:
        self.file_reads += 1


@dataclass
class AgentState:
    owner: str
    repo_name: str
    repo: Repository
    default_branch: str
    extra_instruction: Optional[str] = None
    sections: Dict[str, str] = field(default_factory=dict)
    section_order: List[str] = field(default_factory=list)
    files_read: Dict[str, str] = field(default_factory=dict)
    tree_cache: Optional[List[str]] = None
    manifest_cache: Optional[str] = None
    finished: bool = False
    finish_summary: Optional[str] = None
    budget: AgentBudget = field(default_factory=AgentBudget)


# --- Araç şemaları (function declarations) ------------------------------------


def _schema_object(properties: Dict[str, genai_types.Schema], required: List[str]) -> genai_types.Schema:
    return genai_types.Schema(
        type=genai_types.Type.OBJECT,
        properties=properties,
        required=required,
    )


def _string(desc: str) -> genai_types.Schema:
    return genai_types.Schema(type=genai_types.Type.STRING, description=desc)


def _array_of_strings(desc: str) -> genai_types.Schema:
    return genai_types.Schema(
        type=genai_types.Type.ARRAY,
        items=genai_types.Schema(type=genai_types.Type.STRING),
        description=desc,
    )


def build_tool() -> genai_types.Tool:
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="repo_tree",
                description=(
                    "GitHub deposundaki güvenli dosya/klasör yollarının düz listesini döner. "
                    "Çok büyük depolarda baştan kısaltılabilir."
                ),
                parameters=_schema_object({}, []),
            ),
            genai_types.FunctionDeclaration(
                name="file_read",
                description=(
                    "Verilen yoldaki tek dosyanın metin içeriğini döner. Büyük dosyalar baştan "
                    "kısaltılır. Aynı dosya tekrar istenirse önbellekten gelir."
                ),
                parameters=_schema_object(
                    {"path": _string("Repo köküne göre dosya yolu, ör: composer.json")},
                    ["path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="manifest_summary",
                description=(
                    "composer.json, package.json, pyproject.toml, Dockerfile, docker-compose, "
                    "requirements.txt gibi yapılandırma dosyalarının kısa özetini Markdown olarak "
                    "döner."
                ),
                parameters=_schema_object({}, []),
            ),
            genai_types.FunctionDeclaration(
                name="search_repo",
                description=(
                    "Repo içindeki yolları sorguya göre filtreler (path eşleşmesi). En fazla "
                    "30 sonuç döner; içerik döndürmez (içerik için file_read kullan)."
                ),
                parameters=_schema_object(
                    {"query": _string("Yol veya dosya adı parçası, ör: route, controller, .env")},
                    ["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="section_write",
                description=(
                    "README'ye yeni bir bölüm ekler (aynı isim varsa üzerine yazar). Markdown "
                    "başlığı yazma; ajan kodu bölüm adından başlık üretir."
                ),
                parameters=_schema_object(
                    {
                        "name": _string("Bölüm adı, ör: Özet, Kurulum"),
                        "markdown": _string("Bölümün Markdown içeriği (başlık satırı olmadan)."),
                    },
                    ["name", "markdown"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="section_revise",
                description="Var olan bir bölümü yeni içerikle değiştirir.",
                parameters=_schema_object(
                    {
                        "name": _string("Bölüm adı"),
                        "markdown": _string("Yeni Markdown içerik (başlık satırı olmadan)."),
                    },
                    ["name", "markdown"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="finish",
                description=(
                    "README'nin hazır olduğunu bildirir. Ardından ajan kodu bölümleri "
                    "birleştirir ve GitHub'da PR açar."
                ),
                parameters=_schema_object(
                    {
                        "summary": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="Kısa özet veya bitiş notu (opsiyonel).",
                        ),
                    },
                    [],
                ),
            ),
        ]
    )


# --- Araç gövdeleri ------------------------------------------------------------

_AGENT_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cs",
        ".html",
        ".css",
        ".scss",
        ".json",
        ".md",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".rb",
        ".go",
        ".rs",
        ".php",
        ".blade.php",
        ".sql",
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".cfg",
        ".sh",
        ".ps1",
        ".dart",
        ".kt",
        ".swift",
        ".vue",
        ".svelte",
        ".tsx",
    }
)


def _is_secret_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if base == ".env" or base.startswith(".env."):
        return True
    if base in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    if base.endswith(".pem") or base.endswith(".key"):
        return True
    return False


def _safe_short(text: str, limit: int = 200) -> str:
    t = text.replace("\n", " ⏎ ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _build_tree(state: AgentState) -> List[str]:
    if state.tree_cache is not None:
        return state.tree_cache
    repo = state.repo
    branch_ref = repo.get_branch(state.default_branch)
    tree = repo.get_git_tree(branch_ref.commit.sha, recursive=True)
    paths: List[str] = []
    skip_dirs = (
        "node_modules/",
        ".git/",
        ".venv/",
        "venv/",
        "dist/",
        "build/",
        "__pycache__/",
        ".idea/",
        ".vscode/",
    )
    for item in tree.tree:
        if item.type != "blob":
            continue
        p = item.path
        low = p.replace("\\", "/").lower()
        if any(seg in low for seg in skip_dirs):
            continue
        if _is_secret_path(p):
            continue
        paths.append(p)
    paths.sort()
    state.tree_cache = paths
    return paths


def tool_repo_tree(state: AgentState) -> Dict[str, Any]:
    paths = _build_tree(state)
    # Çok büyük repolarda ilk 800 yolla yetin
    capped = paths[:800]
    return {
        "total": len(paths),
        "returned": len(capped),
        "paths": capped,
        "note": "Liste 800 yol ile sınırlandı." if len(paths) > 800 else "Tam liste.",
    }


def tool_file_read(state: AgentState, path: str) -> Dict[str, Any]:
    if not path or not isinstance(path, str):
        return {"error": "path zorunlu."}
    norm = path.replace("\\", "/").strip().lstrip("/")
    if _is_secret_path(norm):
        return {"error": "Bu dosya gizli olduğu için okunamaz (güvenlik çiti).", "path": norm}
    if norm in state.files_read:
        return {
            "path": norm,
            "cached": True,
            "content": state.files_read[norm],
        }
    if state.budget.file_reads >= state.budget.max_file_reads:
        return {"error": "Dosya okuma bütçesi doldu.", "path": norm}
    try:
        cf = state.repo.get_contents(norm, ref=state.default_branch)
    except GithubException as ex:
        return {"error": f"GitHub: {getattr(ex, 'data', {}).get('message', str(ex))}", "path": norm}
    except Exception as ex:
        return {"error": f"Okuma hatası: {ex}", "path": norm}
    if isinstance(cf, list):
        return {"error": "Bu yol bir klasör.", "path": norm}
    if getattr(cf, "type", "") == "dir":
        return {"error": "Bu yol bir klasör.", "path": norm}
    size = getattr(cf, "size", 0) or 0
    raw_b64 = getattr(cf, "content", None)
    if not raw_b64:
        return {"error": "İçerik döndürülemedi (boş veya çok büyük).", "path": norm, "size": size}
    try:
        decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
    except Exception as ex:
        return {"error": f"Decode hatası: {ex}", "path": norm}
    truncated = False
    if len(decoded.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        # Karakter bazlı kısaltma yeterli
        decoded = decoded[: MAX_FILE_BYTES] + "\n… [dosya güvenlik çitine göre kısaltıldı]"
        truncated = True
    state.budget.consume_read()
    state.files_read[norm] = decoded
    return {
        "path": norm,
        "size": size,
        "truncated": truncated,
        "content": decoded,
    }


_MANIFEST_PATTERNS = (
    "composer.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
    "Procfile",
    ".env.example",
    "go.mod",
    "Cargo.toml",
    "pubspec.yaml",
)


def tool_manifest_summary(state: AgentState) -> Dict[str, Any]:
    if state.manifest_cache is not None:
        return {"cached": True, "markdown": state.manifest_cache}
    paths = _build_tree(state)
    found: List[Tuple[str, str]] = []
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base in _MANIFEST_PATTERNS:
            read = tool_file_read(state, p)
            if "content" in read:
                snippet = read["content"]
                if len(snippet) > 6000:
                    snippet = snippet[:6000] + "\n… [özet için kısaltıldı]"
                found.append((p, snippet))
    if not found:
        text = "Bilinen manifest dosyası bulunamadı."
    else:
        lines = ["# Manifest özetleri"]
        for path, body in found:
            lines.append(f"\n## `{path}`\n")
            lines.append("```\n" + body + "\n```")
        text = "\n".join(lines)
    state.manifest_cache = text
    return {"cached": False, "markdown": text}


def tool_search_repo(state: AgentState, query: str) -> Dict[str, Any]:
    if not query or not isinstance(query, str):
        return {"error": "query zorunlu."}
    q = query.strip().lower()
    if not q:
        return {"error": "query boş."}
    paths = _build_tree(state)
    hits = [p for p in paths if q in p.lower()]
    return {
        "query": query,
        "total_hits": len(hits),
        "returned": min(len(hits), MAX_SEARCH_HITS),
        "paths": hits[:MAX_SEARCH_HITS],
    }


def tool_section_write(state: AgentState, name: str, markdown: str) -> Dict[str, Any]:
    if not name or not isinstance(name, str):
        return {"error": "name zorunlu."}
    if not isinstance(markdown, str):
        return {"error": "markdown string olmalı."}
    key = name.strip()
    if not key:
        return {"error": "name boş olamaz."}
    if key not in state.sections:
        state.section_order.append(key)
    state.sections[key] = markdown.strip()
    return {"ok": True, "name": key, "sections": state.section_order}


def tool_section_revise(state: AgentState, name: str, markdown: str) -> Dict[str, Any]:
    if name not in state.sections:
        return {"error": f"'{name}' adlı bölüm yok; önce section_write kullan."}
    state.sections[name] = (markdown or "").strip()
    return {"ok": True, "name": name}


def tool_finish(state: AgentState, summary: Optional[str] = None) -> Dict[str, Any]:
    state.finished = True
    state.finish_summary = (summary or "").strip() or None
    return {"ok": True, "sections": state.section_order}


def execute_tool(state: AgentState, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "repo_tree":
            return tool_repo_tree(state)
        if name == "file_read":
            return tool_file_read(state, str(args.get("path", "")))
        if name == "manifest_summary":
            return tool_manifest_summary(state)
        if name == "search_repo":
            return tool_search_repo(state, str(args.get("query", "")))
        if name == "section_write":
            return tool_section_write(state, str(args.get("name", "")), str(args.get("markdown", "")))
        if name == "section_revise":
            return tool_section_revise(state, str(args.get("name", "")), str(args.get("markdown", "")))
        if name == "finish":
            return tool_finish(state, args.get("summary"))
    except Exception as ex:
        return {"error": f"Araç çalıştırma hatası: {ex}"}
    return {"error": f"Bilinmeyen araç: {name}"}


# --- Birleştirme ve PR --------------------------------------------------------


def assemble_readme(state: AgentState) -> str:
    parts: List[str] = []
    seen: set = set()
    for name in state.section_order:
        if name in seen:
            continue
        seen.add(name)
        body = state.sections.get(name, "").strip()
        if not body:
            continue
        parts.append(f"## {name}\n\n{body}")
    return "\n\n".join(parts).strip() + "\n"


# --- Ajan çalışma döngüsü -----------------------------------------------------


def _model_candidates(preferred: str) -> List[str]:
    out: List[str] = []
    if preferred:
        out.append(preferred)
    for m in AGENT_MODEL_FALLBACKS:
        if m not in out:
            out.append(m)
    return out


def _looks_like_model_missing_error(err: BaseException) -> bool:
    msg = str(err).lower()
    return "404" in str(err) or "not found" in msg or "is not supported" in msg


def _short_args_repr(args: Dict[str, Any]) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return _safe_short(s, 160)


def _format_tool_call_summary(name: str, args: Dict[str, Any]) -> str:
    """Konsola yazılan tool çağrılarını uzun ham JSON yerine kısa Türkçe özet olarak basar."""
    if name == "repo_tree":
        return "repo_tree"
    if name == "manifest_summary":
        return "manifest_summary"
    if name == "file_read":
        path = str(args.get("path", "")).strip() or "?"
        return f"file_read ← {path}"
    if name == "search_repo":
        query = str(args.get("query", "")).strip()
        return f"search_repo ← \"{_safe_short(query, 60)}\""
    if name == "section_write":
        section = str(args.get("name", "")).strip() or "(adsız)"
        md = args.get("markdown", "")
        length = len(md) if isinstance(md, str) else 0
        return f"section_write → \"{section}\" (~{length} karakter)"
    if name == "section_revise":
        section = str(args.get("name", "")).strip() or "(adsız)"
        md = args.get("markdown", "")
        length = len(md) if isinstance(md, str) else 0
        return f"section_revise → \"{section}\" (~{length} karakter)"
    if name == "finish":
        return "finish"
    return f"{name}({_short_args_repr(args)})"


def _format_tool_result_summary(name: str, result: Any) -> str:
    """Tool sonucu için kısa, okunaklı bir özet üretir. Ham JSON dökmez."""
    if not isinstance(result, dict):
        return _safe_short(str(result), 180)
    if "error" in result and result["error"]:
        return f"hata: {_safe_short(str(result['error']), 160)}"
    if name == "repo_tree":
        total = result.get("total", "?")
        returned = result.get("returned", "?")
        note = result.get("note", "")
        suffix = f" · {note}" if note else ""
        return f"yol sayısı: {total} (gönderilen: {returned}){suffix}"
    if name == "manifest_summary":
        md = result.get("markdown", "") if isinstance(result.get("markdown", ""), str) else ""
        cached = " (önbellek)" if result.get("cached") else ""
        return f"manifest özeti hazır{cached} (~{len(md)} karakter)"
    if name == "file_read":
        path = result.get("path", "?")
        size = result.get("size", "?")
        truncated = " · kısaltıldı" if result.get("truncated") else ""
        cached = " · önbellek" if result.get("cached") else ""
        return f"dosya okundu: {path} ({size} B){truncated}{cached}"
    if name == "search_repo":
        total = result.get("total_hits", "?")
        returned = result.get("returned", "?")
        return f"arama bitti: {total} eşleşme (gönderilen: {returned})"
    if name == "section_write" or name == "section_revise":
        section_name = result.get("name", "?")
        sections = result.get("sections")
        count = len(sections) if isinstance(sections, list) else None
        if count is not None:
            return f"bölüm güncellendi: \"{section_name}\" · toplam bölüm: {count}"
        return f"bölüm güncellendi: \"{section_name}\""
    if name == "finish":
        sections = result.get("sections")
        if isinstance(sections, list):
            return f"ajan bitti · toplam bölüm: {len(sections)}"
        return "ajan bitti"
    # bilinmeyen araç için makul fallback
    safe = {k: v for k, v in result.items() if k not in {"content", "markdown", "paths"}}
    try:
        return _safe_short(json.dumps(safe, ensure_ascii=False), 180)
    except Exception:
        return _safe_short(str(safe), 180)


async def run_agent_pipeline(
    *,
    repo_url: str,
    api_key: str,
    github_token: str,
    preferred_model: str,
    ek_talimat: Optional[str],
    parse_repo: Callable[[str], Tuple[str, str]],
    pr_factory: Callable[[Github, str, str, str, Callable[[str], None]], str],
    mermaid_sanitizer: Callable[[str], str],
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Asenkron üretici. Her `yield` bir SSE olayı sözlüğüdür:
      {"type": "log", "message": "..."} | {"type": "readme", "content": "..."} |
      {"type": "success", "message": "...", "pr_url": "..."} | {"type": "error", "message": "..."}.
    """

    if not github_token or not api_key:
        yield {
            "type": "error",
            "message": "Sunucu yapılandırması eksik: GITHUB_TOKEN ve GEMINI_API_KEY .env içinde tanımlı olmalı.",
        }
        return

    try:
        owner, name = parse_repo(repo_url)
    except ValueError as ex:
        yield {"type": "error", "message": str(ex)}
        return

    g = Github(github_token)
    try:
        repo = await asyncio.to_thread(g.get_repo, f"{owner}/{name}")
        _ = repo.full_name
    except GithubException as ex:
        api_msg = (
            ex.data.get("message", str(ex)) if isinstance(getattr(ex, "data", None), dict) else str(ex)
        )
        yield {"type": "error", "message": f"Repoya erişilemedi: {api_msg}"}
        return
    except Exception as ex:
        yield {"type": "error", "message": str(ex)}
        return

    yield {"type": "log", "message": f"[ajan/1] Hedef repo: {repo.full_name}; ajan başlatılıyor…"}

    state = AgentState(
        owner=owner,
        repo_name=name,
        repo=repo,
        default_branch=repo.default_branch,
        extra_instruction=(ek_talimat or "").strip() or None,
    )

    client = genai.Client(api_key=api_key)
    tool = build_tool()
    config = genai_types.GenerateContentConfig(
        tools=[tool],
        system_instruction=AGENT_SYSTEM_PROMPT,
    )

    user_brief_parts = [
        f"Hedef: GitHub deposu '{owner}/{name}' için Türkçe profesyonel bir README üret.",
        "Araçları kullanarak repoyu keşfet, bölümleri biriktir; hazırsa 'finish' çağır.",
    ]
    if state.extra_instruction:
        user_brief_parts.append(
            "Kullanıcının ek istekleri (öncelikli):\n" + state.extra_instruction
        )
    contents: List[genai_types.Content] = [
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text="\n\n".join(user_brief_parts))],
        )
    ]

    candidates = _model_candidates(preferred_model)
    chosen_model: Optional[str] = None
    last_err: Optional[BaseException] = None

    while not state.finished and state.budget.turn < state.budget.max_turns:
        state.budget.consume_turn()
        yield {
            "type": "log",
            "message": (
                f"[ajan] Tur {state.budget.turn}/{state.budget.max_turns} · "
                f"bölüm {len(state.sections)} · dosya {state.budget.file_reads}/{state.budget.max_file_reads}"
            ),
        }

        response = None
        for model_name in candidates if chosen_model is None else [chosen_model]:
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                chosen_model = model_name
                break
            except Exception as ex:  # noqa: BLE001
                last_err = ex
                if _looks_like_model_missing_error(ex):
                    yield {
                        "type": "log",
                        "message": f"[ajan] Model kullanılamadı ({model_name}); sıradaki deneniyor…",
                    }
                    continue
                yield {"type": "error", "message": f"Gemini hatası: {ex}"}
                return
        if response is None:
            yield {
                "type": "error",
                "message": f"Uygun Gemini modeli bulunamadı: {last_err}",
            }
            return

        if not response.candidates:
            yield {"type": "log", "message": "[ajan] Model boş cevap döndü; tur bitiriliyor."}
            break

        candidate = response.candidates[0]
        if candidate.content is None:
            yield {"type": "log", "message": "[ajan] Boş içerik geldi; tur bitiriliyor."}
            break

        parts = candidate.content.parts or []
        contents.append(candidate.content)

        function_call = None
        thought_texts: List[str] = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                function_call = fc
                break
            text_val = getattr(part, "text", None)
            if text_val:
                thought_texts.append(text_val)

        if thought_texts:
            joined = " ".join(t.strip() for t in thought_texts if t).strip()
            if joined:
                yield {"type": "log", "message": f"[ajan/düşünce] {_safe_short(joined, 180)}"}

        if function_call is None:
            yield {
                "type": "log",
                "message": "[ajan] Bu turda araç çağrısı yok; ajana bir tool çağırması hatırlatılıyor.",
            }
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_text(
                            text=(
                                "Lütfen sonraki turda mutlaka bir araç çağır (repo_tree, file_read, "
                                "manifest_summary, search_repo, section_write, section_revise) veya "
                                "hazırsan 'finish' çağır."
                            )
                        )
                    ],
                )
            )
            continue

        args_raw = function_call.args or {}
        try:
            args_dict = dict(args_raw)
        except Exception:
            args_dict = {}

        yield {
            "type": "log",
            "message": f"[ajan/araç] {_format_tool_call_summary(function_call.name, args_dict)}",
        }

        result = await asyncio.to_thread(execute_tool, state, function_call.name, args_dict)

        # Sonucu kısaltarak logla, modele tam JSON ver (ama makul boyutta)
        result_for_model = result
        try:
            payload_json = json.dumps(result, ensure_ascii=False)
            if len(payload_json) > 60_000:
                # Aşırı uzun ise content alanını kırp
                if isinstance(result, dict) and "content" in result and isinstance(result["content"], str):
                    trimmed = dict(result)
                    trimmed["content"] = result["content"][:55_000] + "\n… [model bağlamı için kısaltıldı]"
                    result_for_model = trimmed
        except Exception:
            result_for_model = {"error": "Sonuç serileştirilemedi."}

        yield {
            "type": "log",
            "message": f"[ajan/sonuç] {_format_tool_result_summary(function_call.name, result)}",
        }

        contents.append(
            genai_types.Content(
                role="tool",
                parts=[
                    genai_types.Part.from_function_response(
                        name=function_call.name,
                        response={"result": result_for_model},
                    )
                ],
            )
        )

        if function_call.name == "finish":
            # Sonraki tura geçme; while koşulu state.finished sayesinde kapanır.
            continue

    if not state.finished:
        yield {
            "type": "log",
            "message": (
                "[ajan] Bütçe doldu, 'finish' çağrısı gelmeden döngü kapatıldı. "
                "Mevcut bölümlerle README birleştiriliyor."
            ),
        }

    if not state.sections:
        yield {
            "type": "error",
            "message": "Ajan herhangi bir README bölümü üretmedi; PR açılmadı.",
        }
        return

    final = assemble_readme(state)
    try:
        final = mermaid_sanitizer(final)
    except Exception:
        pass

    yield {"type": "readme", "content": final}

    yield {"type": "log", "message": "[ajan] README birleştirildi; GitHub PR akışı başlatılıyor…"}

    pr_logs: List[str] = []

    def pr_log(msg: str) -> None:
        pr_logs.append(msg)

    try:
        pr_url = await asyncio.to_thread(pr_factory, g, owner, name, final, pr_log)
    except GithubException as ex:
        for line in pr_logs:
            yield {"type": "log", "message": line}
        msg = ex.data.get("message", str(ex)) if isinstance(getattr(ex, "data", None), dict) else str(ex)
        yield {"type": "error", "message": f"GitHub PR hatası: {msg}"}
        return
    except Exception as ex:
        for line in pr_logs:
            yield {"type": "log", "message": line}
        yield {"type": "error", "message": f"PR oluşturma hatası: {ex}"}
        return

    for line in pr_logs:
        yield {"type": "log", "message": line}

    yield {
        "type": "success",
        "pr_url": pr_url,
        "message": "Otonom ajan akışı tamamlandı; Pull Request açıldı.",
    }
