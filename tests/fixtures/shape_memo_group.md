Notes from the review are up and I wanted to capture the reasoning while it was fresh — there were a few threads worth preserving.

The migration itself went cleanly. We had budgeted an hour of downtime and used about eleven minutes of it, which is the third release in a row where the estimate has been badly conservative — worth revisiting how we size these, because padding the window has a real cost in how people plan around it.

On the rollback path: we tested it, it works, and it is slower than the forward migration by roughly a factor of three. That asymmetry is fine at the current data volume and stops being fine somewhere around ten times it — see notes/migration-sizing.md for the arithmetic.

The schema change is described in db/migrations/0042_add_index.sql if you want the detail. I have left the old column in place for one release so that a rollback does not need a data restore — we can drop it next cycle.

Two smaller things. The staging environment drifted again, which is now a recurring theme and probably deserves its own conversation rather than a line in a release note. And the test suite gained about forty seconds, almost all of it in one fixture that builds a large graph; nobody needs to act on that today but it will matter eventually.

Nothing here is urgent. I mostly wanted it written down somewhere durable before the details evaporate.

One more thing while I have the page. The observability story around the migration path is thinner than I would like — we can see that a run started and that it finished, but the middle is opaque, and when the staging drift bit us last month it took an embarrassingly long time to work out which step had actually stalled. I do not think this needs a dashboard. A handful of structured log lines at the phase boundaries would have collapsed that investigation from an afternoon into about five minutes, and would cost roughly an hour to add.

Related, and genuinely minor: the runbook in ops/runbooks/migrate.md still describes the pre-2025 procedure, including a manual step we removed two releases ago. Anyone following it literally would do something unnecessary and mildly alarming. I will fix it unless someone objects, since the correct version is just shorter.
