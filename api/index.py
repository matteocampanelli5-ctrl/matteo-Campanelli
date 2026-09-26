"""API FastAPI pour classer et stocker des transcriptions vocales (Supabase + Vercel)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pathlib import Path

import anthropic
import dateparser
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pywebpush import WebPushException, webpush
from pydantic import BaseModel, Field
from supabase import Client, create_client


load_dotenv()

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
LOGGER = logging.getLogger("matnot.reminders")
CRON_SECRET = os.getenv("CRON_SECRET", "")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL et SUPABASE_KEY doivent être définies.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

Category = Literal["note", "liste", "rendez-vous"]
ListAction = Literal["add", "delete"]

FRENCH_WEEKDAYS = (
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
)
FRENCH_MONTHS = (
    "janvier", "février", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "août", "aout", "septembre", "octobre", "novembre",
    "décembre", "decembre",
)

app = FastAPI(
    title="API de transcription et de classement",
    description="Classe les transcriptions en notes, listes ou rendez-vous.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "index.html", media_type="text/html")


@app.get("/service-worker.js", include_in_schema=False)
def serve_service_worker() -> FileResponse:
    return FileResponse(
        PUBLIC_DIR / "service-worker.js", media_type="application/javascript"
    )


# --------------------------------------------------------------------------
# Modèles Pydantic
# --------------------------------------------------------------------------

class TranscriptionRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Texte transcrit")


class ChecklistItem(BaseModel):
    text: str = Field(..., min_length=1)
    completed: bool = False


class ChecklistItemUpdate(BaseModel):
    completed: bool


class ItemCreate(BaseModel):
    category: Category
    title: str | None = None
    content: str | list[ChecklistItem] = Field(..., min_length=1)
    date: str | None = None
    heure: str | None = None


class ItemUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1)
    date: str | None = None
    heure: str | None = None
    action: ListAction | None = None
    text: str | None = Field(default=None, min_length=1)
    index: int | None = Field(default=None, ge=0)
    items: list[str] | None = None
    title: str | None = None


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(..., min_length=1)
    auth: str = Field(..., min_length=1)


class PushSubscription(BaseModel):
    endpoint: str = Field(..., min_length=1)
    keys: PushSubscriptionKeys


class SubscriptionEndpoint(BaseModel):
    endpoint: str = Field(..., min_length=1)


class Item(ItemCreate):
    id: str
    created_at: str
    reminder_at: str | None = None
    notified: bool = False


# --------------------------------------------------------------------------
# Accès aux données (Supabase)
# --------------------------------------------------------------------------

def _fetch_items() -> list[dict]:
    response = supabase.table("items").select("*").order("created_at").execute()
    return response.data or []


def _fetch_item(item_id: str) -> dict | None:
    response = supabase.table("items").select("*").eq("id", item_id).limit(1).execute()
    data = response.data or []
    return data[0] if data else None


def _insert_item_row(row: dict) -> dict:
    response = supabase.table("items").insert(row).execute()
    return response.data[0]


def _update_item_row(item: dict) -> dict:
    item_id = item["id"]
    payload = {k: v for k, v in item.items() if k != "id"}
    response = supabase.table("items").update(payload).eq("id", item_id).execute()
    return response.data[0]


def _delete_item_row(item_id: str) -> bool:
    response = supabase.table("items").delete().eq("id", item_id).execute()
    return bool(response.data)


def _fetch_subscriptions() -> list[dict]:
    response = supabase.table("subscriptions").select("*").execute()
    return response.data or []


def _upsert_subscription(subscription: PushSubscription) -> None:
    supabase.table("subscriptions").upsert(
        {
            "endpoint": subscription.endpoint,
            "p256dh": subscription.keys.p256dh,
            "auth": subscription.keys.auth,
        }
    ).execute()


def _delete_subscription(endpoint: str) -> None:
    supabase.table("subscriptions").delete().eq("endpoint", endpoint).execute()


def _delete_subscriptions(endpoints: set[str]) -> None:
    if not endpoints:
        return
    supabase.table("subscriptions").delete().in_("endpoint", list(endpoints)).execute()


def _to_subscription_info(row: dict) -> dict:
    return {
        "endpoint": row["endpoint"],
        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
    }


# --------------------------------------------------------------------------
# Logique métier (inchangée par rapport à la version Replit)
# --------------------------------------------------------------------------

def _split_list_content(content: str) -> list[dict[str, str | bool]]:
    source = content.strip()
    if ":" in source:
        prefix, suffix = source.split(":", 1)
        if prefix.strip() and suffix.strip():
            source = suffix.strip()

    parts = re.split(r"(?:\r?\n|[;,])", source)
    cleaned_parts = []
    for part in parts:
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", part).strip()
        if cleaned:
            cleaned_parts.append(cleaned)
    if not cleaned_parts:
        cleaned_parts = [content.strip()]
    return [{"text": part, "completed": False} for part in cleaned_parts]


def _extract_appointment_details(text: str) -> tuple[str, str]:
    date_value = ""
    heure_value = ""
    day_pattern = "|".join(re.escape(day) for day in FRENCH_WEEKDAYS)
    month_pattern = "|".join(re.escape(month) for month in FRENCH_MONTHS)

    date_pattern = (
        rf"\b(?:"
        rf"(?:aujourd'hui|demain|après-demain|{day_pattern})"
        rf"(?:\s+\d{{1,2}}(?:er)?(?:\s+(?:{month_pattern}))?"
        rf"(?:\s+\d{{4}})?)?"
        rf"|"
        rf"\d{{1,2}}(?:[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?"
        rf"|\s+(?:{month_pattern})(?:\s+\d{{4}})?)"
        rf")\b"
    )
    date_match = re.search(date_pattern, text, flags=re.IGNORECASE)
    if date_match:
        date_value = date_match.group(0).strip(" ,.;")

    time_match = re.search(
        r"\b(?:[01]?\d|2[0-3])"
        r"(?:\s*(?:h(?:[0-5]\d)?|:[0-5]\d|heures?))\b",
        text,
        flags=re.IGNORECASE,
    )
    if time_match:
        heure_value = time_match.group(0).strip(" ,.;")

    return date_value, heure_value


def _resolve_reminder_datetime(
    date_text: str | None,
    heure_text: str | None,
    created_at: str | None,
) -> datetime | None:
    if not isinstance(date_text, str) or not date_text.strip():
        return None
    if not isinstance(heure_text, str) or not heure_text.strip():
        return None
    if not isinstance(created_at, str) or not created_at.strip():
        return None

    try:
        relative_base = datetime.fromisoformat(
            created_at.strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None

    if relative_base.tzinfo is None:
        relative_base = relative_base.replace(tzinfo=timezone.utc)
    else:
        relative_base = relative_base.astimezone(timezone.utc)

    normalised_hour = heure_text.strip()
    hour_match = re.fullmatch(
        r"(\d{1,2})\s*h\s*(\d{1,2})?", normalised_hour, flags=re.IGNORECASE
    )
    if hour_match:
        normalised_hour = (
            f"{int(hour_match.group(1)):02d}:{int(hour_match.group(2) or 0):02d}"
        )

    combined_text = f"{date_text.strip()} {normalised_hour}"
    try:
        resolved = dateparser.parse(
            combined_text,
            languages=["fr"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": relative_base,
            },
        )
    except (TypeError, ValueError, OverflowError):
        return None

    if resolved is None:
        return None
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _normalise_list_content(content: object) -> list[dict[str, str | bool]]:
    if isinstance(content, str):
        return _split_list_content(content)

    if not isinstance(content, list):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Le contenu d'une liste doit être un texte ou une liste d'éléments.",
        )

    normalised_items = []
    for entry in content:
        if isinstance(entry, ChecklistItem):
            entry_text = entry.text
            completed = entry.completed
        elif isinstance(entry, str):
            entry_text = entry.strip()
            completed = False
        elif isinstance(entry, dict):
            entry_text = entry.get("text", entry.get("content"))
            completed = entry.get("completed", False)
        else:
            entry_text = None
            completed = False

        if isinstance(entry_text, str) and entry_text.strip():
            normalised_items.append(
                {
                    "text": entry_text.strip(),
                    "completed": completed if isinstance(completed, bool) else False,
                }
            )
    return normalised_items


def _insert_item(
    category: Category,
    content: object,
    date: str | None = None,
    heure: str | None = None,
    title: str | None = None,
) -> dict:
    row: dict = {
        "category": category,
        "content": (
            _normalise_list_content(content) if category == "liste" else content
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if title and title.strip():
        row["title"] = title.strip()
    if category == "rendez-vous":
        extracted_date, extracted_heure = _extract_appointment_details(str(content))
        row["date"] = date.strip() if date and date.strip() else extracted_date
        row["heure"] = heure.strip() if heure and heure.strip() else extracted_heure
        resolved = _resolve_reminder_datetime(
            row["date"], row["heure"], row["created_at"]
        )
        row["reminder_at"] = resolved.isoformat() if resolved else None
        row["notified"] = False

    return _insert_item_row(row)


def _summarise_existing_items(items: list[dict]) -> str:
    recent_items = items[-30:]
    if not recent_items:
        return "(aucun élément existant)"

    summary_lines = []
    for item in reversed(recent_items):
        content = item.get("content", "")
        if isinstance(content, list):
            preview = " | ".join(
                str(entry.get("text", ""))
                for entry in content[:5]
                if isinstance(entry, dict) and entry.get("text")
            )
        else:
            preview = str(content).strip()
        preview = preview[:180] or "(vide)"

        fields = [f"id={item.get('id', '')}", f"category={item.get('category', '')}"]
        if item.get("title"):
            fields.append(f"title={str(item['title'])[:80]}")
        fields.append(f"aperçu={preview}")
        if item.get("date"):
            fields.append(f"date={item['date']}")
        if item.get("heure"):
            fields.append(f"heure={item['heure']}")
        summary_lines.append("- " + " | ".join(fields))
    return "\n".join(summary_lines)


def _clean_model_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"null", "none", "aucun", "aucune"}:
        return None
    return cleaned


def _extract_model_items(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = []
    for entry in value:
        if isinstance(entry, str):
            item_text = entry
        elif isinstance(entry, dict):
            item_text = entry.get("text", entry.get("content", ""))
        else:
            item_text = ""
        cleaned = _clean_model_value(item_text)
        if not cleaned:
            return None
        items.append(cleaned)
    return items


def _extract_classification(response_data: object, source_text: str) -> dict | None:
    if not isinstance(response_data, dict):
        return None
    parsed_content = response_data

    category = parsed_content.get("category")
    allowed_categories = ("note", "liste", "rendez-vous")
    if category not in allowed_categories:
        return None

    action = parsed_content.get("action", "create")
    if not isinstance(action, str) or action not in {"create", "update"}:
        return None

    raw_item_id = parsed_content.get("item_id")
    if raw_item_id is not None and not isinstance(raw_item_id, str):
        return None
    item_id = _clean_model_value(raw_item_id)
    if action == "update" and not item_id:
        return None
    if action == "create" and item_id:
        return None

    raw_text = parsed_content.get("text", parsed_content.get("content"))
    if raw_text is not None and not isinstance(raw_text, str):
        return None
    cleaned_text = _clean_model_value(raw_text)
    if action == "create" and not cleaned_text:
        return None

    raw_items = parsed_content.get("items", [])
    if raw_items is None and action == "update":
        items = []
    else:
        items = _extract_model_items(raw_items)
    if items is None:
        return None
    if action == "create" and category == "liste" and not items:
        return None
    if category != "liste" and items:
        return None

    raw_title = parsed_content.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return None
    title = _clean_model_value(raw_title)
    if category != "liste" and title:
        return None

    raw_date = parsed_content.get("date")
    raw_heure = parsed_content.get("heure")
    if raw_date is not None and not isinstance(raw_date, str):
        return None
    if raw_heure is not None and not isinstance(raw_heure, str):
        return None

    extracted_date, extracted_heure = _extract_appointment_details(source_text)
    date_value = _clean_model_value(raw_date)
    heure_value = _clean_model_value(raw_heure)
    if action == "create":
        date_value = date_value or extracted_date
        heure_value = heure_value or extracted_heure

    return {
        "action": action,
        "item_id": item_id,
        "category": category,
        "text": cleaned_text,
        "title": (
            title or "Liste"
            if category == "liste" and action == "create"
            else title
        ),
        "items": items,
        "date": date_value or None,
        "heure": heure_value or None,
    }


def _classify_with_claude(text: str, existing_items: list[dict] | None = None) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La clé ANTHROPIC_API_KEY n'est pas configurée.",
        )

    existing_summary = _summarise_existing_items(existing_items or [])
    prompt = f"""
Analyse et nettoie cette transcription vocale française.

Retourne uniquement un objet JSON valide, sans markdown ni explication, avec
exactement cette structure :
{{
  "action": "create" | "update",
  "item_id": "id de l'élément à modifier, ou null",
  "category": "note" | "liste" | "rendez-vous",
  "text": "texte principal reformulé proprement, ou null si inchangé",
  "title": "titre court de la liste, ou null si inchangé",
  "items": ["élément 1", "élément 2"],
  "date": "date prononcée ou null si inchangée",
  "heure": "heure prononcée ou null si inchangée"
}}

Règles :
- Utilise "create" pour une nouvelle entrée.
- Utilise "update" uniquement si la transcription fait clairement référence à
  un élément existant ci-dessous, avec une intention explicite comme
  « modifie », « change », « déplace », « annule » ou « supprime », et une
  description qui correspond clairement à cet élément.
- Si aucun élément ne correspond clairement, ou si l'intention est ambiguë,
  utilise "create" par défaut. Il vaut mieux créer une nouvelle entrée que
  modifier la mauvaise.
- Pour "update", renvoie uniquement les champs qui changent réellement.
  Pour un rendez-vous, mets "text", "date" ou "heure" à null lorsqu'ils ne
  changent pas afin de ne pas écraser la valeur existante. "item_id" doit
  toujours être l'identifiant exact fourni ci-dessous.
- Supprime les tics de langage et répétitions orales comme « euh », « du coup »,
  « oué », « donc » et « il faut que il faut que ».
- Pour une note, écris une phrase courte, naturelle et utile, souvent à
  l'infinitif. Exemple : « il faut que il faut que j'aille faire les courses
  et que j'achète des crêpes et de la farine » devient
  « Acheter des crêpes et de la farine ».
- Pour une liste de courses, d'objectifs ou de tâches, mets chaque élément
  distinct dans "items", sans les regrouper dans une phrase. Donne à la liste
  un titre court dans "title". Conserve dans le titre les informations utiles
  comme « demain ». Exemple : « oué du coup demain faut que j'aille faire les
  courses et que je prenne du pain du nutella des oeufs » donne le titre
  « Courses — demain » et les éléments « Pain », « Nutella », « Œufs ».
- Pour un rendez-vous, "text" doit être une phrase courte comme
  « Rendez-vous chez le coiffeur », sans la date, l'heure ni les mots parasites.
- Pour "date" et "heure", extrais les expressions telles qu'elles sont
  prononcées (« demain », « jeudi », « 17h30 »). Ne convertis jamais une date
  relative en date calendrier. Mets null si l'information est absente.
- Pour une note ou un rendez-vous, "items" doit être [] et "title" doit être
  null.

Éléments existants récents (référence uniquement, ne les modifie pas sans
intention explicite) :
{existing_summary}

Transcription :
{text}
""".strip()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            extra_body={"temperature": 0.1},
            system="Tu es un classificateur strict. Tu ne retournes qu'un JSON valide.",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Impossible de contacter le service Claude.",
        ) from exc

    model_content = "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
        and isinstance(getattr(block, "text", None), str)
    ).strip()
    model_content = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", model_content, flags=re.IGNORECASE | re.DOTALL
    ).strip()
    try:
        response_data = json.loads(model_content)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Claude a renvoyé une réponse JSON invalide.",
        ) from exc

    classification = _extract_classification(response_data, text)
    if classification is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="La réponse de Claude ne contient pas une classification structurée valide.",
        )
    return classification


def _apply_update(item: dict, update_fields: dict[str, object]) -> None:
    category = item.get("category")
    if category == "note":
        if "content" not in update_fields or update_fields["content"] is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une note doit recevoir un nouveau champ « content ».",
            )
        if set(update_fields) != {"content"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une note accepte uniquement le champ « content ».",
            )
        content = update_fields["content"]
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Le champ « content » ne peut pas être nul.",
            )
        item["content"] = content.strip()
        return

    if category == "rendez-vous":
        allowed_fields = {"content", "date", "heure"}
        if not set(update_fields).intersection(allowed_fields):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Un rendez-vous doit recevoir au moins un champ parmi « content », « date » ou « heure ».",
            )
        if not set(update_fields).issubset(allowed_fields):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Un rendez-vous n'accepte pas les opérations de liste.",
            )

        if "content" in update_fields:
            content = update_fields["content"]
            if not isinstance(content, str) or not content.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Le champ « content » ne peut pas être nul.",
                )
            item["content"] = content.strip()
            extracted_date, extracted_heure = _extract_appointment_details(item["content"])
            if "date" not in update_fields:
                item["date"] = extracted_date
            if "heure" not in update_fields:
                item["heure"] = extracted_heure

        if "date" in update_fields:
            date = update_fields["date"]
            item["date"] = date.strip() if isinstance(date, str) else ""
        if "heure" in update_fields:
            heure = update_fields["heure"]
            item["heure"] = heure.strip() if isinstance(heure, str) else ""

        resolved = _resolve_reminder_datetime(
            item.get("date"), item.get("heure"), item.get("created_at")
        )
        item["reminder_at"] = resolved.isoformat() if resolved else None
        item["notified"] = False
        return

    if category == "liste":
        allowed_fields = {"action", "text", "index", "items", "title"}
        if not set(update_fields).issubset(allowed_fields):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une liste se modifie avec « action », « text », « index », « items » ou « title ».",
            )

        content = item.get("content")
        if not isinstance(content, list):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Le contenu de la liste est invalide.",
            )

        if "items" in update_fields:
            items = update_fields["items"]
            if not isinstance(items, list) or not items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La modification de liste nécessite des « items ».",
                )
            item["content"] = _normalise_list_content(items)
            if "title" in update_fields:
                title = update_fields["title"]
                if isinstance(title, str) and title.strip():
                    item["title"] = title.strip()
            return

        if "title" in update_fields:
            title = update_fields["title"]
            if title is None:
                item.pop("title", None)
            elif isinstance(title, str) and title.strip():
                item["title"] = title.strip()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Le titre de la liste doit être un texte non vide.",
                )
            if "action" not in update_fields:
                return

        action = update_fields.get("action")
        if action == "add":
            text = update_fields.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="L'action « add » nécessite un champ « text ».",
                )
            if "index" in update_fields:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="L'action « add » n'utilise pas le champ « index ».",
                )
            content.append({"text": text.strip(), "completed": False})
        elif action == "delete":
            index = update_fields.get("index")
            if not isinstance(index, int):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="L'action « delete » nécessite un champ « index ».",
                )
            if "text" in update_fields or index >= len(content):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="L'élément de liste demandé est introuvable.",
                )
            content.pop(index)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une liste nécessite l'action « add » ou « delete ».",
            )
        return

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Catégorie d'élément inconnue.",
    )


# --------------------------------------------------------------------------
# Rappels push
# --------------------------------------------------------------------------

def _vapid_config() -> tuple[str, str, str] | None:
    public_key = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("VAPID_SUBJECT", "").strip()
    if not public_key or not private_key or not subject:
        return None
    return public_key, private_key, subject


def _require_vapid_config() -> tuple[str, str, str]:
    config = _vapid_config()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="La configuration VAPID n'est pas complète.",
        )
    return config


def _reminder_minutes_before() -> int:
    raw_value = os.getenv("REMINDER_MINUTES_BEFORE", "30")
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 30


def _send_due_reminders() -> dict:
    config = _vapid_config()
    if config is None:
        LOGGER.warning("Rappels push désactivés : configuration VAPID incomplète.")
        return {"sent": 0, "reason": "vapid_not_configured"}
    _, private_key, subject = config

    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=_reminder_minutes_before())

    response = (
        supabase.table("items")
        .select("*")
        .eq("category", "rendez-vous")
        .eq("notified", False)
        .not_.is_("reminder_at", "null")
        .gte("reminder_at", now.isoformat())
        .lte("reminder_at", deadline.isoformat())
        .execute()
    )
    due_items = response.data or []
    subscriptions = _fetch_subscriptions()
    if not due_items or not subscriptions:
        return {"sent": 0}

    expired_endpoints: set[str] = set()
    successfully_notified: set[str] = set()
    for item in due_items:
        payload = json.dumps(
            {
                "title": str(item.get("content") or "Rendez-vous"),
                "body": str(item.get("heure") or item.get("date") or "Rappel"),
                "item_id": item.get("id"),
            },
            ensure_ascii=False,
        )
        sent_for_item = False
        for row in subscriptions:
            sub_info = _to_subscription_info(row)
            try:
                webpush(
                    subscription_info=sub_info,
                    data=payload,
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject},
                    ttl=3600,
                )
                sent_for_item = True
            except WebPushException as exc:
                response_obj = getattr(exc, "response", None)
                if getattr(response_obj, "status_code", None) == 410:
                    expired_endpoints.add(row["endpoint"])
                LOGGER.warning("Échec d'envoi du rappel push : %s", exc)
            except Exception:
                LOGGER.exception("Erreur inattendue lors d'un envoi push.")
        if sent_for_item and item.get("id"):
            successfully_notified.add(item["id"])

    if expired_endpoints:
        _delete_subscriptions(expired_endpoints)
    if successfully_notified:
        supabase.table("items").update({"notified": True}).in_(
            "id", list(successfully_notified)
        ).execute()

    return {"sent": len(successfully_notified)}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/items", response_model=list[Item], response_model_exclude_none=True)
def get_items() -> list[dict]:
    return _fetch_items()


@app.post(
    "/items",
    response_model=Item,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
def create_item(item: ItemCreate) -> dict:
    return _insert_item(item.category, item.content, item.date, item.heure, item.title)


@app.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: str) -> Response:
    deleted = _delete_item_row(item_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"L'élément '{item_id}' est introuvable.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.patch("/items/{item_id}", response_model=Item, response_model_exclude_none=True)
def update_item(item_id: str, update: ItemUpdate) -> dict:
    update_fields = {field: getattr(update, field) for field in update.model_fields_set}

    item = _fetch_item(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"L'élément '{item_id}' est introuvable.",
        )
    _apply_update(item, update_fields)
    return _update_item_row(item)


@app.patch(
    "/items/{item_id}/checklist/{item_index}",
    response_model=Item,
    response_model_exclude_none=True,
)
def update_checklist_item(item_id: str, item_index: int, update: ChecklistItemUpdate) -> dict:
    if item_index < 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="L'élément de liste demandé est introuvable.",
        )

    item = _fetch_item(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"L'élément '{item_id}' est introuvable.",
        )
    if item.get("category") != "liste":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"L'élément '{item_id}' n'est pas une catégorie « liste ».",
        )

    content = item.get("content")
    if not isinstance(content, list) or item_index >= len(content):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="L'élément de liste demandé est introuvable.",
        )

    checklist_item = content[item_index]
    if not isinstance(checklist_item, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Le contenu de la liste est invalide.",
        )

    checklist_item["completed"] = update.completed
    return _update_item_row(item)


@app.post(
    "/transcribe",
    response_model=Item,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
def transcribe(request: TranscriptionRequest) -> dict:
    source_text = request.text.strip()
    existing_items = _fetch_items()
    classification = _classify_with_claude(source_text, existing_items)

    category = classification["category"]
    if classification["action"] == "update":
        update_fields: dict[str, object] = {}
        if classification["text"] is not None:
            update_fields["content"] = classification["text"]
        if classification["date"] is not None:
            update_fields["date"] = classification["date"]
        if classification["heure"] is not None:
            update_fields["heure"] = classification["heure"]
        if category == "liste":
            if classification["items"]:
                update_fields["items"] = classification["items"]
            if classification["title"] is not None:
                update_fields["title"] = classification["title"]

        item = _fetch_item(classification["item_id"])
        if item is not None and item.get("category") == category:
            if not update_fields:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Claude a identifié une modification sans nouvelle valeur à appliquer.",
                )
            _apply_update(item, update_fields)
            return _update_item_row(item)

    if category == "liste":
        content = [{"text": item_text, "completed": False} for item_text in classification["items"]]
        if not content:
            content = _normalise_list_content(classification["text"] or source_text)
    else:
        content = classification["text"] or source_text

    return _insert_item(
        category,
        content,
        classification["date"],
        classification["heure"],
        classification["title"],
    )


@app.post("/subscribe")
def subscribe(subscription: PushSubscription) -> dict[str, bool]:
    _upsert_subscription(subscription)
    return {"subscribed": True}


@app.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(subscription: SubscriptionEndpoint) -> Response:
    _delete_subscription(subscription.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/vapid-public-key")
def get_vapid_public_key() -> dict[str, str]:
    public_key, _, _ = _require_vapid_config()
    return {"publicKey": public_key}


@app.get("/cron/reminders")
def cron_reminders(secret: str = "") -> dict:
    """Appelée par un service de cron externe toutes les ~5 minutes."""

    if CRON_SECRET and secret != CRON_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Secret invalide.")
    return _send_due_reminders()
