"""Reachmark inbound lead qualification: deterministic scoring and auditable routing."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import jsonify, request, session


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value, limit=2000):
    return str(value or "").strip()[:limit]


def _score(lead, profile=None):
    """Transparent baseline scores. Missing evidence never earns positive points."""
    profile = profile or {}
    fit = 0
    intent = 0
    reasons = []
    missing = []
    website = _clean(lead.get("website"), 1000)
    email = _clean(lead.get("email"), 250)
    company = _clean(lead.get("company") or lead.get("name"), 200)
    pain = _clean(lead.get("pain_point"), 1000)
    goal = _clean(lead.get("goal"), 1000)
    budget = _clean(lead.get("budget"), 200)
    timeline = _clean(lead.get("timeline"), 200)
    authority = _clean(lead.get("authority"), 50)

    industry = _clean(lead.get("industry"), 100)
    target_industries = profile.get("industries") or []
    if industry and (not target_industries or industry.lower() in {str(x).lower() for x in target_industries}):
        fit += 20
        reasons.append("Industry matches the configured target or no industry restriction is set.")
    else:
        missing.append("industry")
    if website:
        parsed = urlparse(website if "://" in website else "https://" + website)
        if parsed.hostname and "." in parsed.hostname and not parsed.hostname.endswith("."):
            fit += 20
            reasons.append("A business website was supplied for review.")
        else:
            missing.append("valid_website")
    else:
        missing.append("website")
    if company:
        fit += 15
        reasons.append("Company or business name is available.")
    else:
        missing.append("company")
    if email:
        fit += 10
        reasons.append("An email contact was supplied.")
    else:
        missing.append("email")
    if authority == "decision_maker":
        fit += 20
        reasons.append("Lead identifies as a decision-maker.")
    elif authority == "influencer":
        fit += 10
        reasons.append("Lead identifies as an influencer.")
    else:
        missing.append("authority")
    if pain:
        intent += 25
        reasons.append("A specific pain point was provided.")
    else:
        missing.append("pain_point")
    if goal:
        intent += 15
        reasons.append("A desired outcome was provided.")
    else:
        missing.append("goal")
    if budget:
        intent += 20
        reasons.append("Budget information was provided.")
    else:
        missing.append("budget")
    if timeline:
        intent += 20
        reasons.append("A timeline was provided.")
    else:
        missing.append("timeline")
    if _clean(lead.get("preferred_next_step"), 100).lower() in {"book", "book a call", "call", "meeting", "demo"}:
        intent += 20
        reasons.append("Lead requested a call or meeting.")

    fit = min(100, fit)
    intent = min(100, intent)
    total = round(fit * 0.45 + intent * 0.55)
    tier = "A" if total >= 80 else "B" if total >= 60 else "C" if total >= 40 else "D"
    if tier == "A":
        action, stage = "assign_sales_rep", "qualified"
    elif tier == "B":
        action, stage = ("ask_question" if missing else "book_call"), ("qualifying" if missing else "qualified")
    elif tier == "C":
        action, stage = "nurture", "nurture"
    else:
        action, stage = ("ask_question" if missing and (fit >= 25 or intent >= 25) else "review_fit"), ("qualifying" if missing and (fit >= 25 or intent >= 25) else "disqualified")
    questions = {
        "website": "What is your business website?",
        "industry": "Which industry does your business operate in?",
        "company": "What is your business name?",
        "email": "What is the best work email to reach you?",
        "authority": "Are you the person who makes the final decision on this project?",
        "pain_point": "What is the main challenge you want to solve?",
        "goal": "What result would you most like to achieve?",
        "budget": "What budget range have you set aside for this project?",
        "timeline": "When would you ideally like to get started?",
    }
    next_question = next((questions[x] for x in missing if x in questions), None)
    return {
        "fit_score": fit, "intent_score": intent, "total_score": total, "tier": tier,
        "stage": stage, "next_action": action, "missing_fields": missing,
        "qualification_reasons": reasons, "next_question": next_question,
        "summary": f"{company or 'Unknown business'} — {tier} tier, fit {fit}/100, intent {intent}/100.",
        "scoring_version": "baseline-v1",
    }


def register_lead_qualifier(app, db, log, add_lead):
    """Register endpoints on the existing Flask app and SQLite connection."""
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lead_qualifications (
          lead_id TEXT PRIMARY KEY, owner_user_id TEXT, company TEXT, industry TEXT,
          pain_point TEXT, goal TEXT, budget TEXT, timeline TEXT, authority TEXT,
          preferred_next_step TEXT, fit_score INTEGER NOT NULL DEFAULT 0,
          intent_score INTEGER NOT NULL DEFAULT 0, total_score INTEGER NOT NULL DEFAULT 0,
          tier TEXT NOT NULL DEFAULT 'D', stage TEXT NOT NULL DEFAULT 'new',
          next_action TEXT NOT NULL DEFAULT 'qualify', missing_fields TEXT NOT NULL DEFAULT '[]',
          qualification_reasons TEXT NOT NULL DEFAULT '[]', next_question TEXT,
          summary TEXT, enrichment TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL,
          updated TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lead_qualification_events (
          id TEXT PRIMARY KEY, lead_id TEXT NOT NULL, owner_user_id TEXT,
          event_type TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lead_conversations (
          id TEXT PRIMARY KEY, lead_id TEXT NOT NULL, owner_user_id TEXT,
          role TEXT NOT NULL CHECK(role IN ('user','agent','system')),
          channel TEXT NOT NULL DEFAULT 'website_form', content TEXT NOT NULL,
          created TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lq_owner_score ON lead_qualifications(owner_user_id, total_score DESC);
        CREATE INDEX IF NOT EXISTS idx_lqc_lead_created ON lead_conversations(lead_id, created);
        """)

    def owner_id():
        if session.get("owner"):
            return "owner"
        if session.get("client_id") and session.get("role") == "client":
            return str(session["client_id"])
        return None

    def owned(lead_id, owner):
        with db() as c:
            return c.execute(
                "SELECT q.*, l.name, l.email, l.phone, l.website FROM lead_qualifications q "
                "JOIN leads l ON l.id=q.lead_id WHERE q.lead_id=? AND q.owner_user_id IS ?",
                (lead_id, owner),
            ).fetchone()

    def record_event(c, lead_id, owner, event_type, payload):
        c.execute(
            "INSERT INTO lead_qualification_events(id,lead_id,owner_user_id,event_type,payload,created) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, lead_id, owner, event_type, json.dumps(payload, ensure_ascii=False), _now()),
        )

    @app.post("/api/qualifier/leads")
    def qualifier_capture():
        owner = owner_id()
        if not owner:
            return jsonify(error="Sign in to capture and manage leads."), 401
        body = request.get_json(silent=True) or {}
        fields = {key: _clean(body.get(key), limit) for key, limit in {
            "name": 200, "email": 250, "phone": 100, "website": 1000, "company": 200,
            "industry": 100, "pain_point": 1000, "goal": 1000, "budget": 200,
            "timeline": 200, "authority": 50, "preferred_next_step": 100,
        }.items()}
        if not fields["name"] and not fields["company"]:
            return jsonify(error="Provide a name or company name."), 400
        if fields["email"] and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", fields["email"]):
            return jsonify(error="Enter a valid email address."), 400
        fields["name"] = fields["name"] or fields["company"]
        fields["source_key"] = "qualifier:" + (fields["email"].lower() or
            (urlparse(fields["website"] if "://" in fields["website"] else "https://" + fields["website"]).hostname or "").lower() or
            (fields["name"].lower() + "|" + fields["phone"]))
        fields["source"] = "Lead Qualifier"
        fields["owner_user_id"] = owner
        try:
            with db() as c:
                duplicate = None
                if fields["email"]:
                    duplicate = c.execute(
                        "SELECT id FROM leads WHERE owner_user_id IS ? AND lower(email)=lower(?) AND email<>'' LIMIT 1",
                        (owner, fields["email"]),
                    ).fetchone()
                if not duplicate and fields["website"]:
                    host = (urlparse(fields["website"] if "://" in fields["website"] else "https://" + fields["website"]).hostname or "").lower()
                    if host:
                        duplicate = c.execute(
                            "SELECT id FROM leads WHERE owner_user_id IS ? AND lower(website) LIKE ? LIMIT 1",
                            (owner, "%" + host + "%"),
                        ).fetchone()
                if duplicate:
                    lead_id = duplicate["id"]
                    c.execute("UPDATE leads SET updated=? WHERE id=?", (_now(), lead_id))
                    record_event(c, lead_id, owner, "duplicate_submission", {"email": fields["email"], "website": fields["website"]})
                    return jsonify(ok=True, duplicate=True, lead_id=lead_id, message="Existing lead found; no duplicate created."), 200
            add_lead(fields)
            with db() as c:
                row = c.execute(
                    "SELECT id FROM leads WHERE owner_user_id IS ? AND source_key=? ORDER BY created DESC LIMIT 1",
                    (owner, fields["source_key"]),
                ).fetchone()
                if not row:
                    return jsonify(error="Lead could not be persisted."), 500
                lead_id = row["id"]
                score = _score(fields)
                stamp = _now()
                c.execute("""INSERT OR REPLACE INTO lead_qualifications
                    (lead_id,owner_user_id,company,industry,pain_point,goal,budget,timeline,authority,preferred_next_step,
                     fit_score,intent_score,total_score,tier,stage,next_action,missing_fields,qualification_reasons,
                     next_question,summary,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lead_id, owner, fields["company"] or fields["name"], fields["industry"], fields["pain_point"],
                     fields["goal"], fields["budget"], fields["timeline"], fields["authority"], fields["preferred_next_step"],
                     score["fit_score"], score["intent_score"], score["total_score"], score["tier"], score["stage"],
                     score["next_action"], json.dumps(score["missing_fields"]), json.dumps(score["qualification_reasons"]),
                     score["next_question"], score["summary"], stamp, stamp))
                record_event(c, lead_id, owner, "qualified", score)
                if score["next_question"]:
                    c.execute("INSERT INTO lead_conversations(id,lead_id,owner_user_id,role,channel,content,created) VALUES(?,?,?,?,?,?,?)",
                              (uuid.uuid4().hex, lead_id, owner, "agent", "website_form", score["next_question"], stamp))
            log("lead_qualifier", f"Qualified inbound lead {lead_id} ({score['tier']}, {score['total_score']}/100)")
            _notify_telegram(fields, score, lead_id)
            return jsonify(ok=True, lead_id=lead_id, duplicate=False, qualification=score), 201
        except Exception:
            app.logger.exception("Lead qualifier capture failed")
            return jsonify(error="Lead was not processed. Please try again."), 500

    def _notify_telegram(fields, score, lead_id):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
        if not token or not chat_id:
            return
        import requests
        text = (f"Reachmark lead: {fields.get('company') or fields.get('name') or 'Unknown'}\n"
                f"Score: {score['total_score']}/100 · Tier {score['tier']}\n"
                f"Fit: {score['fit_score']} · Intent: {score['intent_score']}\n"
                f"Next action: {score['next_action']}\n"
                f"Email: {fields.get('email') or 'Not provided'}\n"
                f"Website: {fields.get('website') or 'Not provided'}\n"
                f"Lead ID: {lead_id}")
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=8,
            )
            response.raise_for_status()
        except Exception:
            app.logger.exception("Telegram lead notification failed for %s", lead_id)
            with db() as c:
                record_event(c, lead_id, owner_id(), "notification_failed", {"channel": "telegram"})

    @app.get("/api/qualifier/leads")
    def qualifier_list():
        owner = owner_id()
        if not owner:
            return jsonify(error="Authentication required."), 401
        with db() as c:
            rows = c.execute(
                """SELECT q.*,l.name,l.email,l.phone,l.website,l.stage AS crm_stage,l.created AS lead_created
                   FROM lead_qualifications q JOIN leads l ON l.id=q.lead_id
                   WHERE q.owner_user_id IS ? ORDER BY q.total_score DESC,q.updated DESC LIMIT 200""",
                (owner,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["missing_fields"] = json.loads(item["missing_fields"] or "[]")
                item["qualification_reasons"] = json.loads(item["qualification_reasons"] or "[]")
                item["enrichment"] = json.loads(item["enrichment"] or "{}")
                result.append(item)
        return jsonify(leads=result)

    @app.get("/api/qualifier/leads/<lead_id>")
    def qualifier_detail(lead_id):
        owner = owner_id()
        if not owner:
            return jsonify(error="Authentication required."), 401
        row = owned(lead_id, owner)
        if not row:
            return jsonify(error="Lead not found."), 404
        with db() as c:
            messages = c.execute(
                "SELECT role,channel,content,created FROM lead_conversations WHERE lead_id=? AND owner_user_id=? ORDER BY created",
                (lead_id, owner),
            ).fetchall()
            events = c.execute(
                "SELECT event_type,payload,created FROM lead_qualification_events WHERE lead_id=? AND owner_user_id=? ORDER BY created DESC LIMIT 100",
                (lead_id, owner),
            ).fetchall()
        item = dict(row)
        item["missing_fields"] = json.loads(item["missing_fields"] or "[]")
        item["qualification_reasons"] = json.loads(item["qualification_reasons"] or "[]")
        item["conversation"] = [dict(x) for x in messages]
        item["events"] = [{**dict(x), "payload": json.loads(x["payload"])} for x in events]
        return jsonify(lead=item)

    @app.post("/api/qualifier/leads/<lead_id>/message")
    def qualifier_message(lead_id):
        owner = owner_id()
        if not owner:
            return jsonify(error="Authentication required."), 401
        row = owned(lead_id, owner)
        if not row:
            return jsonify(error="Lead not found."), 404
        body = request.get_json(silent=True) or {}
        message = _clean(body.get("message"), 4000)
        if not message:
            return jsonify(error="Message is required."), 400
        if len(message) < 2:
            return jsonify(error="Message is too short."), 400
        with db() as c:
            c.execute("INSERT INTO lead_conversations(id,lead_id,owner_user_id,role,channel,content,created) VALUES(?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex, lead_id, owner, "user", _clean(body.get("channel"), 50) or "dashboard", message, _now()))
            current = c.execute("SELECT * FROM lead_qualifications WHERE lead_id=? AND owner_user_id=?", (lead_id, owner)).fetchone()
            data = dict(current)
            for field in ("company","industry","pain_point","goal","budget","timeline","authority","preferred_next_step"):
                data[field] = data.get(field) or ""
            text = message.lower()
            if not data["pain_point"] and any(x in text for x in ("website","leads","customers","sales","conversion","marketing","traffic","booking")):
                data["pain_point"] = message
            elif not data["goal"]:
                data["goal"] = message
            score = _score(data)
            stamp = _now()
            c.execute("""UPDATE lead_qualifications SET pain_point=?,goal=?,fit_score=?,intent_score=?,total_score=?,
                tier=?,stage=?,next_action=?,missing_fields=?,qualification_reasons=?,next_question=?,summary=?,updated=?
                WHERE lead_id=? AND owner_user_id=?""",
                (data["pain_point"],data["goal"],score["fit_score"],score["intent_score"],score["total_score"],score["tier"],
                 score["stage"],score["next_action"],json.dumps(score["missing_fields"]),json.dumps(score["qualification_reasons"]),
                 score["next_question"],score["summary"],stamp,lead_id,owner))
            reply = score["next_question"] or ("Thanks — I have enough information to recommend the next step. Would you prefer a call or an email follow-up?")
            c.execute("INSERT INTO lead_conversations(id,lead_id,owner_user_id,role,channel,content,created) VALUES(?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex,lead_id,owner,"agent","dashboard",reply,stamp))
            record_event(c,lead_id,owner,"conversation_updated",{"score":score,"message_length":len(message)})
        return jsonify(reply=reply, qualification=score)

    @app.get("/api/qualifier/summary")
    def qualifier_summary():
        owner = owner_id()
        if not owner:
            return jsonify(error="Authentication required."), 401
        with db() as c:
            rows = c.execute(
                "SELECT tier,COUNT(*) AS n FROM lead_qualifications WHERE owner_user_id IS ? GROUP BY tier",
                (owner,),
            ).fetchall()
            stages = c.execute(
                "SELECT stage,COUNT(*) AS n FROM lead_qualifications WHERE owner_user_id IS ? GROUP BY stage",
                (owner,),
            ).fetchall()
        return jsonify(total=sum(r["n"] for r in rows), tiers={r["tier"]: r["n"] for r in rows},
                       stages={r["stage"]: r["n"] for r in stages})
