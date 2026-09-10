# VFFL Coach

A phone-and-desktop app for one fantasy team in the VFFL. It reads Sleeper on a
schedule, applies the league constitution, and tells you:

- the best lineup for the week (scored with the league's own settings) and what to change
- injuries to your players and who replaces them
- waiver targets with a FAAB bid, and who to drop
- trade ideas that fix your weakest position using your surplus, with a pitch you can copy
- calendar traps: byes in the seeding week, bad playoff schedules, keeper value
- a push alert when a new trade idea appears that's worth at least `trade_alert_min_gain` points a week (default 3)

Claude is only called once a week for a short written summary (Tuesday, after waivers)
and when you ask it a question from the Coach tab. Everything else is plain rules code.

Sleeper's API is read-only, so the app can't submit lineups or claims. Every screen has
a button that opens the right Sleeper page for the one tap that's left.

## Setup (about 15 minutes)

1. **Create a GitHub repo** (private is fine) and push this folder to it.
2. **Turn on Pages**: Settings → Pages → Source "Deploy from a branch", branch `main`,
   folder `/docs`. Your app URL will be `https://<you>.github.io/<repo>/`.
3. **Let Actions write to the repo**: Settings → Actions → General → Workflow
   permissions → "Read and write permissions".
4. **Check `config.json`**: league and draft IDs are already yours. The engine finds your
   roster by matching "Andric" against the owner name in Sleeper; if that doesn't match
   your Sleeper display name, set `roster_id_override`.
5. **Run it once**: Actions → Update coach → Run workflow. When it finishes, open the app URL.
6. **On your phone**: open the URL in Safari/Chrome → Share → Add to Home Screen.

### Push notifications (optional)

1. Run `python scripts/make_vapid_keys.py` locally (needs `pip install cryptography`) and
   add the two values as repo secrets `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`.
   Put your email in `config.json → push.vapid_subject`.
2. Run the workflow once more so the public key reaches the app.
3. In the app (opened from the Home Screen on iPhone): Settings → Turn on notifications.
   Copy the text it shows into a repo secret named `PUSH_SUBSCRIPTIONS`.
   For a second device, run the same and add its entry to the JSON list.

### Weekly Claude notes and "ask the coach" (optional)

Add a repo secret `ANTHROPIC_API_KEY`. The Tuesday run writes ~200 words of notes
(one call, well under a cent). To ask questions from the app, paste the same key in
Settings — it stays in that browser only.

## Schedule

Seven runs a week (see `.github/workflows/update.yml`): Monday morning for the waiver
reminder, Tuesday after FAAB, Thursday and Saturday for injury reports, and three on
Sunday ending 15 minutes before the early kickoffs. Adjust the cron lines if you want more.

## Rules the engine applies

`rules.json` holds the constitution: playoff weeks, seeding week, trade deadline, FAAB
bid shares, keeper cost formula (round − 2, tag needed for rounds 1–2 and undrafted),
the 2027 amendment on tagged players, byes, good/bad playoff slates, and a do-not-roster
list. Scoring and roster slots are read live from Sleeper, so if the commish changes a
setting the app follows.

## Notes

- Projections come from Sleeper's own feed (the one the Sleeper app uses). It's not in
  their public docs, so if it ever moves, lineup and waiver numbers will show 0 — open an
  issue in your head and swap the URL in `fetch_projections`.
- Bye weeks are derived from Sleeper's schedule feed and fall back to the table in
  `rules.json`. Fill that table in when the full 2026 byes are known.
- To test offline: `python scripts/update.py --fixture <dir> --no-push`.
