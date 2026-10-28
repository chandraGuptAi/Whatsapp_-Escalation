"""Source-linked seller enquiries, independently of AI table coverage.

Counts cover the saved report window, once per message/AWB/seller/group.
Unknown or mismatched people are visible but never counted as verified sellers.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from .aggregate import escalation_rows
from ..logistics.awb import extract_awbs
from ..logistics.classifier import classify
from ..logistics.tat import format_duration
from ..utils.ist import IST

HEADERS = ["Company", "Time", "Seller", "No. of Escalation Entries", "AWB", "Mile",
           "Category/Subcategory", "Order Status", "Priority", "Staff response",
           "Esc. Raised", "Revert TAT", "Revert Date", "Resolution Date"]


def awb_refs(text):
    # A bare ten-digit shipment must not disappear because it resembles a phone.
    # Explicit contact-labelled numbers are still excluded by the shared extractor.
    return extract_awbs(text, include_phone_candidates=True)


def directory(runtime):
    rows = runtime.db.read("SELECT payload FROM master_report_directory WHERE id=1")
    return json.loads(rows[0]["payload"]) if rows else {"employees": [], "sellers": [], "members": [], "shipments": []}


def normalized(name):
    return " ".join(str(name or "").casefold().split())


def identity(message, mappings):
    label = message.get("author_name") or message.get("author") or "Unknown"
    # The displayed composite ID is checked against authoritative master data.
    parsed = re.fullmatch(r"\s*(.*?)\s*-\s*(\d+)\s*-\s*(Seller|BIGSHIP|Employee)\s*", label, re.I)
    membership = next((m for m in mappings.get("members", [])
                       if m.get("senderId") == message.get("author") and m.get("groupId") == message["group_id"]), None)
    membership = membership or next((m for m in mappings.get("members", [])
                                    if m.get("senderId") == message.get("author") and not m.get("groupId")), {})
    candidates = []
    for kind, key, role in (("sellers", "sellerId", "seller"), ("employees", "employeeId", "employee")):
        identifier = parsed[2] if parsed else membership.get(key)
        for person in mappings.get(kind, []):
            if str(person["id"]) == identifier:
                candidates.append((person, role))
    if len(candidates) != 1:
        return dict(role="unknown", key=message.get("author") or label, label="❓ " + label)
    person, role = candidates[0]
    name = parsed[1] if parsed else label
    if normalized(name) != normalized(person["name"]) or (parsed and (parsed[3].lower() == "seller") != (role == "seller")):
        return dict(role="mismatch", key=person["id"], label="⚠️ " + label)
    return dict(role=role, key=person["id"], label=("🛒 " if role == "seller" else "👤 ") + label)


def stamp(value):
    return datetime.fromtimestamp(value, IST).strftime("%d-%m-%Y %H:%M:%S") if value is not None else ""


def report_time(value, pattern):
    return datetime.fromtimestamp(value, IST).strftime(pattern).lstrip("0") if value is not None else "-"


def staff_status(responses, awb):
    aliases = {"In Transit": "In-Transit", "Out For Delivery": "Out for Delivery",
               "Delivered": "Delivered", "Undelivered (Generic)": "Undelivered"}
    for event in reversed(responses):
        for line in reversed(re.split(r"[\n;]", event["message"].get("body", ""))):
            refs = {r["awb"] for r in awb_refs(line)}
            if refs and refs != {awb}:
                continue
            if not refs and not event["message"].get("quoted_wa_id"):
                continue
            if re.search(r"\b(?:will|should|please|pls|when|kab|request)\b|\?", line, re.I):
                continue
            status = aliases.get(classify(line)["status"])
            if status:
                return status
    return None


def confirms_resolution(body):
    """Seller receipt/resolution confirmation, never a bare acknowledgement."""
    if re.search(r"\b(?:not|never|nahi|nhi|ni|pending|please|pls|will|when|kab)\b|\?", body, re.I):
        return False
    return bool(re.search(r"\b(?:received (?:the )?(?:parcel|shipment|package)|(?:parcel|shipment|package) received|issue (?:is )?resolved|haan mil gaya|delivered.*thanks)\b", body, re.I))


def build_rows(messages, report, mappings):
    annotations = {}
    for row in escalation_rows(report.get("report", "")) or []:
        for ref in awb_refs(row.get("awb", "")):
            annotations.setdefault(ref["awb"], row)
    messages = sorted(messages, key=lambda m: (m["timestamp"], m["id"]))
    unique, seen = [], set()
    for message in messages:
        key = message.get("wa_id") or message["id"]
        if key not in seen:
            unique.append(message)
            seen.add(key)
    events, by_id, by_wa = [], {}, {}
    for message in unique:
        who = identity(message, mappings)
        refs = {r["awb"] for r in awb_refs(message.get("body", ""))}
        event = dict(message=message, who=who, refs=refs)
        events.append(event)
        by_id[message["id"]] = event
        if message.get("wa_id"):
            by_wa[message["wa_id"]] = event
    enquiries = defaultdict(list)
    replies = defaultdict(list)
    confirmations = defaultdict(list)
    for event in events:
        message, who = event["message"], event["who"]
        refs = set(event["refs"])
        quoted = by_wa.get(message.get("quoted_wa_id"))
        if not refs and quoted and (quoted["message"]["timestamp"], quoted["message"]["id"]) < (message["timestamp"], message["id"]):
            refs = quoted["refs"]
        event["refs"] = refs
        if who["role"] == "employee":
            for awb in refs:
                replies[awb].append(event)
        elif who["role"] == "seller" and confirms_resolution(message.get("body", "")):
            for awb in refs:
                confirmations[(who["key"], awb)].append(event)
        elif who["role"] == "seller" and re.fullmatch(
                r"\s*(?:ok(?:ay)?|thanks|thank you|thx|noted|ji|👍|🙏)[\s,.!🙏👍]*",
                message.get("body", ""), re.I):
            continue  # An acknowledgement of a quoted reply is not another enquiry.
        else:
            for awb in refs:
                enquiries[(who["role"], who["key"], awb)].append(event)
    output = []
    for (role, _, awb), raised in enquiries.items():
        first = raised[0]
        source = first["message"]
        responses = [e for e in replies[awb] if (e["message"]["timestamp"], e["message"]["id"]) > (source["timestamp"], source["id"])
                     and e["message"].get("author") != source.get("author")]
        # A quote targets its own seller; an explicit AWB response without a quote
        # is shared only when there is one seller conversation for that AWB.
        seller_threads = sum(key[2] == awb for key in enquiries)
        responses = [e for e in responses if
                     e["message"].get("quoted_wa_id") in {r["message"].get("wa_id") for r in raised if r["message"].get("wa_id")}
                     or (not e["message"].get("quoted_wa_id") and seller_threads == 1)]
        response = responses[0]["message"] if responses else None
        annotation = annotations.get(awb, {})
        observed = classify(source.get("body", ""))
        mile = annotation.get("mile") or observed["mile"]
        mile = {"first": "First Mile", "last": "Last Mile"}.get(mile.lower(), mile)
        shipments = [s for s in mappings.get("shipments", []) if s["groupId"] == source["group_id"]
                     and s["awb"].upper() == awb and s["observedAt"] <= report["period_end"]]
        shipment = max(shipments, key=lambda s: s["observedAt"], default={})
        closure = by_id.get(shipment.get("closureMessageId"))
        closure_message = closure["message"] if closure else None
        if not (closure and closure["who"]["role"] == "employee" and awb in closure["refs"]
                and any(e["message"]["id"] == closure_message["id"] for e in responses)
                and (closure_message["timestamp"], closure_message["id"]) >
                    (raised[-1]["message"]["timestamp"], raised[-1]["message"]["id"])):
            closure_message = None
        for confirmation in confirmations[(first["who"]["key"], awb)]:
            candidate = confirmation["message"]
            if response and (candidate["timestamp"], candidate["id"]) > max(
                    (response["timestamp"], response["id"]),
                    (raised[-1]["message"]["timestamp"], raised[-1]["message"]["id"])):
                if closure_message is None or candidate["timestamp"] < closure_message["timestamp"]:
                    closure_message = candidate
                break
        order_status = shipment.get("status") or staff_status(responses, awb)
        values = [report.get("group_name") or source["group_id"], stamp(source["timestamp"]), first["who"]["label"],
                  len(raised) if role == "seller" else None, awb, mile,
                  annotation.get("category/subcategory") or observed["category"], order_status or "Unknown",
                  annotation.get("priority", "normal"), annotation.get("staff response") or response.get("body", "") if response else "none",
                  report_time(source["timestamp"], "%I:%M %p").lower(), format_duration(response["timestamp"] - source["timestamp"]) if response else "-",
                  datetime.fromtimestamp(response["timestamp"], IST).strftime("%d-%b-%Y") if response else "-",
                  datetime.fromtimestamp(closure_message["timestamp"], IST).strftime("%d-%b-%Y") if closure_message else "-"]
        output.append(dict(values=values, groupId=source["group_id"], awb=awb,
                           raisedAt=source["timestamp"], responseAt=response["timestamp"] if response else None,
                           resolutionAt=closure_message["timestamp"] if closure_message else None,
                           orderStatusSource="ERP mapping" if shipment else "Staff message (unverified)" if order_status else "Unknown",
                           identityStatus=role, sourceMessageIds=[e["message"]["id"] for e in raised],
                           responseMessageId=response["id"] if response else None,
                           resolutionMessageId=closure_message["id"] if closure_message else None))
    return output


def report_rows(runtime, reports):
    mappings = directory(runtime)
    rows = []
    for report in reports:
        messages = runtime.groups.messages(report["group_id"], {"from": report["period_start"], "to": report["period_end"], "inclusive": True})
        if report.get("max_message_id") is not None:
            messages = [m for m in messages if m["id"] <= report["max_message_id"]]
        rows.extend(build_rows(messages, report, mappings))
    return rows


def children_for_report(runtime, report):
    if report["scope"] != "master":
        return [report]
    children = runtime.reports.children(report["id"])
    if children:
        return children
    cluster = report.get("cluster_id") or report["group_id"].removeprefix("__master__:")
    groups = {g["group_id"] for g in runtime.scope_groups("cluster", cluster)}
    latest = {}
    for row in runtime.reports.history(scope="group"):
        if (row["group_id"] in groups and row["period_start"] >= report["period_start"]
                and row["period_end"] <= report["period_end"]):
            latest.setdefault(row["group_id"], row)
    return list(latest.values())
