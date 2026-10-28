# Master AWB enquiry report

The Master Report viewer now shows source-linked AWB enquiry rows above the narrative PDF.
Its Excel download uses the same rows. Each row represents one group, verified seller ID
(or unverified sender), and AWB within a saved child report's time window. The five old
follow-up/status/closure-summary columns are removed from this enquiry sheet.

## Identity directory

Import authoritative directory JSON in the Master Report viewer, or use authenticated
`PUT /master/directory`. `GET /master/directory` reads the currently saved directory.
The existing service identity/gateway access contract applies. This is shared configuration;
import replaces the directory, including optional shipment mappings. No seller or employee
identities are inferred from group ownership, names, or a `Seller`/`BIGSHIP` suffix alone.

Example shape (replace these illustrative records with actual master data):

```json
{
  "sellers": [{"id": "101", "name": "Example Seller"}],
  "employees": [{"id": "7", "name": "Example Agent"}],
  "members": [
    {"groupId": "example@g.us", "senderId": "seller@s.whatsapp.net", "sellerId": "101"},
    {"groupId": "example@g.us", "senderId": "staff@s.whatsapp.net", "employeeId": "7"}
  ],
  "shipments": []
}
```

Composite display names such as `Example Seller-101-Seller` are matched by ID first,
then normalized full name and role. Plain display names require a sender membership mapping
and a matching directory name. Unknown IDs use ❓; known IDs with mismatched names or roles
use ⚠️. Only validated sellers get a numeric enquiry count; only validated employees
can provide a linked staff response. Whitespace and letter case are normalized for names.
IDs must be strings. With an empty directory, rows remain visible but identities are unknown.

Optional shipment mappings use the existing performance mapping schema: `groupId`, `awb`,
`status`, `observedAt` (epoch seconds), and optional `closureMessageId`. Only observations at
or before report end apply. A closure message must belong to the report's group and period,
reference the AWB, come from verified staff, and link to that seller's enquiry.

## Counting and linking

- Extract all numeric 8–16 digit and supported letter-prefixed AWBs, including slash,
  backslash, comma, pipe, whitespace and newline-separated lists. Preserve leading zeros.
- Phone-shaped bare numbers remain AWB candidates in this report so valid shipments are
  not silently lost; explicit contact-labelled numbers are excluded. Extraction is not ERP
  shipment validation. Other report engines retain their existing phone-exclusion default.
- Count once per unique seller message and AWB. Repeated AWBs inside one message count once;
  repeated enquiries in different messages count again. Staff messages never increase counts.
- An explicit same-AWB staff reply links when there is only one sender conversation for that
  AWB. With multiple sellers, require a quote targeting the seller's enquiry. Generic nearby
  staff messages remain unassigned. Multi-AWB quoted replies apply to the referenced AWBs.
- Native WhatsApp captures quoted-message keys for new messages. Imports may provide
  `quoted_wa_id`. Historical messages without stored quote metadata can only use explicit AWBs.
- Revert TAT is elapsed time from the first enquiry to the first linked staff reply, including
  an acknowledgement. No response means `-` timestamps/TAT, not zero.
- Order status prefers an ERP mapping. Otherwise an explicit, non-question staff statement
  may supply an **unverified observed status**. The Evidence sheet records the source.
- Resolution requires mapped staff closure evidence or explicit receipt/resolution confirmation
  from the same verified seller after a linked staff reply. Bare thanks does not close a case.
  Explicit confirmations do not increase enquiry counts. Later seller enquiries reopen the row
  and clear an earlier resolution date.

## Data and export contracts

`GET /master/reports/{report_id}/rows` returns `headers`, source-linked `rows`, and count/identity
notes. A group report ID is also supported. Master IDs prefer stored child report relationships;
legacy masters without relationships use group reports in the master's cluster and period.
Rows carry source, response and resolution message IDs plus order-status provenance.

Excel contains Escalations, Evidence, and Report rules sheets. Time cells contain real Excel
datetimes in IST. Time uses `dd-mm-yyyy hh:mm:ss`, Esc. Raised uses `h:mm am/pm`,
and Revert Date/Resolution Date use `dd-mmm-yyyy`; unavailable timings show `-`. AWBs remain strings and user-controlled
text is protected against spreadsheet formula execution.

Rows are reconstructed from retained messages inside the saved report window and, where stored,
its maximum message ID. They are not immutable snapshots: correcting directory mappings or message
media extraction can change the reconstructed result. Deleted/expired messages cannot be recovered.
The existing AI narrative/PDF remains separate from the source-linked enquiry table.

The database adds a directory table and nullable `messages.quoted_wa_id` through the existing
idempotent initialization/migration path; no existing messages are deleted or rewritten.
