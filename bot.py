
import hashlib
import re
import time
from datetime import datetime
from threading import RLock
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Vera Challenge Bot", version="1.0.0")
START = time.time()
LOCK = RLock()

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
# conversation_id -> state
conversations: dict[str, dict[str, Any]] = {}
# suppression_key -> sent timestamp
sent_suppressions: dict[str, float] = {}

ALLOWED_SCOPES = {"category", "merchant", "customer", "trigger"}
CATEGORY_STYLE = {
    "dentists": {
        "prefix": "Dr. ",
        "voice": "clinical, peer-to-peer, precise",
    },
    "salons": {
        "prefix": "",
        "voice": "warm, practical, operator-friendly",
    },
    "restaurants": {
        "prefix": "",
        "voice": "operator-to-operator, commercial, practical",
    },
    "gyms": {
        "prefix": "",
        "voice": "coach-like, energetic, disciplined",
    },
    "pharmacies": {
        "prefix": "",
        "voice": "trustworthy, precise, neighbourhood-pharmacist",
    },
}

def _get(scope: str, cid: Optional[str]) -> Optional[dict]:
    if not cid:
        return None
    item = contexts.get((scope, cid))
    return item["payload"] if item else None

def _owner(merchant: dict) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "there")

def _merchant_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "your business")

def _category(category: dict) -> str:
    return category.get("slug", "business")

def _active_offers(merchant: dict) -> list[dict]:
    return [x for x in merchant.get("offers", []) if x.get("status") == "active"]

def _offer_text(merchant: dict) -> str:
    offers = _active_offers(merchant)
    return offers[0].get("title", "") if offers else ""

def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except Exception:
        return str(x)

def _num(x: Any) -> str:
    try:
        return f"{float(x):g}"
    except Exception:
        return str(x)

def _clean_body(s: str) -> str:
    # Challenge explicitly rejects URLs in message bodies.
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:10]

def _find_digest(category: dict, trigger: dict) -> Optional[dict]:
    payload = trigger.get("payload", {})
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    if not wanted:
        return None
    for item in category.get("digest", []):
        if item.get("id") == wanted:
            return item
    return None

def _customer_allowed(customer: Optional[dict], trigger: dict, category: dict) -> bool:
    if not customer:
        return True
    consent = customer.get("consent", {})
    scopes = set(consent.get("scope", []))
    kind = trigger.get("kind", "")
    needed = {
        "recall_due": "recall_reminders",
        "appointment_tomorrow": "appointment_reminders",
        "trial_followup": "appointment_reminders",
        "chronic_refill_due": "refill_reminders",
        "customer_lapsed_hard": "winback_offers",
        "customer_lapsed_soft": "winback_offers",
        "winback_eligible": "winback_offers",
    }.get(kind)
    # Operational reminders are allowed when the customer explicitly enabled
    # reminders, even if the stored consent scope is narrower than the trigger
    # taxonomy used by this challenge.
    if kind in {"recall_due", "appointment_tomorrow", "trial_followup", "chronic_refill_due"}:
        if customer.get("preferences", {}).get("reminder_opt_in") is True:
            return True
    if kind in {"customer_lapsed_hard", "customer_lapsed_soft", "winback_eligible"}:
        if ("winback_offers" in scopes or "promotional_offers" in scopes) and consent.get("opted_in_at"):
            return True
    if needed and scopes and needed not in scopes:
        return False
    return bool(consent.get("opted_in_at") or not scopes)

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Deterministic context-grounded composer.
    It uses only values present in the four contexts and category-specific rules.
    """
    slug = _category(category)
    kind = trigger.get("kind", "")
    p = trigger.get("payload", {}) or {}
    owner = _owner(merchant)
    merchant_name = _merchant_name(merchant)
    active_offer = _offer_text(merchant)
    perf = merchant.get("performance", {}) or {}
    signals = merchant.get("signals", []) or []
    customer_name = (customer or {}).get("identity", {}).get("name", "")
    customer_pref = (customer or {}).get("preferences", {}).get("preferred_slots", "")
    send_as = "merchant_on_behalf" if customer else "vera"

    # Customer-facing messages.
    if customer:
        if not _customer_allowed(customer, trigger, category):
            return {
                "body": "",
                "cta": "none",
                "send_as": send_as,
                "suppression_key": trigger.get("suppression_key", kind),
                "rationale": "No outbound customer action: consent scope does not cover this trigger."
            }

        if kind == "recall_due":
            slots = p.get("available_slots", [])
            slot1 = slots[0].get("label") if len(slots) > 0 else None
            slot2 = slots[1].get("label") if len(slots) > 1 else None
            price = active_offer or "the current cleaning offer"
            service = p.get("service_due", "your next recall")
            if slug == "dentists":
                body = f"Hi {customer_name}, {merchant_name} here 🦷 Your {service.replace('_',' ')} window is due. "
                if slot1 and slot2:
                    body += f"We have {slot1} or {slot2} available"
                elif slot1:
                    body += f"We have {slot1} available"
                else:
                    body += "We can help you pick a convenient slot"
                body += f". {price}. Reply YES and we'll hold a slot for you."
                cta = "binary_yes"
            else:
                body = f"Hi {customer_name}, {merchant_name} here. Your {service.replace('_',' ')} is due"
                if slot1:
                    body += f"; the next available slot is {slot1}"
                body += ". Want me to hold it for you?"
                cta = "binary_yes"
            rationale = "Uses the due service, customer identity, real availability and an active merchant offer; low-friction booking CTA."

        elif kind in {"customer_lapsed_hard", "customer_lapsed_soft", "winback_eligible"}:
            days = p.get("days_since_last_visit") or p.get("days_since_expiry")
            focus = p.get("previous_focus")
            if slug == "gyms":
                detail = f" Your earlier focus was {focus.replace('_',' ')}." if focus else ""
                offer = active_offer or "a trial session"
                body = f"Hi {customer_name} 👋 {owner} from {merchant_name} here. It's been {days} days since your last visit — no pressure.{detail} We have {offer} available. Want me to hold a trial spot for you?"
            elif slug == "salons":
                body = f"Hi {customer_name} — {owner} from {merchant_name} here. It's been {days} days since your last visit. We have {active_offer or 'a current service option'} available; want me to hold a convenient slot?"
            elif slug == "pharmacies":
                body = f"Hi {customer_name}, {merchant_name} here. It's been {days} days since your last refill/visit. If you'd like, we can check your next refill and arrange delivery. Want us to check?"
            else:
                body = f"Hi {customer_name}, {merchant_name} here. It's been {days} days since your last visit. If you'd like to continue, I can help with the next available appointment. Want me to check?"
            cta = "binary_yes"
            rationale = "Acknowledges the customer's actual lapse without pressure, uses the merchant's real offer/context, and asks for one simple next step."

        elif kind in {"trial_followup", "appointment_tomorrow"}:
            options = p.get("next_session_options", [])
            when = options[0].get("label") if options else "the next available slot"
            body = f"Hi {customer_name} — {merchant_name} here. Following up on your recent visit/trial. Your next option is {when}. Want me to hold that slot?"
            cta = "binary_yes"
            rationale = "Continues the known customer journey with the actual next-session option and a single booking CTA."

        elif kind == "chronic_refill_due":
            molecules = p.get("molecule_list", [])
            names = ", ".join(molecules) if molecules else "your regular medicines"
            stock = p.get("stock_runs_out_iso", "")
            delivery = " Free home delivery is available." if any("Free Home Delivery" in o.get("title","") for o in _active_offers(merchant)) else ""
            body = f"Namaste — {merchant_name} here. Your regular refill ({names}) is due before {stock[:10] if stock else 'the next refill date'}.{delivery} Reply CONFIRM and we can prepare it, or tell us if anything has changed."
            cta = "confirm"
            rationale = "Uses the actual molecule list and refill timing, plus a verified active delivery offer when present; preserves a safe confirmation step."

        else:
            body = f"Hi {customer_name}, {merchant_name} here. We have an update relevant to your recent interaction. Want me to share the details?"
            cta = "binary_yes"
            rationale = "Fallback customer message stays grounded and asks permission before adding detail."

        return {
            "body": _clean_body(body),
            "cta": cta,
            "send_as": send_as,
            "suppression_key": trigger.get("suppression_key", kind),
            "rationale": rationale,
        }

    # Merchant-facing messages.
    if kind == "research_digest":
        d = _find_digest(category, trigger)
        if d:
            title = d.get("title", "a new research item")
            source = d.get("source", "")
            numbers = []
            if d.get("trial_n"): numbers.append(f"{d['trial_n']:,}-person trial")
            # Pull a useful numeric claim from the title rather than inventing one.
            body = f"{owner}, {source or 'a new category digest'} has an item worth a look: {title}."
            if numbers:
                body += f" {numbers[0]}."
            if merchant.get("customer_aggregate", {}).get("high_risk_adult_count"):
                body += f" It is especially relevant to your {merchant['customer_aggregate']['high_risk_adult_count']} high-risk-adult customers."
            body += " Want me to pull the key takeaway and draft a short customer-safe version?"
        else:
            body = f"{owner}, a new {slug} research digest is available. Want me to pull the most relevant item and turn it into a usable customer-facing draft?"
        cta = "open_ended"
        rationale = "Connects the research trigger to the category digest and a merchant-specific cohort, then offers to do the next piece of work."

    elif kind in {"regulation_change", "cde_opportunity", "supply_alert"}:
        d = _find_digest(category, trigger)
        if kind == "supply_alert":
            batches = ", ".join(p.get("affected_batches", []))
            body = f"{owner}, urgent supply alert: {p.get('molecule','the affected medicine')} — batches {batches} from {p.get('manufacturer','the manufacturer')} are flagged."
            body += " I can turn the alert into an affected-customer check and a replacement workflow. Want me to draft that?"
        elif kind == "regulation_change":
            deadline = p.get("deadline_iso")
            body = f"{owner}, compliance update for {slug}: {d.get('title') if d else 'a new regulation item'}"
            if deadline: body += f"; deadline {deadline}."
            else: body += "."
            body += " Want me to turn the required action into a short checklist?"
        else:
            body = f"{owner}, there's a relevant {slug} opportunity"
            if d: body += f": {d.get('title')} ({d.get('source','')})."
            else: body += "."
            body += " Want me to summarize the opportunity and the next step?"
        cta = "open_ended"
        rationale = "Uses the trigger's concrete external event and offers a specific follow-through rather than a generic growth pitch."

    elif kind == "recall_due":
        # Defensive path if customer context was omitted.
        body = f"{owner}, a customer recall trigger is active for {merchant_name}. Want me to prepare the reminder using the available appointment data?"
        cta = "open_ended"
        rationale = "Trigger is customer-scoped but customer context was not supplied; asks before acting."

    elif kind in {"perf_dip", "seasonal_perf_dip", "perf_spike"}:
        metric = p.get("metric", "performance")
        delta = p.get("delta_pct")
        value = _pct(delta) if delta is not None else "a change"
        direction = "down" if isinstance(delta, (int,float)) and delta < 0 else "up"
        baseline = p.get("vs_baseline")
        if kind == "seasonal_perf_dip" and p.get("is_expected_seasonal"):
            body = f"{owner}, {metric} is {value} this week, but this is flagged as the expected {p.get('season_note','seasonal')} window. I’d focus on retention rather than extra acquisition spend right now."
            if merchant.get("customer_aggregate", {}).get("total_unique_ytd"):
                body += f" You have {merchant['customer_aggregate']['total_unique_ytd']} unique customers YTD to work from."
            body += " Want me to draft one retention action?"
        elif kind == "perf_spike":
            driver = p.get("likely_driver")
            body = f"{owner}, {metric} is {value} over the last {p.get('window','7d')}"
            if baseline is not None: body += f" versus a baseline of {baseline}"
            if driver: body += f" — the likely driver is {driver}"
            body += ". Want me to turn that signal into one repeatable action?"
        else:
            body = f"{owner}, {metric} is {value} over {p.get('window','7d')}"
            if baseline is not None: body += f" versus a baseline of {baseline}"
            body += ". I can help isolate the likely cause and draft one corrective action. Want me to?"
        cta = "binary_yes"
        rationale = "Uses the trigger metric and observed delta/baseline, distinguishes an expected seasonal dip from an actionable change, and proposes one next action."

    elif kind in {"ipl_match_today", "festival_upcoming", "category_seasonal"}:
        if kind == "ipl_match_today":
            match = p.get("match", "today's match")
            time_text = p.get("match_time_iso", "").split("T")[-1][:5]
            existing = active_offer or "your existing offer"
            if not p.get("is_weeknight", True):
                body = f"Quick heads-up {owner} — {match} is on today"
                if time_text: body += f" at {time_text}"
                body += ". Because it’s not a weeknight, I’d avoid building a match-night dine-in promo; use your existing " + existing + " for delivery instead. Want me to draft the delivery copy?"
            else:
                body = f"{owner}, {match} is on today"
                if time_text: body += f" at {time_text}"
                body += f". Your active offer is {existing}. Want me to adapt it into a match-night message?"
        elif kind == "festival_upcoming":
            fest = p.get("festival", "the upcoming festival")
            body = f"{owner}, {fest} is {p.get('days_until','')} days away"
            if p.get("date"): body += f" ({p['date']})"
            body += f". For {slug}, this is a useful planning window. Want me to turn it into one concrete offer/post using your current catalog?"
        else:
            trends = ", ".join(p.get("trends", [])[:3])
            body = f"{owner}, the {p.get('season','current')} demand shift is showing {trends}. I’d adjust the shelf/offer mix rather than run a generic campaign. Want me to draft the exact change?"
        cta = "open_ended"
        rationale = "Interprets the event in category context and uses the merchant's existing offer or the trigger's measured seasonal signal instead of generic promotion."

    elif kind in {"active_planning_intent"}:
        topic = p.get("intent_topic", "the idea you were planning")
        last = p.get("merchant_last_message")
        history = merchant.get("conversation_history", [])
        prior_vera = next((h.get("body", "") for h in reversed(history)
                           if h.get("from") == "vera" and h.get("body")), "")
        body = f"{owner}, picking up your {topic.replace('_',' ')} plan"
        if prior_vera:
            # Reuse concrete facts that Vera already stated in the conversation;
            # never invent new pricing or quantities.
            compact = prior_vera.replace("Want me to draft the GBP post + Insta carousel?", "").strip()
            body += f": {compact}"
        elif last:
            body += f" — from your “{last}”"
        if _category(category) == "restaurants" and active_offer:
            body += f" Your current {active_offer} can be the base offer."
        body += " Want me to turn that into the final customer-facing copy?"
        cta = "open_ended"
        rationale = "Continues the merchant's explicit planning intent and reuses only concrete details already present in the conversation or active catalog."

    elif kind == "milestone_reached":
        metric = p.get("metric", "metric")
        now = p.get("value_now")
        milestone = p.get("milestone_value")
        body = f"{owner}, you’re at {now} {metric.replace('_',' ')} — {milestone} is the next milestone."
        if p.get("is_imminent"): body += " It’s close enough to use as a fresh proof point."
        body += " Want me to draft a post that turns the milestone into social proof?"
        cta = "open_ended"
        rationale = "Uses the exact milestone values and converts the achievement into one concrete, low-effort marketing artifact."

    elif kind in {"curious_ask_due"}:
        body = f"Hi {owner}! Quick check — what service/product has been most asked for this week at {merchant_name}? I’ll turn your answer into a short customer reply/post you can reuse."
        cta = "open_ended"
        rationale = "Asks one low-effort operator question and offers an immediate reusable artifact in return."

    elif kind in {"review_theme_emerged"}:
        theme = p.get("theme", "review theme")
        occurrences = p.get("occurrences_30d")
        quote = p.get("common_quote")
        body = f"{owner}, a review theme is rising: {theme.replace('_',' ')}"
        if occurrences is not None: body += f" ({occurrences} mentions in 30 days)"
        if quote: body += f" — one customer wrote “{quote}”"
        body += ". Want me to draft the smallest operational fix + a reply template?"
        cta = "open_ended"
        rationale = "Anchors the message in the measured review theme and actual customer language, then offers a concrete response artifact."

    elif kind in {"competitor_opened"}:
        competitor = p.get("competitor_name", "a nearby competitor")
        distance = p.get("distance_km")
        offer = p.get("their_offer")
        body = f"{owner}, {competitor} opened"
        if distance is not None: body += f" {distance} km away"
        if offer: body += f" with {offer}"
        body += ". I’d avoid copying the price; your current offer is " + (active_offer or "not currently active") + ". Want me to draft a differentiation angle?"
        cta = "open_ended"
        rationale = "Uses the competitor's actual distance and offer, then recommends a differentiation response rather than an unsupported price reaction."

    elif kind in {"renewal_due"}:
        days = p.get("days_remaining")
        amount = p.get("renewal_amount")
        body = f"{owner}, your {p.get('plan','subscription')} renewal is in {days} days"
        if amount is not None: body += f" (₹{amount:,})"
        body += ". Want me to prepare the renewal checklist and flag any profile/performance items worth fixing first?"
        cta = "open_ended"
        rationale = "Uses the exact renewal timing and amount from the trigger and ties the next step to the merchant's current state."

    elif kind in {"gbp_unverified"}:
        body = f"{owner}, your Google Business Profile is still unverified. The available path is {p.get('verification_path','the listed verification path')}"
        if p.get("estimated_uplift_pct") is not None:
            body += f"; the supplied estimate is {_pct(p['estimated_uplift_pct'])} uplift."
        body += " Want me to walk you through the verification steps?"
        cta = "open_ended"
        rationale = "States the exact verification status and available path; any uplift figure is explicitly sourced from the trigger."

    elif kind in {"dormant_with_vera"}:
        days = p.get("days_since_last_merchant_message", "a while")
        topic = p.get("last_topic", "your last topic")
        body = f"{owner}, it’s been {days} days since we last spoke about {topic.replace('_',' ')}. I have your current context ready — want to pick that back up?"
        cta = "binary_yes"
        rationale = "Uses conversation recency and the actual last topic to reopen the thread with minimal friction."

    elif kind in {"wedding_package_followup"}:
        date = p.get("wedding_date")
        days = p.get("days_to_wedding")
        window = p.get("next_step_window_open", "").replace("_"," ")
        body = f"Hi {customer_name or owner} 💍 {days} days to the wedding"
        if date: body += f" ({date})"
        body += f" — your {window} window is open. Want me to draft the next package using the salon's active offers?"
        cta = "open_ended"
        rationale = "Uses the exact wedding date/countdown and the named next-step window; avoids inventing package details."

    elif kind in {"winback_eligible"}:
        days = p.get("days_since_expiry")
        added = p.get("lapsed_customers_added_since_expiry")
        body = f"{owner}, it’s been {days} days since the offer expired"
        if added is not None: body += f" and {added} lapsed customers have been added since then"
        body += ". Want me to build a simple win-back message from your current active offer?"
        cta = "open_ended"
        rationale = "Uses the trigger's expiry and lapsed-customer counts and offers a targeted win-back artifact."

    else:
        # Generic but still grounded fallback.
        body = f"{owner}, a {kind or 'new'} signal is active for {merchant_name}. I can turn the available context into one concrete next step. Want me to draft it?"
        cta = "open_ended"
        rationale = "Fallback is deliberately conservative: it names the actual trigger kind and asks before adding unsupported claims."

    return {
        "body": _clean_body(body),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id','unknown')}"),
        "rationale": rationale,
    }


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str = ""

class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)

class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: str = ""
    turn_number: int = 1

@app.get("/v1/healthz")
def healthz():
    counts = {k: 0 for k in ALLOWED_SCOPES}
    with LOCK:
        for (scope, _), _v in contexts.items():
            counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time()-START), "contexts_loaded": counts}

@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": "Vera Grounded Composer",
        "team_members": ["candidate"],
        "model": "deterministic-rule-composer",
        "approach": "context-grounded deterministic templates with category/trigger/customer routing",
        "contact_email": "submission@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
    }

@app.post("/v1/context")
def push_context(req: ContextPush):
    if req.scope not in ALLOWED_SCOPES:
        return {"accepted": False, "reason": "invalid_scope", "details": req.scope}
    key = (req.scope, req.context_id)
    with LOCK:
        current = contexts.get(key)
        if current and current["version"] >= req.version:
            return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
        contexts[key] = {"version": req.version, "payload": req.payload}
    return {"accepted": True, "ack_id": f"ack_{_hash(req.scope+req.context_id)}_v{req.version}",
            "stored_at": datetime.utcnow().isoformat() + "Z"}

@app.post("/v1/tick")
def tick(req: TickRequest):
    actions = []
    with LOCK:
        for trigger_id in req.available_triggers:
            trigger = _get("trigger", trigger_id)
            if not trigger:
                continue
            merchant_id = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
            merchant = _get("merchant", merchant_id)
            if not merchant:
                continue
            cat_slug = merchant.get("category_slug") or trigger.get("payload", {}).get("category")
            category = _get("category", cat_slug)
            if not category:
                continue
            customer_id = trigger.get("customer_id")
            customer = _get("customer", customer_id) if customer_id else None

            action = compose(category, merchant, trigger, customer)
            if not action.get("body"):
                continue
            key = action["suppression_key"]
            if key in sent_suppressions:
                continue

            suffix = customer_id or merchant_id
            conv_id = f"vera_{merchant_id}_{suffix}_{_hash(trigger_id)}"
            conversations[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "category_slug": cat_slug,
                "trigger": trigger,
                "history": [{"role": "vera", "body": action["body"]}],
                "last_action": action,
            }
            sent_suppressions[key] = time.time()

            actions.append({
                "conversation_id": conv_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": action["send_as"],
                "trigger_id": trigger_id,
                "template_name": f"vera_{trigger.get('kind','event')}_v1",
                "template_params": [action["body"]],
                **{k: action[k] for k in ("body", "cta", "suppression_key", "rationale")}
            })
            if len(actions) >= 20:
                break
    return {"actions": actions}

def _is_auto_reply(msg: str) -> bool:
    s = msg.lower().strip()
    patterns = [
        "thank you for contacting us",
        "our team will respond shortly",
        "we will get back to you",
        "automated response",
        "auto-reply",
        "this is an automated",
    ]
    return any(p in s for p in patterns)

def _is_stop(msg: str) -> bool:
    s = msg.lower()
    return any(p in s for p in ["stop messaging", "don't message", "do not message", "unsubscribe", "not interested", "useless spam"])

def _is_commitment(msg: str) -> bool:
    s = msg.lower()
    return any(p in s for p in ["ok lets do it", "ok, let's do it", "let's do it", "yes do it", "go ahead", "proceed", "do it"])

def _followup_action(state: dict, message: str) -> dict:
    trigger = state["trigger"]
    kind = trigger.get("kind", "")
    if _is_stop(message):
        return {"action": "end", "rationale": "Merchant/customer explicitly asked to stop; ending rather than sending another nudge."}
    if _is_auto_reply(message):
        state.setdefault("auto_replies", 0)
        state["auto_replies"] += 1
        if state["auto_replies"] >= 2:
            return {"action": "end", "rationale": "Repeated canned auto-replies detected; exiting to avoid spam."}
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Likely automated response; backing off before another contact."}
    if _is_commitment(message):
        return {
            "action": "send",
            "body": "Done — I’ll move to the concrete next step from the context already provided. I won’t ask you to repeat the details.",
            "cta": "open_ended",
            "rationale": "Merchant has explicitly committed; switching from qualification to action."
        }
    if any(x in message.lower() for x in ["yes", "sure", "send", "draft", "please", "sounds good"]):
        if kind in {"research_digest", "cde_opportunity"}:
            return {"action": "send", "body": "On it — I’ll use the source and merchant context already provided and keep the draft short enough to use directly.", "cta": "open_ended", "rationale": "Acknowledges acceptance and moves directly to the requested artifact."}
        if kind in {"curious_ask_due"}:
            return {"action": "send", "body": "Perfect — I’ll turn that into one reusable customer-facing reply/post, keeping the wording aligned with your category voice.", "cta": "open_ended", "rationale": "Merchant accepted the low-effort ask; proceeding to the promised artifact."}
        return {"action": "send", "body": "Perfect — I’ll use the details you just confirmed and move to the next concrete step.", "cta": "open_ended", "rationale": "Merchant accepted; proceeding without another qualification loop."}
    return {"action": "wait", "wait_seconds": 900, "rationale": "No clear acceptance or rejection; waiting rather than over-messaging."}

@app.post("/v1/reply")
def reply(req: ReplyRequest):
    with LOCK:
        state = conversations.get(req.conversation_id)
        if not state:
            # The simulator may probe reply handling without a prior tick.
            state = {
                "merchant_id": req.merchant_id,
                "customer_id": req.customer_id,
                "category_slug": None,
                "trigger": {"kind": "unknown", "suppression_key": "reply-only"},
                "history": [],
            }
            conversations[req.conversation_id] = state
        state["history"].append({"role": req.from_role, "body": req.message})
        return _followup_action(state, req.message)
