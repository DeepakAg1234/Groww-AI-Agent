# Groww Weekly Review Pulse

## Product

Build this for the **Groww** platform (Groww Stocks, Mutual Fund, and IPO — apps on Google Play).

Primary Play Store listing:

https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN

This version uses **Google Play reviews only**. The App Store is deferred.

## Goal

Turn raw mobile-store feedback into a weekly pulse the team can scan in minutes: what users care about, what they actually said, and what to do next.

Reviews are already public. The job is to aggregate, theme, summarize, and deliver that insight through familiar surfaces — Google Docs for the written pulse and Gmail for a draft you can send yourself — without handling credentials or REST wiring yourself.

## Done looks like

1. Pull recent Google Play reviews for the product, within the constraints below.
2. Cluster them into a small set of themes and distill a one-page weekly note.
3. Put that note where stakeholders can read it (Google Docs).
4. Create a draft email to yourself (or an alias) that contains or links to that pulse (Gmail).

## Who this helps

| Audience | Why |
| --- | --- |
| Product / Growth | Prioritize fixes and improvements from real signals |
| Support | Align messaging with what users are actually saying |
| Leadership | One-page health check without drowning in raw reviews |

## What to build

- Import reviews from roughly the last **8–12 weeks**. Keep fields such as rating, title, text, and date — whatever the export provides.
- Group reviews into **at most 5 themes**. Examples that may fit: onboarding, KYC, payments, statements, withdrawals. Pick themes that match the product.
- Generate a weekly one-page note with:
  - **Top 3 themes** (a subset of the clustered themes)
  - **3 user quotes**
  - **3 action ideas**
- Draft an email with the note to yourself or an alias.

## Weekly pulse contents

The one-page pulse must include:

- **Top themes** — what people are talking about most
- **Real user quotes** — verbatim snippets from reviews; no invented wording
- **Three action ideas** — concrete next steps grounded in the themes

Then send yourself a draft email containing this weekly note, or a clear pointer to it.

## Integrations

Use **MCP (Model Context Protocol)** servers for Google Docs and Gmail — for example, creating or updating the pulse document and creating the draft message — rather than integrating Google APIs directly. No bespoke OAuth client and REST client as the primary integration path.

MCP servers expose tools the agent or app can call. Use that pattern so Docs and Gmail stay consistent with the course tooling and avoid duplicating auth and HTTP plumbing.

Choose MCP servers or connectors the environment provides for Docs and Gmail. The requirement is MCP-first, not calling Google APIs manually.

## Constraints

- **Reviews.** Use public Google Play review exports only. No scraping behind store logins and no automation that violates store terms of service. The App Store is out of this version.
- **Themes.** Maximum 5 themes for clustering. The written pulse highlights the top 3.
- **Length.** Keep the note scannable, about 250 words where applicable.
- **Privacy.** Do not include PII. No usernames, emails, device IDs, or other identifiable reviewer data in any artifact. Quotes should be anonymous and stripped as needed.
