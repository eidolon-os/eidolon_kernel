"""What a Host can do that another Host cannot, as a release has to see it.

A release brings the same components to every Host — except that one of them is
not on every Host at all. A board with an NPU listens and speaks with its own
models; one without reaches a provider for the same two things and should not
be carrying two gigabytes of weights it will never load.

The mechanism is not new here: `eidolon_ops.capabilities` states the closed set,
each component's `ops/component.toml` states what it requires, and a Host
profile states what it provides. Neither side names the other. What was missing
is that a release descriptor is validated *before* any of that is read — that is
the point of validating it, to refuse early — so the fixed sets in `manifest`
had no way to express a component that is present on one Host and absent on
another, and the conditional component could never ship at all.

So the set is stated a second time, here, for the same reason
`eidolon_ops.config.CAPABILITY_UNITS` states the unit topology a second time:
the early refusal has to be possible without loading the thing being refused.
A drift test holds the two together. This module deliberately imports nothing
from Ops — a release contract that depended on the operator's package would
have to be satisfied by whoever holds a descriptor, which includes the Host.
"""

from __future__ import annotations

__all__ = ["HOST_CAPABILITIES", "require_known_capabilities"]

#: Every capability a Host may provide and a component may require. Mirrors
#: ``eidolon_ops.capabilities.HOST_CAPABILITIES``; a test pins them equal.
HOST_CAPABILITIES: frozenset[str] = frozenset(
    {
        #: A Rockchip NPU with the RKNPU2 runtime, which is what makes RKNN and
        #: RKLLM model artifacts loadable rather than dead weight.
        "rknpu2",
        #: Speech recognition runs on this Host instead of a provider.
        "local_asr",
        #: Speech synthesis runs on this Host instead of a provider.
        "local_tts",
        #: The conversational model runs on this Host instead of a provider.
        "local_llm",
        "local_laya",
    }
)


def require_known_capabilities(values: object) -> frozenset[str]:
    """Refuse a capability nobody defined, on either side of the match.

    An open set fails silently: a capability misspelt in a descriptor selects
    no additions, so the expected sets come out as the baseline and the release
    is accepted as a Host that runs nothing extra — which is exactly what a
    Host with a typo would then install.
    """

    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("capabilities must be an array of strings")
    unknown = sorted(set(values) - HOST_CAPABILITIES)
    if unknown:
        known = ", ".join(sorted(HOST_CAPABILITIES))
        raise ValueError(
            f"unknown Host capability {unknown!r}. A capability must be one of: "
            f"{known}. An unknown name would otherwise select nothing, silently."
        )
    if len(set(values)) != len(values):
        raise ValueError("capabilities must not repeat")
    return frozenset(values)
