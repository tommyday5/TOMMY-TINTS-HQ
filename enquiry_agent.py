"""Safe, deterministic first-pass intake for future AI-assisted quoting.

No external model, customer storage, or autonomous publishing is enabled.
A future model can propose structured candidates, but must pass validation
and owner approval before pricing, booking, or catalogue publication.
"""
import re

SERVICES = {
    "window_tinting": ("Window Tinting", ("tint", "sun strip", "privacy glass")),
    "vehicle_wrapping": ("Vehicle Wrapping", ("wrap", "vinyl", "dechrome")),
    "bodywork_paint": ("Bodywork & Paint", ("dent", "scratch", "respray", "paint", "bumper", "bodywork", "scuff")),
    "wheel_refurbishment": ("Wheel Refurbishment", ("alloy", "wheel refurb", "wheels", "kerb rash")),
    "valeting_detailing": ("Valeting & Detailing", ("valet", "detail", "clean", "polish", "ceramic")),
    "leather_repair": ("Leather Repair", ("leather", "seat repair", "upholstery")),
}
NEW_SERVICE_HINTS = {
    "caliper_painting": ("Brake Caliper Painting", ("caliper", "calipers")),
    "splitter_installation": ("Splitter Installation", ("splitter",)),
    "diagnostics": ("Vehicle Diagnostics", ("diagnostic", "fault code", "warning light")),
}
QUESTION_MAP = {
    "window_tinting": "Which windows need tinting, and what is the vehicle make and model?",
    "vehicle_wrapping": "Is this a full wrap or individual panels, and what finish would you like?",
    "bodywork_paint": "Which panels are affected, and can you provide clear photos showing the damage?",
    "wheel_refurbishment": "How many wheels need work, and what finish and damage are involved?",
    "valeting_detailing": "What is the vehicle size and current condition, and do you need an interior or exterior service?",
    "leather_repair": "Which areas need repair, and can you provide photos?",
    "caliper_painting": "What vehicle is this for, and what colour and finish do you want?",
    "splitter_installation": "What vehicle is this for, and will you supply the splitter?",
    "diagnostics": "What symptoms or warning lights are present, and when do they occur?",
}
def analyse_enquiry(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    description = payload.get("description", "")
    if not isinstance(description, str) or not 3 <= len(description.strip()) <= 3000:
        raise ValueError("Description must contain 3 to 3000 characters")
    vehicle = payload.get("vehicle", {})
    if not isinstance(vehicle, dict):
        raise ValueError("Vehicle must be an object")
    clean_vehicle = {}
    for field in ("make", "model", "year", "registration"):
        value = vehicle.get(field, "")
        if not isinstance(value, (str, int)) or len(str(value)) > 80:
            raise ValueError("Invalid vehicle field: " + field)
        clean_vehicle[field] = str(value).strip()
    lower = description.lower()
    def matches(terms):
        return any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", lower) for t in terms)
    matched = [{"service_id": key, "name": name, "status": "existing", "requires_owner_approval": True}
               for key, (name, terms) in SERVICES.items() if matches(terms)]
    proposals = [{"service_id": key, "name": name, "status": "draft_proposal", "requires_owner_approval": True}
                 for key, (name, terms) in NEW_SERVICE_HINTS.items() if matches(terms)]
    ids = [x["service_id"] for x in matched + proposals]
    questions = []
    if not clean_vehicle["make"] or not clean_vehicle["model"]:
        questions.append("What is the vehicle make and model?")
    for key in ids:
        question = QUESTION_MAP[key]
        if question not in questions:
            questions.append(question)
    if not ids:
        questions.append("Could you describe the work required, including the affected parts and desired result?")
    return {
        "schema_version": "1.0", "analysis_mode": "rules_only_no_ai_model",
        "existing_services": matched, "new_service_proposals": proposals,
        "needs_clarification": True, "clarifying_questions": questions[:6],
        "pricing_status": "not_priced", "booking_status": "not_booked",
        "customer_description": description.strip(), "vehicle": clean_vehicle,
        "requires_owner_approval": True,
        "warnings": ["Classification is preliminary; keywords may be ambiguous.",
                     "No price, booking, or new service is approved automatically.",
                     "Hidden damage and unreported work require inspection and separate agreement."],
    }
