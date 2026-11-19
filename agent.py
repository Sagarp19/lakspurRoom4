"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal

DATA_DIR = Path(__file__).resolve().parent / "data" / "americas"


# ── Build 2, step 2.1: two tools of our own ───────────────────────────────────
def care_entitlements(policy_row_id: str, loyalty_tier: str = "member",
                      wait_minutes_for_alternative: Optional[int] = None,
                      overnight: bool = False) -> Dict[str, Any]:
    """Meal / hotel / ground care for one policy row, from disruption_policy.json,
    with the Handbook section 6 text that backs it."""
    policy = json.loads((DATA_DIR / "disruption_policy.json").read_text(encoding="utf-8"))
    row = next((r for r in policy["rows"] if r["policy_row_id"] == policy_row_id), None)
    if row is None:
        return {"error": f"Unknown policy_row_id {policy_row_id!r}.",
                "valid_policy_row_ids": [r["policy_row_id"] for r in policy["rows"]]}

    care = row["care"]
    tier = (loyalty_tier or "member").strip().title()
    tier_rule = policy["loyalty_rules"].get(tier, {})

    # Meal: a flat amount, or a threshold on how long the wait for the alternative is.
    meal = care["meal_usd"]
    meal_note = None
    if isinstance(meal, dict):
        threshold = meal["if_wait_minutes_for_alternative_gte"]
        if wait_minutes_for_alternative is None:
            meal_note = (f"Depends on the wait: ${meal['amount']} if the wait for the new "
                         f"flight is {threshold} minutes or more, otherwise ${meal['else']}. "
                         f"Pass wait_minutes_for_alternative to resolve it.")
            meal = None
        else:
            meal = meal["amount"] if wait_minutes_for_alternative >= threshold else meal["else"]
    if meal:  # Gold and Summit get a bonus on any meal credit, up to a cap
        meal = min(meal + tier_rule.get("meal_bonus_usd", 0),
                   tier_rule.get("meal_bonus_cap_usd", meal + tier_rule.get("meal_bonus_usd", 0)))

    # Hotel: only "if_overnight" rows depend on the overnight flag. Summit has a courtesy override.
    hotel = dict(care["hotel"])
    if hotel.get("eligible") == "if_overnight":
        hotel["eligible"] = bool(overnight)
    courtesy = tier_rule.get("courtesy_overrides", {}).get("uncontrollable_overnight_hotel")
    if courtesy and overnight and policy_row_id in courtesy["applies_to_rows"]:
        hotel = {"eligible": True, "nightly_cap_usd": courtesy["nightly_cap_usd"],
                 "nights": courtesy["nights"], "approval": courtesy["approval"],
                 "label": courtesy["label"]}

    ground = care["ground_usd"]
    if isinstance(ground, dict):
        ground = ground["if_overnight"] if overnight else ground["else"]

    handbook = (DATA_DIR / "fare_rules_excerpt.md").read_text(encoding="utf-8")
    section = re.search(r"^### 6\..*?(?=^### |\Z)", handbook, re.S | re.M)

    return {
        "policy_row_id": policy_row_id,
        "loyalty_tier": tier,
        "per_passenger": {"meal_usd": meal, "ground_usd": ground},
        "meal_note": meal_note,
        "hotel": hotel,
        "amounts_are": policy["amount_semantics"],
        "handbook_section_6": section.group(0).strip() if section else None,
    }


def reopen_stats(intent_label: Optional[str] = None) -> Dict[str, Any]:
    """How often each kind of problem came back within 72 hours, from the eight
    human-handled conversations in transcripts_sample.jsonl."""
    lines = (DATA_DIR / "transcripts_sample.jsonl").read_text(encoding="utf-8").splitlines()
    convos = [json.loads(line) for line in lines if line.strip()]
    if intent_label:
        convos = [c for c in convos if c["intent_label"] == intent_label]
        if not convos:
            return {"error": f"No conversations with intent_label {intent_label!r}."}

    by_kind: Dict[str, Dict[str, Any]] = {}
    for c in convos:
        k = by_kind.setdefault(c["intent_label"], {"intent_label": c["intent_label"],
                                                   "conversations": 0, "reopened_within_72h": 0,
                                                   "reopen_reasons": []})
        k["conversations"] += 1
        if c["reopened_within_72h"]:
            k["reopened_within_72h"] += 1
            k["reopen_reasons"].append(c.get("reopen_reason"))
    for k in by_kind.values():
        k["reopen_rate"] = round(k["reopened_within_72h"] / k["conversations"], 2)

    reopened = sum(k["reopened_within_72h"] for k in by_kind.values())
    return {
        "source": "transcripts_sample.jsonl (human-handled conversations)",
        "total_conversations": len(convos),
        "total_reopened_within_72h": reopened,
        "overall_reopen_rate": round(reopened / len(convos), 2),
        "by_kind": sorted(by_kind.values(), key=lambda k: -k["reopen_rate"]),
        "caveat": "Tiny sample: each kind of problem appears about once, so a rate is "
                  "0% or 100% from one conversation. Report counts, not trends.",
    }


EXTRA_TOOLS: List[Dict[str, Any]] = [      # ✏️ Build 2, step 2.1: schemas for the tools you add
    {
        "name": "care_entitlements",
        "description": (
            "Spell out the care a customer gets while they wait: meal credit, hotel and "
            "ground transport, in dollars per passenger, with the Handbook section 6 "
            "(Care while you wait) text behind it. Call it AFTER check_policy, when the "
            "customer asks what they are owed for food, a hotel or transport, or why they "
            "are not getting one. It reads the policy row you pass and applies the "
            "customer's loyalty tier (Gold and Summit get a meal bonus; Summit has a "
            "courtesy hotel on weather overnights). It does not issue anything: use "
            "issue_voucher for that. Quote amounts only from this tool's answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_row_id": {"type": "string",
                                  "description": "The policy_row_id check_policy returned, e.g. UNC-CXL."},
                "loyalty_tier": {"type": "string",
                                 "description": "From lookup_booking: member, silver, gold or summit."},
                "wait_minutes_for_alternative": {"type": "integer",
                                                 "description": "Minutes until the customer's new flight. Needed to decide meal credit on cancellations."},
                "overnight": {"type": "boolean",
                              "description": "True if the customer is stranded overnight away from home. From check_policy."},
            },
            "required": ["policy_row_id"],
        },
    },
    {
        "name": "reopen_stats",
        "description": (
            "How often each kind of problem (rebooking after a cancellation, hotel "
            "request, refund versus credit, baggage and so on) came back to Larkspur "
            "within 72 hours, counted over the eight conversations that human agents "
            "handled. Use it when someone asks which problems tend to be re-opened, or "
            "to judge whether a resolution is likely to stick. It takes no booking "
            "information. The sample is tiny, so give counts and the reasons customers "
            "came back, never a trend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_label": {"type": "string",
                                 "description": "Optional. One kind of problem, e.g. refund_vs_credit. Omit for all kinds."},
            },
            "required": [],
        },
    },
]
LOCAL_TOOLS: Dict[str, Any] = {            # ✏️ Build 2, step 2.1: the functions behind them
    "care_entitlements": care_entitlements,
    "reopen_stats": reopen_stats,
}


def text_of(response) -> str:
    """Given. The last non-empty text block, never content[0]."""
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and b.text]
    return texts[-1] if texts else ""


def tool_results(response) -> List[Dict[str, Any]]:
    """Given. Runs every tool_use block and packages the results the way the
    API expects them back. A tool can live in three places: the MCP server,
    LOCAL_TOOLS, or support/tools.py."""
    # three branches, no try/except in this file: mcp_client.call_remote() and
    # support.call_local() answer with an error dict instead of raising, and both
    # record what came back on the trace
    results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name in mcp_client.tool_names:
            output = mcp_client.call_remote(block.name, block.input)
        elif block.name in LOCAL_TOOLS:
            output = call_local(LOCAL_TOOLS[block.name], block.name, block.input)
        else:
            output = execute_tool(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        })
    return results


def run_agent(pnr: str, last_name: str, message: str) -> str:            # ✏️ Build 1, step 1.2
    """Run the tool loop until Claude stops asking for tools. Return its final text."""
    client, tracer = new_session()
    tools = tool_list()
    messages = [
        {"role": "user", "content": f"PNR {pnr}, last name {last_name}. {message}"},
    ]

    response = client.messages.create(
        model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    answer = ""
    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1
    answer = text_of(response)
    return answer


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    # mcp_client.tools() starts the MCP server on first call and returns the
    # schemas for next_available_day and fare_rules; tool_results() already routes them.
    return build_tools() + mcp_client.tools() + EXTRA_TOOLS


# ──────────────────────────────────────────────────────────────────────────────
# Below this line: what Claude is told about each tool. Step 1.3.
# The functions these describe are written and correct, in support/tools.py.
# ──────────────────────────────────────────────────────────────────────────────
def build_tools() -> List[Dict[str, Any]]:                 # ✏️ Build 1, step 1.3
    """Anthropic-shaped schemas: name, description, input_schema. What Claude is
    told about each of the nine tools, and all it is ever told."""
    return [
        {
            "name": "lookup_booking",
            "description": (
                "Retrieve a Larkspur reservation from Altura by confirmation code (PNR) "
                "and the passenger's last name. Both are required to prevent a lookup on "
                "a guessed PNR. Returns fare family, loyalty tier, the segment that needs "
                "attention, and any group/partner/minor/SSR flags relevant to scope."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "last_name": {"type": "string"}},
                "required": ["pnr", "last_name"],
            },
        },
        {
            "name": "get_flight_status",
            "description": (
                "Look up a Larkspur or Larkspur Link flight's current OpsFeed status for "
                "one local date: status, delay minutes, and cause. Use this before telling "
                "a customer anything about a flight's timing; never state it from memory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "flight_no": {"type": "string"},
                    "date": {"type": "string", "description": "Local flight date in YYYY-MM-DD format, e.g. 2025-05-08"},
                },
                "required": ["flight_no", "date"],
            },
        },
        {
            "name": "search_alternatives",
            "description": "Search for alternative flight options for a booking. Call this after "
            "check_policy, once you know what the customer is entitled to, and pass "
            "the booking's pnr. Returns a list of available options, each with an "
            "option_id. To reserve one, pass its option_id to hold_seat. Don't "
            "invent ids; only use ones this tool returned.",
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}},
                "required": ["pnr"],
            },
        },
        {
            "name": "check_policy",
            "description": (
                "Resolve what Larkspur owes this customer for the disruption: rebooking "
                "waiver, refund path, meal/hotel/ground care, goodwill eligibility and cap, "
                "and any escalation triggers. cause_code, delay_minutes and status describe "
                "what get_flight_status told you; fare_family, loyalty_tier and whether this "
                "is overnight are looked up from the booking, not asked of you. Every "
                "response carries a policy_row_id. Cite it if you reference this decision "
                "again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"},
                    "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"]},
                    "delay_minutes": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"]},
                    "wait_minutes_for_alternative": {"type": "integer"},
                    "chosen_option_id": {"type": "string"},
                },
                "required": ["pnr", "cause_code", "delay_minutes", "status"],
            },
        },
        {
            "name": "hold_seat",
            "description": "Place a 15-minute hold on one alternative returned by search_alternatives. "
            "Pass that option's option_id exactly as search_alternatives gave it, plus "
            "the booking's pnr; never make up an id. Reversible: the hold simply "
            "expires if unused. Returns a hold_id, which confirm_rebooking needs to "
            "finalize the booking (that step also needs the customer's own "
            "Confirm-click).",
            "input_schema": {
                "type": "object",
                "properties": {"option_id": {"type": "string"}, "pnr": {"type": "string"}},
                "required": ["option_id", "pnr"],
            },
        },
        {
            "name": "confirm_rebooking",
            "description": (
                "Finalize a held seat. Irreversible. Requires a confirmation_token that "
                "only the customer's own Confirm-click can produce. You cannot supply it "
                "yourself, and 'the customer said yes' in chat does not substitute for it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}, "confirmation_token": {"type": "string"}},
                "required": ["hold_id", "confirmation_token"],
            },
        },
        {
            "name": "issue_voucher",
            "description": (
                "Issue a meal, ground, hotel, or goodwill voucher. Auto-approves within the "
                "policy's threshold for that type; above it, returns a pending status for a "
                "human. It does not fail. Always pass the policy_row_id that made it eligible."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_type": {"type": "string", "enum": ["meal", "ground", "hotel", "goodwill"]},
                    "amount_usd": {"type": "number"},
                    "pnr": {"type": "string"},
                    "policy_row_id": {"type": "string"},
                },
                "required": ["voucher_type", "amount_usd", "pnr", "policy_row_id"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand this conversation to a human, with your reasoning attached. Use for "
                "groups, partner segments, unaccompanied minors, refunds, or anything else "
                "out of scope. This is the correct outcome for those cases, not a failure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"}, "reason": {"type": "string"},
                    "summary_for_human": {"type": "string"}, "queue": {"type": "string"},
                },
                "required": ["pnr", "reason", "summary_for_human"],
            },
        },
        {
            "name": "send_confirmation",
            "description": "Send the customer a written confirmation of what was just done. Benign.",
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "message": {"type": "string"}},
                "required": ["pnr", "message"],
            },
        },
    ]
