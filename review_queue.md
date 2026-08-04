# Review queue

Matches the robot was not fully confident about. Check each one; if two
entries are the same show, add the wrong spelling to the `aliases` table
pointing at the right `show_id` and re-run `python run.py --match`.

_Nothing to check. 🎉_

## Held — not on the leaderboard

These matched no festival programme and did not look like a review of a
single Edinburgh show. If one is wrong, delete its row from the `holds`
table and re-run `python run.py --match` to let it back in.

| Publication | Headline | Why held |
|---|---|---|
| Binge Fringe | [REVIEW: Compression Test, 92 Beats, Durham Fringe 2026 ★★★★](https://www.bingefringe.com/2026/08/03/review-compression-test-92-beats-durham-fringe-2026-%e2%98%85%e2%98%85%e2%98%85%e2%98%85/) | This reviews a show at Durham Fringe, not an Edinburgh festival. |
| Corr Blimey | [Review: Bard in the Botanics: The Duchess of Malfi – Kibble Palace, Glasgow](https://corrblimey.uk/2026/07/20/review-bard-in-the-botanics-the-duchess-of-malfi-kibble-palace-glasgow/) | This reviews a show in Glasgow (Kibble Palace), not at an Edinburgh festival. |
| Binge Fringe | [REVIEW: Kópakonan, Hangmore, Durham Fringe 2026 ★★★★](https://www.bingefringe.com/2026/07/30/review-kopakonan-hangmore-durham-fringe-2026-%e2%98%85%e2%98%85%e2%98%85%e2%98%85/) | This is a review of a show at Durham Fringe, not an Edinburgh festival. |
| Binge Fringe | [REVIEW: Queen of The Empire, Durham Fringe 2026 ★★★](https://www.bingefringe.com/2026/07/30/review-queen-of-the-empire-durham-fringe-2026-%e2%98%85%e2%98%85%e2%98%85/) | This reviews a show at Durham Fringe, not an Edinburgh festival. |
