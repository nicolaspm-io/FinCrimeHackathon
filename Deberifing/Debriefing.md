Fin crime hackathon · Sanctions screening
Is my customer sanctioned?
Screening compares each customer with sanctions, PEP, enforcement and adverse media lists, and raises an alert when a name looks similar. Each alert has one or more hits: list profiles that might be the customer. Build a system that decides, for every hit, whether it is the customer.

379 development alerts
766 hits to decide
71,865 customer records
50 practice labels
Unseen set held back
What is name screening?
Identity, not policy: is the hit the customer?

A payments company screens every customer against sanctions, PEP, enforcement and adverse media lists. When a name looks similar, an alert is raised, and an analyst has to decide whether the listed profile really is the customer or just someone with a similar name. How serious the listing is, or whether the customer would still be acceptable, does not matter here. Only identity does.

The three decisions
Decision	Meaning
DIFFERENT_ENTITY	The evidence shows the hit is a different person or company. The hit is cleared.
SAME_ENTITY	The evidence shows the hit is the customer. The alert goes to the compliance team.
REVIEW	The evidence cannot settle it. A person looks at it.
An alert is cleared only when every hit in it is DIFFERENT_ENTITY. Clearing a hit that is the customer is the worst mistake. When in doubt, REVIEW.

The challenge
The customer is spread over the bank's systems, and the hit's facts can be anywhere.

Know who the customer is first
Their full picture is spread over the system of record, accounts, onboarding, the KYC provider and the support tool, which do not share one key. Follow whatever link the alert carries. Without one, find the records that fit what was screened. Never use another customer's records.

One person, one picture
Records that share a phone number and the same full date of birth belong to the same person. A shared phone or address alone does not: family members often do. If several people fit an alert and lead to different decisions, the decision is REVIEW with AMBIGUOUS_CUSTOMER_RECORD.

Verified beats declared
The most recent approved identity check is the most reliable source for legal name, date of birth and document country. Rejected or expired checks are not evidence.

Gather the hit's facts wherever they are
A hit's date of birth may be in its dobs field, in a sentence of its profile notes, or only in web search results, most of which are about namesakes.

Names are messy
Compare name parts ignoring case, accents, punctuation and brackets, with name variants (Muhammad, Mohammed, Mohamed), parts written together or apart, and small spelling edits. Aliases and nicknames count. Placeholders such as NA, TEST or UNKNOWN are not name parts.

When to clear, when to confirm
From Diligent's screening SOP (POLICY.md), in the data.

Clear only on a contradiction
Time
The customer was younger than 16 at the earliest dated report, or a company was incorporated after it, or the profile gives a date of death before the customer was screened.

Date of birth
The customer's birth year is more than 1 year outside every birth year the hit gives, or both give a month in the same year and the months or days differ.

Location
Every country tied to the customer differs from every country tied to the hit. A country difference does not clear a hit whose name and date of birth agree: that is REVIEW.

Name
The customer's verified legal name agrees with none of the hit's names or aliases.

Type
One is a person and the other a company.

Confirm only on agreement
Confirm a person when name and date of birth agree and nothing contradicts them. Confirm a company when name and country agree and nothing contradicts them. Otherwise, REVIEW.

Reason codes
Give the main reason with one code that belongs to the decision, and cite the records it rests on by their IDs, plus the customer record it resolved (null if none was found).

Decision	Reason code	Use it when
DIFFERENT_ENTITY	TEMPORAL_IMPOSSIBILITY	The customer was too young, or not yet incorporated, at the earliest dated report
DIFFERENT_ENTITY	DOB_MISMATCH	The customer's date of birth (or a company's incorporation date) contradicts every birth date the hit gives
DIFFERENT_ENTITY	LOCATION_MISMATCH	Every country tied to the customer differs from every country tied to the hit
DIFFERENT_ENTITY	NAME_MISMATCH_VERIFIED	The customer's verified legal name does not agree with any of the hit's names
DIFFERENT_ENTITY	ENTITY_TYPE_MISMATCH	One is a person and the other a company
DIFFERENT_ENTITY	DECEASED	The hit's profile gives a date of death before the customer was screened
SAME_ENTITY	NAME_DOB_LOCATION_MATCH	Name, date of birth and country all agree and nothing contradicts them
SAME_ENTITY	NAME_DOB_MATCH	Name and date of birth agree and nothing contradicts them
SAME_ENTITY	NAME_LOCATION_MATCH	Companies only: name and country agree and nothing contradicts them
REVIEW	INSUFFICIENT_DATA	Nothing settles it: no contradiction, and not enough agreement to confirm
REVIEW	AMBIGUOUS_CUSTOMER_RECORD	More than one person in the system of record fits the alert and they lead to different decisions
The data
A development set to build on, and an unseen set you never see.

Your system reads the JSON Lines files in the dev/ folder of the data room: they are the source of truth, and the unseen set comes in the same format. The workbook below is a read-only view of the development set, for browsing, without answers. It also holds 50 practice labels with the path through the files that leads to each answer.

Tab	Rows	What it is
Alerts	379	One row per screening alert: the customer as screened, the link to the customer if the alert has one, and the hit IDs
Hits	766	One row per watchlist profile that matched. Decide each of these
Customers	71,865	The bank's system of record
Accounts, Onboarding	86,227 · 71,865	Links from account numbers and logins to customers, and from accounts to KYC applicants
KYC checks	53,977	Identity checks by the KYC provider, keyed by applicant ID
Notes	31,675	Support and onboarding notes, filed by the customer's email
Search results	16,791	Web search results by name; most are about namesakes
Practice labels	50	Answered hits, with the path through the files to the answer
Policy		The SOP (POLICY.md) and the name variants (name_variants.json)
How the tabs link. Alerts.hit_ids → Hits.hit_id. Alerts.account_ref → Accounts → Customers. Alerts.login_handle and Alerts.email → Customers. Accounts → Onboarding → KYC checks. Customers.email → Notes. Hit name → Search results.

