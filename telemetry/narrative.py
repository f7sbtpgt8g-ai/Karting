"""Narrative generation (Part 4): turns already-computed, deterministic
corner-comparison facts (`corner_engine.py`) into plain-language coaching
sentences.

Diagnosis and phrasing are kept strictly separate: this module only ever
receives already-classified pattern facts (pattern_type, confidence, the
per-zone/per-point deltas) and turns them into a sentence -- it never
re-derives or second-guesses the numbers. Two phrasing backends:

- `templated_sentence` (default, no external dependency): a solid,
  deterministic template per pattern type.
- `anthropic_phrasing` (optional): asks Claude to phrase the same facts more
  naturally, with a tightly scoped prompt that forbids inventing any cause,
  number, or recommendation not present in the input. Requires the
  `anthropic` package and an `ANTHROPIC_API_KEY` environment variable; any
  failure (missing package/key, API error, empty response) falls back to
  `templated_sentence` rather than breaking the page -- this is always an
  optional enhancement, never a hard dependency.
"""

from __future__ import annotations

import json
import os

import pandas as pd

ANTHROPIC_MODEL = "claude-sonnet-5"

ANTHROPIC_SYSTEM_PROMPT = (
    "You are a karting coach writing one or two short, plain-language sentences about a single corner, based "
    "strictly on the structured facts given as JSON in the user message. State only what is in that JSON: never "
    "invent a cause, a number, or a recommendation that isn't present in the input. Do not restate the raw JSON "
    "keys verbatim -- write natural prose a driver would actually read, in second person ('you'). Keep it to 1-2 "
    "sentences and do not add any preamble or sign-off."
)


def templated_sentence(row: dict) -> str:
    """A driver-facing sentence for one classified corner comparison,
    built from a fixed template per `pattern_type` -- the no-external-
    dependency fallback (and, if `use_anthropic=False`, the only) phrasing
    path. `row` is one row (as a dict) of `corner_engine.compare_corners`'s
    output, i.e. it has `corner_label`, `pattern_type`, `net_time_impact_s`,
    the per-zone/per-point deltas, and `evidence`.
    """
    pattern = row.get("pattern_type")
    label = row.get("corner_label", "This corner")
    net = row.get("net_time_impact_s", 0.0) or 0.0
    ev = row.get("evidence") or {}
    root_cause = row.get("root_cause_corner")

    if pattern == "compromised_exit_fast_entry":
        sentence = (
            f"{label}: you carried {ev.get('entry_speed_delta_kmh', 0):.1f} km/h more entry speed than your "
            f"reference lap and gained {abs(ev.get('zone_b_delta_s', 0)):.2f}s through the corner itself, but the "
            f"compromised exit cost {ev.get('zone_c_delta_s', 0):.2f}s down the following straight -- a net loss "
            f"of {net:.2f}s."
        )
    elif pattern == "conservative_entry_strong_exit":
        sentence = (
            f"{label}: a {abs(ev.get('entry_speed_delta_kmh', 0)):.1f} km/h slower entry set up a stronger exit, "
            f"gaining {abs(net):.2f}s net across the corner and following straight combined."
        )
    elif pattern == "early_apex_exit_compromised":
        sentence = (
            f"{label}: the apex came {abs(ev.get('apex_distance_delta_m', 0)):.0f}m earlier than your reference "
            f"lap -- using up the corner early cost {ev.get('zone_c_delta_s', 0):.2f}s in exit speed and time down "
            "the following straight."
        )
    elif pattern == "late_apex_exit_rewarded":
        sentence = (
            f"{label}: a later apex ({ev.get('apex_distance_delta_m', 0):.0f}m further round) paid off with a "
            f"stronger exit, gaining {abs(ev.get('zone_c_delta_s', 0)):.2f}s down the following straight."
        )
    elif pattern == "braking_point_delta_no_pace_change":
        direction = "earlier" if ev.get("entry_distance_delta_m", 0) < 0 else "later"
        distance = abs(ev.get("entry_distance_delta_m", 0))
        if ev.get("subtype") == "earlier_with_margin":
            sentence = (
                f"{label}: braking {distance:.0f}m {direction} than your reference lap with no difference in pace "
                "through the corner or on exit -- likely margin to spare rather than a mistake."
            )
        else:
            sentence = (
                f"{label}: braking {distance:.0f}m {direction} than your reference lap with no difference in pace "
                "through the corner or on exit -- a later brake point that isn't costing you anything."
            )
    elif pattern == "clean_no_significant_delta":
        sentence = f"{label}: no significant difference from your reference lap here."
    elif pattern == "unclassified_time_delta":
        direction = "loss" if net > 0 else "gain"
        sentence = (
            f"{label}: a {abs(net):.2f}s net {direction} vs. your reference lap that doesn't match a known "
            "pattern -- worth reviewing the raw trace directly."
        )
    else:
        direction = "loss" if net > 0 else "gain"
        sentence = f"{label}: {abs(net):.2f}s net {direction} vs. your reference lap."

    if root_cause:
        sentence += f" This traces back to {root_cause}, part of the same corner complex."
    return sentence


def _anthropic_payload(row: dict) -> dict:
    return {
        "corner": row.get("corner_label"),
        "pattern_type": row.get("pattern_type"),
        "confidence": row.get("confidence"),
        "net_time_impact_s": round(row.get("net_time_impact_s", 0.0) or 0.0, 3),
        "entry_speed_delta_kmh": round(row.get("entry_speed_delta_kmh", 0.0) or 0.0, 1),
        "apex_speed_delta_kmh": round(row.get("apex_speed_delta_kmh", 0.0) or 0.0, 1),
        "exit_speed_delta_kmh": round(row.get("exit_speed_delta_kmh", 0.0) or 0.0, 1),
        "zone_a_braking_delta_s": round(row.get("zone_a_delta_s", 0.0) or 0.0, 3),
        "zone_b_corner_arc_delta_s": round(row.get("zone_b_delta_s", 0.0) or 0.0, 3),
        "zone_c_following_straight_delta_s": round(row.get("zone_c_delta_s", 0.0) or 0.0, 3),
        "root_cause_corner": row.get("root_cause_corner"),
        "evidence": row.get("evidence") or {},
    }


def anthropic_phrasing(row: dict, client=None) -> str | None:
    """Ask Claude to phrase one corner's already-classified facts as 1-2
    natural sentences. Returns None (caller should fall back to
    `templated_sentence`) on any failure -- missing package, missing API
    key, or an API error."""
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = client or anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=150,
            system=ANTHROPIC_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(_anthropic_payload(row))}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
        return text or None
    except Exception:
        return None


def narrative_sentence(row: dict, use_anthropic: bool = False, client=None) -> str:
    """One driver-facing sentence for a classified corner comparison, via
    Anthropic phrasing if requested and available, else the templated
    fallback."""
    if use_anthropic:
        phrased = anthropic_phrasing(row, client=client)
        if phrased:
            return phrased
    return templated_sentence(row)


def rank_headline_findings(comparisons: pd.DataFrame, n: int = 5, use_anthropic: bool = False) -> list[dict]:
    """Top-N corners by |net_time_impact_s|, headline-eligible only, each
    with a generated narrative sentence -- the lead output for the Lap
    Comparison page, same plain-language-first principle as the existing
    Top 3 Focus Areas screen. The full per-corner table (every corner, every
    metric) is expected to still be shown underneath by the caller -- this
    is only the headline slice.
    """
    if comparisons.empty:
        return []
    headline_rows = comparisons[comparisons["headline"] & (comparisons["pattern_type"] != "clean_no_significant_delta")]
    if headline_rows.empty:
        return []
    headline_rows = headline_rows.reindex(headline_rows["net_time_impact_s"].abs().sort_values(ascending=False).index)

    results = []
    for _, row in headline_rows.head(n).iterrows():
        d = row.to_dict()
        d["narrative"] = narrative_sentence(d, use_anthropic=use_anthropic)
        results.append(d)
    return results
