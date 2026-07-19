# GBP API access — application runbook

The Google Business Profile APIs are gated: you apply, Google reviews (typically up
to ~14 days), and approval shows up as quota on your Cloud project. This is the
runbook for LocalPulse's application (spec §15). The submission itself must be done
from your Google account — everything below is prepared so it's a 15-minute task.

## 0. The blocking prerequisite — start this first

Google's stated eligibility (developers.google.com/my-business/content/prereqs):

- The applicant email must be **listed as an owner/manager on a Business Profile**
  that is **verified and active for 60+ days**, and
- that profile must list a **website** representing the business.

For LocalPulse that means: **get your Google account added as a manager on the
pilot bakery's Business Profile now** (Business Profile → Settings → Managers →
Add). If the pilot's profile is newly verified, the 60-day clock is the long pole —
arranging access today is what actually unblocks the application. Keep the profile
complete and current; Google says reviewers look at that.

## 1. One-time Cloud setup (before the form)

1. Sign in with the Google account that has manager access to the pilot's GBP.
2. [Google Cloud Console](https://console.cloud.google.com) → create a project
   (suggested id: `localpulse-prod`).
3. Note the **Project Number** from the project Dashboard — the form asks for it.

## 2. Submit the application

Form: https://support.google.com/business/workflow/16726127 (sign in with the same
account). Choose **“Application for Basic API Access”** and fill it in. Suggested
answers, adapt as needed:

| Field | Suggested answer |
|---|---|
| Contact email | the owner/manager email from step 0 |
| Project Number | from step 1 |
| Company / business name | your registered business name (or the pilot business if applying as the business) |
| Business website | the website listed on the GBP |
| Use case (free text) | see draft below |

Use-case draft (edit to taste, keep it concrete and honest):

> LocalPulse is a software tool that helps small local businesses (bakeries,
> salons) in India keep their Google Business Profile active and responsive. On
> behalf of businesses that have granted us manager access via OAuth, we: (1)
> publish local posts that the business owner has explicitly approved, (2) list
> new reviews and publish owner-approved replies to them, and (3) read basic
> business information and performance metrics to build a monthly report for the
> owner. Every published post and review reply is drafted first and approved by
> the business owner before the API call is made; nothing is published without an
> owner approval on record. Expected volume is low: a few posts per business per
> week and hourly review polling, well within default quotas.

Notes that help review go smoothly:

- Apply as a legitimate business with a real website; the reviewer cross-checks.
- Don't overstate scope — LocalPulse needs posts, reviews/replies, business info,
  and performance data only.
- Requests are reviewed within roughly 14 days; you'll get a follow-up email.

## 3. Confirm approval

Cloud Console → APIs & Services → enabled Business Profile API → **Quotas**:

- **0 QPM** = not approved yet.
- **300 QPM** = approved.

## 4. Post-approval setup (feeds the P3 integration work)

1. Enable the APIs the integration needs (API Library): **Google My Business API**
   (legacy v4 — still the surface for local posts and review replies), **My
   Business Account Management API**, **My Business Business Information API**,
   and **My Business Notifications API**. (The Lodging / Place Actions /
   Verifications / Q&A APIs exist but LocalPulse doesn't need them yet.)
2. OAuth consent screen (external) → add scope
   `https://www.googleapis.com/auth/business.manage`.
3. Create **OAuth client ID** credentials → fill `GBP_OAUTH_CLIENT_ID` and
   `GBP_OAUTH_CLIENT_SECRET` in `.env`. Per-client refresh tokens are stored
   encrypted in the DB, never in `.env` (CLAUDE.md).
4. Engineering notes for the `tools/gbp.py` swap: there is **no sandbox** — use
   `validateOnly=true` on write calls while testing, and send header
   `X-GOOG-API-FORMAT-VERSION: 2` for structured error details.

## Status

- [ ] Manager access to the pilot GBP arranged (step 0)
- [ ] Cloud project created, project number noted
- [ ] Application submitted (date: ____)
- [ ] Approval confirmed (quota 300 QPM)
- [ ] OAuth credentials in `.env`
