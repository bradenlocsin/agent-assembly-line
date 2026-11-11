# Human review rubric

You'll see one lead at a time: the business, its **evidence packet**, and 2–3 anonymous responses.
Each response is an email Braden might send, plus the fit score and offer it chose. Judge every
response **only against the evidence shown**. You know nothing else about these businesses.

Score each response before looking at the next lead. Don't try to guess which system wrote it,
and don't open `KEY_sealed.json` until every row in `review_sheet.csv` is filled in.

## Scores (1–5)

**accuracy_1to5: Is everything it says supported by the evidence?**
- 5: every factual statement is in the evidence
- 4: one small stretch (e.g. "busy shop" from "212 reviews")
- 3: one clear unsupported claim that doesn't change the pitch
- 2: an unsupported claim the pitch depends on, or a wrong fact (e.g. says they lack online booking when they have it)
- 1: invented names, numbers, quotes or events

Also record **unsupported_claims_count**, the number of distinct statements not backed by the evidence.
An invented owner name counts as one. Saying "Hi there," when no owner is named is correct, not a flaw.

**personalization_1to5: Could this only have been written to this business?**
- 5: built around a specific, relevant detail from the evidence; clearly not a template
- 3: mentions the business's name or category, but the body could go to any similar shop
- 1: generic; nothing specific to this business

**writing_1to5: Would a busy owner read it and not cringe?**
- 5: short, clear, natural, one easy ask, no hype
- 3: fine but wordy, stiff, or the ask is unclear
- 1: salesy, confusing, buzzword-heavy, or too long (110 words is the limit)

**offer_fit_1to5: Is the pitched automation the right one for this business, given the evidence?**
- 5: obviously the best fit (e.g. missed-call text-back for a business whose phone goes to voicemail)
- 3: defensible, but another offer fits clearly better
- 1: wrong (e.g. pitching booking automation to a business that already books online), or pitching
  a chain or a closed business at all

## Decisions

**would_send_yes_no**: would you send this email with at most minor edits (a word or two)?
This is the main outcome. A permanently closed business should get `NO_EMAIL`; for that lead,
"yes" means the response correctly declined to write an email.

**rank_within_lead**: rank the responses for this lead (1 = best). Ties aren't allowed. Pick one.

**notes**: optional. Note anything that drove your score, especially invented facts.

## Fairness rules for reviewers

- Score in one sitting if possible, with a short break every 20 rows.
- If you built this project, you are a biased reviewer. Blinding helps, but if possible get a second
  person to score the same sheet, then pass both files to `score --reviews a.csv b.csv` to check agreement.
- Don't change scores after opening the key.
