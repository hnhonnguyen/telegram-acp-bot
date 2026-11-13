"""Payload helpers for streaming DingTalk AI Cards."""

from __future__ import annotations

import json

AI_CARD_TEMPLATE_ID = "382e4302-551d-4880-bf29-a30acfab2e71.schema"
CARD_CONTENT_KEY = "msgContent"
FLOW_STATUS_PROCESSING = "1"
FLOW_STATUS_INPUTING = "2"
FLOW_STATUS_FINISHED = "3"
_CARD_ORDER = (
    "msgTitle",
    "msgContent",
    "staticMsgContent",
    "msgTextList",
    "msgImages",
    "msgSlider",
    "msgButtons",
)


def processing_card_data() -> dict[str, str]:
    """Initial card data while the agent is still working."""
    return {"flowStatus": FLOW_STATUS_PROCESSING}


def inputing_card_data() -> dict[str, str]:
    """Card data that switches the card to its typing state before streaming."""
    return _card_data(FLOW_STATUS_INPUTING)


def finished_card_data(content: str) -> dict[str, str]:
    """Full card data for the completed answer."""
    return _card_data(FLOW_STATUS_FINISHED, content)


def _card_data(flow_status: str, content: str = "") -> dict[str, str]:
    return {
        "flowStatus": flow_status,
        "msgContent": content,
        "sys_full_json_obj": json.dumps({"order": list(_CARD_ORDER)}),
    }


def merge_segments(committed: str, preview: str) -> str:
    """Join finalized reply segments with the preview currently streaming."""
    return f"{committed}\n\n{preview}" if committed else preview
