# Review queue

Matches the robot was not fully confident about. Check each one; if two
entries are the same show, add the wrong spelling to the `aliases` table
pointing at the right `show_id` and re-run `python run.py --match`.

| Publication | Headline | Matched to | Confidence |
|---|---|---|---|
| Binge Fringe | [REVIEW: Club NVRLND, David Adkin & RJG Productions, with Midnight Theatricals and NewYorkRep, EdFringe 2025 ★★★★](https://www.bingefringe.com/2025/08/30/review-club-nvrlnd-david-adkin-rjg-productions-with-midnight-theatricals-and-newyorkrep-edfringe-2025-%e2%98%85%e2%98%85%e2%98%85%e2%98%85/) | David Adkin & RJG Productions, with Midnight Theatricals and NewYorkRep | 93% |

## Held — not on the leaderboard

These matched no festival programme and did not look like a review of a
single Edinburgh show. If one is wrong, delete its row from the `holds`
table and re-run `python run.py --match` to let it back in.

| Publication | Headline | Why held |
|---|---|---|
| The Scotsman | [Artist Rooms: Bourgeois, Chadwick, Mapplethorpe, Edinburgh review: 'confusing'](https://www.scotsman.com/arts-and-culture/art/artist-rooms-bourgeois-chadwick-mapplethorpe-edinburgh-review-confusing-5287441) | This is a review of an art exhibition featuring multiple artists, not a single named show or performance at an Edinburgh festival. |
| The Scotsman | [Aqsa Arif: Raindrops of Rani, Edinburgh Printmakers](https://www.scotsman.com/arts-and-culture/art/edinburgh-art-festival-reviews-robert-powell-aqsa-arif-victoria-crowe-sian-davey-5278190#aqsa-arif-raindrops-of-rani-edinburgh-printmakers) | This appears to be a visual art exhibition at Edinburgh Printmakers rather than a performance or show at an Edinburgh festival. |
