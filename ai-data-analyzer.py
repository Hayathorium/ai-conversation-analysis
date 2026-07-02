#!/usr/bin/env python3
"""
ai-data-analyzer.py
====================

Pipeline:
  1. Load discord_messages.csv with pandas, preserving large IDs as strings.
  2. Sort chronologically and build a registry of User ID -> latest
     Display Name / Avatar URL.
  3. For each user (unique User ID), send their most recent N messages to
     Gemini and ask for a structured behavioral-persona JSON object.
  4. Build an interaction graph: an edge is created between two users when
     one replies directly to the other (Reply To), or when they post in
     immediate succession in the same channel within a short time window.
  5. For each edge, send up to 200 messages of shared conversational history
     to Gemini and ask for a structured relationship-dynamics JSON object.
  6. Write personas + interactions + summary stats to analytics_cache.json.

Requirements:
  pip install pandas google-genai --break-system-packages

Environment:
  GEMINI_API_KEY must be set in the environment.

Usage:
  python ai-data-analyzer.py --csv discord_messages.csv --out analytics_cache.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import combinations
from typing import Any

import pandas as pd

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    genai = None
    genai_types = None

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

MODEL_NAME = "gemini-3.1-flash-lite"
MAX_MESSAGES_PER_PERSONA = 200
MAX_MESSAGES_PER_RELATIONSHIP = 200
SUCCESSIVE_REPLY_WINDOW = timedelta(minutes=5)
MAX_CONCURRENT_REQUESTS = 4
REQUEST_RETRIES = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ai-data-analyzer")

# --------------------------------------------------------------------------
# Structured output schemas (used for Gemini's response_schema)
# --------------------------------------------------------------------------

PERSONA_SCHEMA = {
    "type": "object",
    "properties": {
        "username": {"type": "string"},
        "archetype": {"type": "string"},
        "summary": {"type": "string"},
        "key_traits": {"type": "array", "items": {"type": "string"}},
        "frequent_topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["username", "archetype", "summary", "key_traits", "frequent_topics"],
}

RELATIONSHIP_SCHEMA = {
    "type": "object",
    "properties": {
        "usernames": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
        "relationship_type": {"type": "string"},
        "vibe_score": {"type": "number"},
        "dynamic_summary": {"type": "string"},
    },
    "required": ["usernames", "relationship_type", "vibe_score", "dynamic_summary"],
}


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------

@dataclass
class AgentRegistryEntry:
    user_id: str
    display_name: str
    avatar_url: str
    message_count: int = 0


@dataclass
class InteractionEdge:
    user_a: str
    user_b: str
    weight: int = 0
    reply_count: int = 0
    succession_count: int = 0
    shared_message_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Step 1: Load & normalize CSV
# --------------------------------------------------------------------------

def load_messages(csv_path: str) -> pd.DataFrame:
    log.info("Loading %s", csv_path)
    dtype_map = {
        "Message ID": str,
        "User ID": str,
        "Reply To": str,
        "Server": str,
        "Channel": str,
        "Display Name": str,
        "Avatar URL": str,
        "Content": str,
        "Attachments": str,
        "Edited": str,
        "Pinned": str,
    }
    df = pd.read_csv(
        csv_path,
        dtype=dtype_map,
        keep_default_na=False,
        na_values=[""],
        engine="python",
    )

    required_cols = {
        "Server", "Channel", "Message ID", "Timestamp", "Display Name",
        "Avatar URL", "User ID", "Content", "Attachments", "Reply To",
        "Edited", "Pinned",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)}")

    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["Timestamp"])
    df = df.sort_values("Timestamp", kind="mergesort").reset_index(drop=True)

    # Normalize IDs: strip stray whitespace, guard against float-ification
    for col in ("Message ID", "User ID", "Reply To"):
        df[col] = df[col].apply(lambda v: _clean_id(v))

    df["Content"] = df["Content"].fillna("").astype(str)
    log.info("Loaded %d messages across %d channels", len(df), df["Channel"].nunique())
    return df


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return None
    # Guard against pandas/Excel coercing big int-like IDs to floats (e.g. "123.0")
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


# --------------------------------------------------------------------------
# Step 2: Build user registry (latest display name / avatar per User ID)
# --------------------------------------------------------------------------

def build_registry(df: pd.DataFrame) -> dict[str, AgentRegistryEntry]:
    registry: dict[str, AgentRegistryEntry] = {}
    counts = df["User ID"].value_counts()

    for _, row in df.iterrows():
        uid = row["User ID"]
        if not uid:
            continue
        # Since df is sorted chronologically, later rows overwrite earlier ones,
        # leaving the *latest* display name / avatar for each user.
        registry[uid] = AgentRegistryEntry(
            user_id=uid,
            display_name=row["Display Name"] or uid,
            avatar_url=row["Avatar URL"] or "",
            message_count=int(counts.get(uid, 0)),
        )

    log.info("Registered %d unique users", len(registry))
    return registry


# --------------------------------------------------------------------------
# Step 3: Interaction graph construction
# --------------------------------------------------------------------------

def build_interaction_edges(df: pd.DataFrame) -> dict[tuple[str, str], InteractionEdge]:
    edges: dict[tuple[str, str], InteractionEdge] = {}
    msg_by_id = df.set_index("Message ID", drop=False)

    def edge_key(a: str, b: str) -> tuple[str, str]:
        return tuple(sorted((a, b)))  # type: ignore[return-value]

    def get_edge(a: str, b: str) -> InteractionEdge:
        key = edge_key(a, b)
        if key not in edges:
            edges[key] = InteractionEdge(user_a=key[0], user_b=key[1])
        return edges[key]

    # (a) Direct replies
    for _, row in df.iterrows():
        reply_to = row["Reply To"]
        if not reply_to or reply_to not in msg_by_id.index:
            continue
        parent = msg_by_id.loc[reply_to]
        if isinstance(parent, pd.DataFrame):  # duplicate IDs guard
            parent = parent.iloc[0]
        a, b = row["User ID"], parent["User ID"]
        if not a or not b or a == b:
            continue
        edge = get_edge(a, b)
        edge.reply_count += 1
        edge.weight += 2  # explicit replies count more heavily
        edge.shared_message_ids.append(row["Message ID"])

    # (b) Immediate succession within the same channel & time window
    for (server, channel), group in df.groupby(["Server", "Channel"]):
        group = group.sort_values("Timestamp")
        prev_row = None
        for _, row in group.iterrows():
            if prev_row is not None:
                gap = row["Timestamp"] - prev_row["Timestamp"]
                a, b = row["User ID"], prev_row["User ID"]
                if a and b and a != b and gap <= SUCCESSIVE_REPLY_WINDOW:
                    edge = get_edge(a, b)
                    edge.succession_count += 1
                    edge.weight += 1
                    edge.shared_message_ids.append(row["Message ID"])
            prev_row = row

    log.info("Built interaction graph with %d edges", len(edges))
    return edges


def shared_history(df: pd.DataFrame, user_a: str, user_b: str, limit: int) -> pd.DataFrame:
    subset = df[df["User ID"].isin([user_a, user_b])].sort_values("Timestamp")
    return subset.tail(limit)


# --------------------------------------------------------------------------
# Step 4: Gemini client wrapper
# --------------------------------------------------------------------------

class GeminiAnalyzer:
    def __init__(self, api_key: str | None = None, model: str = MODEL_NAME):
        if genai is None:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install google-genai --break-system-packages"
            )
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set in the environment.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _generate_json(self, prompt: str, schema: dict) -> dict | None:
        async with self.semaphore:
            for attempt in range(1, REQUEST_RETRIES + 1):
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.6,
                        ),
                    )
                    text = response.text
                    return json.loads(text)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Gemini call failed (attempt %d/%d): %s", attempt, REQUEST_RETRIES, exc)
                    await asyncio.sleep(2 ** attempt)
        return None

    async def generate_persona(self, agent: AgentRegistryEntry, messages: pd.DataFrame) -> dict | None:
        transcript = _format_transcript(messages)
        prompt = f"""You are analyzing the behavioral pattern of one participant ("{agent.display_name}") in a groupchat.

Study ONLY the messages authored by "{agent.display_name}" below (their most recent
{len(messages)} messages) and describe the emergent conversational persona this user
exhibits: tone, recurring rhetorical habits, and topical focus. Respond strictly
according to the provided JSON schema.

Transcript (only this user's messages, chronological):
{transcript}
"""
        result = await self._generate_json(prompt, PERSONA_SCHEMA)
        if result:
            result["username"] = agent.display_name
        return result

    async def generate_relationship(
        self, agent_a: AgentRegistryEntry, agent_b: AgentRegistryEntry, messages: pd.DataFrame
    ) -> dict | None:
        transcript = _format_transcript(messages)
        prompt = f"""You are analyzing the emergent interaction dynamic between two users,
"{agent_a.display_name}" and "{agent_b.display_name}". Study their shared conversational history below
and characterize the relationship dynamic that has emerged between them. Respond
strictly according to the provided JSON schema, with "usernames" containing exactly
these two names: ["{agent_a.display_name}", "{agent_b.display_name}"].

Shared conversation history (chronological, interleaved):
{transcript}
"""
        result = await self._generate_json(prompt, RELATIONSHIP_SCHEMA)
        if result:
            result["usernames"] = [agent_a.display_name, agent_b.display_name]
        return result


def _format_transcript(messages: pd.DataFrame) -> str:
    lines = []
    for _, row in messages.iterrows():
        ts = row["Timestamp"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] {row['Display Name']}: {row['Content']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Step 5: Orchestration
# --------------------------------------------------------------------------

async def run_pipeline(csv_path: str, out_path: str, api_key: str | None) -> None:
    df = load_messages(csv_path)
    registry = build_registry(df)
    edges = build_interaction_edges(df)

    analyzer = GeminiAnalyzer(api_key=api_key)

    # --- Personas ---
    log.info("Generating personas for %d users...", len(registry))

    async def persona_task(agent: AgentRegistryEntry):
        agent_messages = df[df["User ID"] == agent.user_id].tail(MAX_MESSAGES_PER_PERSONA)
        if agent_messages.empty:
            return agent.user_id, None
        persona = await analyzer.generate_persona(agent, agent_messages)
        return agent.user_id, persona

    persona_results = await asyncio.gather(*(persona_task(a) for a in registry.values()))
    personas: dict[str, dict] = {}
    for user_id, persona in persona_results:
        if persona:
            personas[user_id] = {
                "user_id": user_id,
                "avatar_url": registry[user_id].avatar_url,
                "message_count": registry[user_id].message_count,
                **persona,
            }

    # --- Interactions ---
    log.info("Generating relationship reports for %d edges...", len(edges))

    async def relationship_task(edge: InteractionEdge):
        a, b = registry.get(edge.user_a), registry.get(edge.user_b)
        if a is None or b is None:
            return None
        history = shared_history(df, edge.user_a, edge.user_b, MAX_MESSAGES_PER_RELATIONSHIP)
        if history.empty:
            return None
        report = await analyzer.generate_relationship(a, b, history)
        if not report:
            return None
        return {
            "user_ids": [edge.user_a, edge.user_b],
            "weight": edge.weight,
            "reply_count": edge.reply_count,
            "succession_count": edge.succession_count,
            **report,
        }

    interaction_results = await asyncio.gather(*(relationship_task(e) for e in edges.values()))
    interactions = [r for r in interaction_results if r]

    # --- Summary stats (computed directly from the CSV, no LLM needed) ---
    summary = {
        "total_messages": int(len(df)),
        "total_agents": int(len(registry)),
        "total_channels": int(df["Channel"].nunique()),
        "total_servers": int(df["Server"].nunique()),
        "date_range": {
            "start": df["Timestamp"].min().isoformat(),
            "end": df["Timestamp"].max().isoformat(),
        },
        "messages_per_channel": df["Channel"].value_counts().to_dict(),
    }

    cache = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "model": MODEL_NAME,
        "summary": summary,
        "personas": personas,
        "interactions": interactions,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    log.info("Wrote %s (%d personas, %d interactions)", out_path, len(personas), len(interactions))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AI Conversation Analytics — backend engine")
    parser.add_argument("--csv", default="discord_messages.csv", help="Path to discord_messages.csv")
    parser.add_argument("--out", default="analytics_cache.json", help="Path to write analytics_cache.json")
    parser.add_argument("--api-key", default=None, help="Gemini API key (defaults to GEMINI_API_KEY env var)")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        log.error("CSV file not found: %s", args.csv)
        sys.exit(1)

    try:
        asyncio.run(run_pipeline(args.csv, args.out, args.api_key))
    except RuntimeError as exc:
        log.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
