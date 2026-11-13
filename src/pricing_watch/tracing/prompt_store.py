"""Prompts in Langfuse Prompt Management: one chat prompt, `pricing-agent` = [system, incident].

Runs fetch it by label (`production` or `ci`), cached by the SDK. When Langfuse is unreachable,
or holds a version whose placeholders the code can't fill in, the local templates are used and
the generation stays unlinked, so a bad edit in the UI can't crash incidents.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any

from langfuse import Langfuse
from langfuse.model import ChatMessageDict

from pricing_watch.agent.prompts import (
    INCIDENT_TEMPLATE,
    LOCAL_PROMPTS,
    PROMPT_NAME,
    SYSTEM_TEMPLATE,
    Prompts,
)

CACHE_TTL_S = 60
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
log = logging.getLogger("pricing_watch")


def _local_messages() -> list[ChatMessageDict]:
    return [{"role": "system", "content": SYSTEM_TEMPLATE},
            {"role": "user", "content": INCIDENT_TEMPLATE}]  # fmt: skip


def from_messages(messages: Sequence[Any], name: str, version: int) -> Prompts | None:
    """The fetched prompt, or None if it isn't [system, user] with exactly the placeholders the
    code fills in."""
    if len(messages) != 2:
        return None
    system, incident = (str(m["content"]) for m in messages)
    if _PLACEHOLDER.search(system) or set(_PLACEHOLDER.findall(incident)) != set(
        _PLACEHOLDER.findall(INCIDENT_TEMPLATE)
    ):
        return None
    return Prompts(system=system, incident=incident, name=name, version=version)


def fetch_prompts(client: Langfuse, label: str) -> Prompts:
    try:
        prompt = client.get_prompt(PROMPT_NAME, label=label, type="chat",
                                   cache_ttl_seconds=CACHE_TTL_S, fallback=_local_messages(),
                                   max_retries=0, fetch_timeout_seconds=5)  # fmt: skip
    except Exception as e:  # never let prompt storage stop a run: the local copies work
        log.warning("prompt %s@%s unavailable (%s); using the local templates", PROMPT_NAME,
                    label, e)  # fmt: skip
        return LOCAL_PROMPTS
    if prompt.is_fallback:
        return LOCAL_PROMPTS
    fetched = from_messages(prompt.prompt, PROMPT_NAME, prompt.version)
    if fetched is None:
        log.warning("prompt %s v%s doesn't fit the code's placeholders; using the local "
                    "templates", PROMPT_NAME, prompt.version)  # fmt: skip
        return LOCAL_PROMPTS
    return fetched


def push_prompts(client: Langfuse, label: str) -> int:
    """Make the local templates the prompt under this label. A version with identical content
    (under this label, or the latest) is reused, so pushing twice never makes a copy. Returns
    the version now carrying the label."""
    local = _content(_local_messages())
    for where in (label, "latest"):
        try:
            current = client.get_prompt(PROMPT_NAME, label=where, type="chat",
                                        cache_ttl_seconds=0, max_retries=0)  # fmt: skip
        except Exception:  # nothing under that label yet
            continue
        if _content(current.prompt) == local:
            if where != label:
                client.update_prompt(name=PROMPT_NAME, version=current.version, new_labels=[label])
            return int(current.version)
    messages: list[Any] = list(_local_messages())  # the SDK's list type is a wider union
    created = client.create_prompt(name=PROMPT_NAME, prompt=messages, labels=[label], type="chat")
    return int(created.version)


def _content(messages: Sequence[Any]) -> list[tuple[str, str]]:
    """Role and text only: fetched messages carry extra keys, such as type."""
    return [(str(m["role"]), str(m["content"])) for m in messages]
