"""Markdown rendering and splitting for DingTalk messages."""

from __future__ import annotations

from acp_chat_bot.acp.models import AgentActivityBlock
from acp_chat_bot.core.chat import ChoiceRequest

ACTIVITY_LABELS = {
    "think": "💡 Thinking",
    "run": "⚙️ Running",
    "read": "📖 Reading",
    "edit": "✏️ Editing",
    "write": "✍️ Writing",
    "search": "🌐 Searching web",
    "query": "🔎 Querying",
}


def chunk_markdown(text: str, limit: int) -> list[str]:
    """Split markdown into chunks no longer than *limit*, preferring paragraph breaks."""
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def activity_markdown(block: AgentActivityBlock) -> str:
    label = ACTIVITY_LABELS.get(block.kind, block.kind.title() or "Working")
    text = f"**{label}**: {block.title}"
    if block.text.strip():
        text += f"\n\n{block.text.strip()}"
    return text


def choice_markdown(request: ChoiceRequest) -> str:
    lines = [f"**{request.title}**", request.body, ""]
    for index, option in enumerate(request.options, start=1):
        lines.append(f"{index}. {option.label}")
    if request.skippable:
        lines.append(f"{len(request.options) + 1}. Skip")
    lines.append("")
    lines.append("Reply with the number" + ("s" if request.multiple else "") + " of your choice.")
    return "\n".join(lines)
