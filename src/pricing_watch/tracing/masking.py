"""Content-off masking for Langfuse: text leaves the app as a placeholder; structure,
attributes, numbers, ids and tool arguments (sku, prices, quantities) stay, so traces can still
be scored. Text inside tool arguments, such as a review's reason, is masked too.

The SDK calls `mask_content` on each export batch. A raise there drops the whole batch, so
the hook is total: a span that can't be masked cleanly has its input and output replaced
outright, never sent as they are.
"""

import json
from typing import Any

from langfuse.types import (
    AttributeValue,
    MaskOtelSpansParams,
    MaskOtelSpansResult,
    OtelSpanPatch,
)

PLACEHOLDER = "[masked]"
_IO = ("langfuse.observation.input", "langfuse.observation.output")
# fields that carry prompt, message or outside text in our observation shapes
TEXT_FIELDS = frozenset({"content", "message", "text", "diagnosis", "supplier_description",
                         "note", "title", "reason", "error"})  # fmt: skip


def mask_content(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    patches = {}
    for key, span in params.spans.items():
        present = [a for a in _IO if a in span.attributes]
        try:
            changed = {a: _mask(span.attributes[a]) for a in present}
        except Exception:  # anything odd: send nothing of it, and never raise
            changed = dict.fromkeys(present, PLACEHOLDER)
        if changed:
            patches[key] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches)


def _mask(value: AttributeValue) -> str:
    if not isinstance(value, str):  # the SDK writes JSON strings; anything else is malformed
        return PLACEHOLDER
    return json.dumps(_scrub(json.loads(value), None))


def _scrub(value: Any, field: str | None) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, field) for v in value]
    if isinstance(value, str) and (field is None or field in TEXT_FIELDS):
        return PLACEHOLDER  # a text field, or a bare string that is the whole input or output
    return value
